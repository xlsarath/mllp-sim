"""A tiny read-only HTTP endpoint for inspecting traffic.

Routes:

* ``GET /healthz`` - liveness plus counters (for container health checks);
* ``GET /messages?limit=N`` - most recent exchanges as JSON;
* ``GET /`` - a single self-contained HTML page that polls ``/messages``.

Built on asyncio streams so the package has no web-framework dependency. It
speaks just enough HTTP/1.1 for browsers, curl and health probes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from http import HTTPStatus
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlsplit

from mllp_sim import __version__
from mllp_sim.traffic import TrafficLog

log = logging.getLogger(__name__)

MAX_REQUEST_HEAD = 8 * 1024
REQUEST_TIMEOUT_S = 5.0
DEFAULT_LIMIT = 50


class StatusServer:
    """Serves ``/healthz``, ``/messages`` and the traffic page."""

    def __init__(self, traffic: TrafficLog, host: str, port: int, *, scenario: str = "") -> None:
        self.traffic = traffic
        self.host = host
        self.port = port
        self.scenario = scenario
        self._server: asyncio.Server | None = None
        self._page: bytes | None = None

    @property
    def bound_port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("status server is not started")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connect, self.host, self.port)
        log.info("HTTP status on http://%s:%d/ (healthz, messages)", self.host, self.bound_port)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), REQUEST_TIMEOUT_S)
            status, content_type, body = self._route(head)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            status, content_type, body = _json(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
        except (ConnectionError, OSError):
            writer.close()
            return
        except Exception:
            log.exception("HTTP handler failed")
            status, content_type, body = _json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "error"})
        head_only = body is None
        payload = b"" if body is None else body
        response = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Cache-Control: no-store\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        with contextlib.suppress(ConnectionError, OSError):
            writer.write(response if head_only else response + payload)
            await writer.drain()
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()

    def _route(self, head: bytes) -> tuple[HTTPStatus, str, bytes | None]:
        if len(head) > MAX_REQUEST_HEAD:
            return _json(HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE, {"error": "header too large"})
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = request_line.split(" ")
        if len(parts) != 3:
            return _json(HTTPStatus.BAD_REQUEST, {"error": "bad request line"})
        method, target, _version = parts
        if method not in ("GET", "HEAD"):
            return _json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "only GET is supported"})
        url = urlsplit(target)
        if url.path == "/healthz":
            result = _json(HTTPStatus.OK, self._health())
        elif url.path == "/messages":
            result = self._messages(parse_qs(url.query))
        elif url.path in ("/", "/index.html"):
            result = (HTTPStatus.OK, "text/html; charset=utf-8", self._index())
        else:
            result = _json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        return (result[0], result[1], None) if method == "HEAD" else result

    def _health(self) -> dict[str, Any]:
        uptime = (datetime.now(UTC) - self.traffic.started_at).total_seconds()
        return {
            "status": "ok",
            "version": __version__,
            "scenario": self.scenario,
            "uptime_s": round(uptime, 1),
            "counts": self.traffic.counts(),
        }

    def _messages(self, query: dict[str, list[str]]) -> tuple[HTTPStatus, str, bytes]:
        raw_limit = query.get("limit", [str(DEFAULT_LIMIT)])[0]
        try:
            limit = int(raw_limit)
        except ValueError:
            return _json(HTTPStatus.BAD_REQUEST, {"error": "limit must be an integer"})
        if limit < 1:
            return _json(HTTPStatus.BAD_REQUEST, {"error": "limit must be >= 1"})
        items = [e.to_dict() for e in self.traffic.recent(limit)]
        return _json(HTTPStatus.OK, {"count": len(items), "messages": items})

    def _index(self) -> bytes:
        if self._page is None:
            self._page = files("mllp_sim").joinpath("static/index.html").read_bytes()
        return self._page


def _json(status: HTTPStatus, data: Any) -> tuple[HTTPStatus, str, bytes]:
    return status, "application/json", json.dumps(data, ensure_ascii=False).encode("utf-8")
