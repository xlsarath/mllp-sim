"""The MLLP server: receive messages, apply the scenario, answer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time

from mllp_sim.ack import build_ack
from mllp_sim.framing import (
    END_BLOCK,
    FrameDecoder,
    FrameTooLargeError,
    encode_frame,
)
from mllp_sim.hl7 import ControlIdGenerator, HL7ParseError, Message
from mllp_sim.outbound import FollowUpDispatcher
from mllp_sim.rules import Decision, ReplyMode, RuleEngine
from mllp_sim.scenario import Scenario
from mllp_sim.traffic import Exchange, TrafficLog

log = logging.getLogger(__name__)

READ_CHUNK = 64 * 1024
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"})


class MllpServer:
    """An asyncio MLLP server driven by a :class:`Scenario`.

    Messages on one connection are handled strictly in order, as MLLP requires;
    separate connections are independent.
    """

    def __init__(
        self,
        scenario: Scenario,
        *,
        traffic: TrafficLog | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.scenario = scenario
        self.settings = scenario.server
        self.traffic = traffic or TrafficLog(history=self.settings.history)
        self.rng = rng or random.Random(scenario.seed)
        self.engine = RuleEngine(scenario.rules, scenario.default_action, self.rng)
        self.control_ids = ControlIdGenerator()
        self.followups = FollowUpDispatcher(
            self.traffic, self.control_ids, rng=self.rng, encoding=self.settings.encoding
        )
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int:
        """The bound port (useful when configured with port 0)."""
        if self._server is None or not self._server.sockets:
            raise RuntimeError("server is not started")
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def active_connections(self) -> int:
        return len(self._connections)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connect, self.settings.host, self.settings.port
        )
        log.info(
            "MLLP server listening on %s:%d scenario=%r rules=%d",
            self.settings.host,
            self.port,
            self.scenario.name,
            len(self.scenario.rules),
        )
        self._warn_about_followup_loops()

    def _warn_about_followup_loops(self) -> None:
        """A follow-up aimed at this very server re-triggers itself; flag it early."""
        actions = [(r.name, r.action) for r in self.scenario.rules]
        actions.append(("<default>", self.scenario.default_action))
        for rule, action in actions:
            for followup in action.followups:
                if followup.port == self.port and followup.host in _LOCAL_HOSTS:
                    log.warning(
                        "rule=%s followup=%s targets this server (%s:%d); "
                        "it may trigger itself in a loop",
                        rule,
                        followup.name,
                        followup.host,
                        followup.port,
                    )

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            for task in list(self._connections):
                task.cancel()
            await asyncio.gather(*list(self._connections), return_exceptions=True)
            await self._server.wait_closed()
            self._server = None
            log.info("MLLP server stopped")
        await self.followups.aclose()

    async def __aenter__(self) -> MllpServer:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- connection handling ------------------------------------------------------------

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        peer = _format_peer(writer.get_extra_info("peername"))
        decoder = FrameDecoder(self.settings.max_message_bytes)
        log.info("connection opened peer=%s", peer)
        reason = "closed by peer"
        try:
            while True:
                chunk = await reader.read(READ_CHUNK)
                if not chunk:
                    break
                keep_open = True
                for payload in decoder.feed(chunk):
                    keep_open = await self._handle(payload, peer, writer)
                    if not keep_open:
                        break
                if not keep_open:
                    reason = "dropped by scenario"
                    break
        except FrameTooLargeError as exc:
            reason = "frame too large"
            log.warning("closing connection peer=%s reason=%s", peer, exc)
        except (ConnectionError, OSError) as exc:
            reason = f"network error: {exc}"
        except asyncio.CancelledError:
            reason = "server shutdown"
            raise
        finally:
            self._connections.discard(task)
            stats = decoder.stats
            if decoder.has_partial_frame:
                log.warning("peer=%s disconnected mid-frame; partial message discarded", peer)
            if stats.discarded_bytes or stats.abandoned_frames or stats.missing_carriage_returns:
                log.warning(
                    "framing deviations peer=%s discarded_bytes=%d abandoned_frames=%d "
                    "missing_cr=%d",
                    peer,
                    stats.discarded_bytes,
                    stats.abandoned_frames,
                    stats.missing_carriage_returns,
                )
            log.info("connection closed peer=%s messages=%d reason=%s", peer, stats.frames, reason)
            writer.close()
            with contextlib.suppress(ConnectionError, OSError, asyncio.CancelledError):
                await writer.wait_closed()

    async def _handle(self, payload: bytes, peer: str, writer: asyncio.StreamWriter) -> bool:
        """Process one message. Returns ``False`` when the connection must be closed."""
        started = time.perf_counter()
        text = payload.decode(self.settings.encoding, errors="replace")
        parse_error: str | None = None
        try:
            message: Message | None = Message.parse(text)
        except HL7ParseError as exc:
            message, parse_error = None, str(exc)
            log.warning("unparseable message peer=%s bytes=%d error=%s", peer, len(payload), exc)

        message_type = message.message_type if message else None
        control_id = message.control_id if message else None
        log.debug("received peer=%s body:\n%s", peer, text.replace("\r", "\n"))

        if message is not None and message.message_code == "ACK":
            # Acknowledgements are never acknowledged (HL7 v2 original mode).
            log.info("received ACK peer=%s control_id=%s; not replying", peer, control_id)
            self._record(
                peer=peer,
                message_type=message_type,
                control_id=control_id,
                decision=None,
                reply=ReplyMode.NONE.value,
                ack_code=None,
                started=started,
                text=text,
                ack_text=None,
            )
            return True

        decision = self.engine.evaluate(message)
        action = decision.action
        if action.delay:
            await asyncio.sleep(action.delay.sample_seconds(self.rng))

        ack_text: str | None = None
        keep_open = True
        if action.reply is ReplyMode.DROP:
            keep_open = False
        elif action.reply is not ReplyMode.NONE:
            ack_text = build_ack(
                message,
                action.ack,
                control_id=self.control_ids.next(),
                error_text=action.error,
                error_code=action.error_code,
                application=self.settings.application,
            )
            await self._write_reply(writer, ack_text, action.reply)

        ack_code = action.ack.value if ack_text is not None else None
        exchange = self._record(
            peer=peer,
            message_type=message_type,
            control_id=control_id,
            decision=decision,
            reply=action.reply.value,
            ack_code=ack_code,
            started=started,
            text=text,
            ack_text=ack_text,
            error=parse_error,
        )
        log.info(
            "message peer=%s type=%s control_id=%s rule=%s reply=%s ack=%s latency_ms=%.1f",
            peer,
            message_type,
            control_id,
            decision.rule,
            action.reply.value,
            ack_code,
            exchange.latency_ms or 0.0,
        )
        log.debug("replied peer=%s body:\n%s", peer, (ack_text or "").replace("\r", "\n"))

        if message is not None:
            for followup in action.followups:
                self.followups.dispatch(followup, message, decision.rule)
        return keep_open

    async def _write_reply(
        self, writer: asyncio.StreamWriter, ack_text: str, mode: ReplyMode
    ) -> None:
        payload = ack_text.encode(self.settings.encoding, errors="replace")
        if mode is ReplyMode.GARBLED:
            # No start block and no trailing CR: a strict receiver never sees a frame.
            data = payload + END_BLOCK
        elif mode is ReplyMode.DUPLICATE:
            data = encode_frame(payload) * 2
        else:
            data = encode_frame(payload)
        writer.write(data)
        await writer.drain()

    def _record(
        self,
        *,
        peer: str,
        message_type: str | None,
        control_id: str | None,
        decision: Decision | None,
        reply: str,
        ack_code: str | None,
        started: float,
        text: str,
        ack_text: str | None,
        error: str | None = None,
    ) -> Exchange:
        return self.traffic.add(
            direction="inbound",
            peer=peer,
            message_type=message_type,
            control_id=control_id,
            rule=decision.rule if decision else None,
            reply=reply,
            ack_code=ack_code,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            message=text,
            ack=ack_text,
            error=error,
        )


def _format_peer(peername: object) -> str:
    if isinstance(peername, tuple) and len(peername) >= 2:
        host, port = peername[0], peername[1]
        return f"[{host}]:{port}" if ":" in str(host) else f"{host}:{port}"
    return str(peername) if peername else "unknown"
