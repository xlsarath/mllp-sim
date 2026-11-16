from __future__ import annotations

import asyncio
import json

import pytest

from mllp_sim.client import MllpClient
from mllp_sim.traffic import TrafficLog
from mllp_sim.web import StatusServer

from .conftest import ADT_A01, ServerFactory


async def http_get(port: int, target: str, method: str = "GET") -> tuple[int, dict, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"{method} {target} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), 2)
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = dict(line.split(": ", 1) for line in lines[1:])
    return status, headers, body


@pytest.fixture
async def status_server() -> StatusServer:
    traffic = TrafficLog(history=10)
    server = StatusServer(traffic, "127.0.0.1", 0, scenario="test")
    await server.start()
    yield server
    await server.close()


async def test_healthz(status_server: StatusServer) -> None:
    status, headers, body = await http_get(status_server.bound_port, "/healthz")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    data = json.loads(body)
    assert data["status"] == "ok"
    assert data["scenario"] == "test"


async def test_messages_reflect_traffic(make_server: ServerFactory) -> None:
    traffic = TrafficLog(history=10)
    async with make_server(traffic=traffic) as server:
        web = StatusServer(traffic, "127.0.0.1", 0)
        await web.start()
        try:
            async with MllpClient("127.0.0.1", server.port) as client:
                await client.send(ADT_A01)
                await client.send(ADT_A01.replace("MSG00001", "MSG00002"))
            status, _, body = await http_get(web.bound_port, "/messages?limit=1")
            _, _, health = await http_get(web.bound_port, "/healthz")
        finally:
            await web.close()
    assert status == 200
    data = json.loads(body)
    assert data["count"] == 1
    assert data["messages"][0]["control_id"] == "MSG00002"
    assert data["messages"][0]["ack_code"] == "AA"
    assert json.loads(health)["counts"]["inbound"] == 2


async def test_index_page(status_server: StatusServer) -> None:
    status, headers, body = await http_get(status_server.bound_port, "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"mllp-sim" in body


async def test_head_has_no_body(status_server: StatusServer) -> None:
    status, headers, body = await http_get(status_server.bound_port, "/healthz", "HEAD")
    assert status == 200
    assert int(headers["Content-Length"]) == 0
    assert body == b""


@pytest.mark.parametrize(
    ("method", "target", "expected"),
    [
        ("GET", "/nope", 404),
        ("POST", "/messages", 405),
        ("GET", "/messages?limit=abc", 400),
        ("GET", "/messages?limit=0", 400),
    ],
)
async def test_errors(status_server: StatusServer, method: str, target: str, expected: int) -> None:
    status, _, _ = await http_get(status_server.bound_port, target, method)
    assert status == expected


async def test_malformed_request(status_server: StatusServer) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", status_server.bound_port)
    writer.write(b"garbage\r\n\r\n")
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), 2)
    writer.close()
    assert raw.startswith(b"HTTP/1.1 400")
