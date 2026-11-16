"""MLLP client: send HL7 v2 messages and wait for their ACKs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass
from types import TracebackType

from mllp_sim.ack import AckCode
from mllp_sim.framing import DEFAULT_MAX_FRAME_BYTES, FrameDecoder, MLLPError, encode_frame
from mllp_sim.hl7 import HL7ParseError, Message, normalize_segments

log = logging.getLogger(__name__)

READ_CHUNK = 64 * 1024
BATCH_ENVELOPE = ("FHS", "BHS", "BTS", "FTS")


class MllpTimeoutError(MLLPError):
    """No acknowledgement arrived within the timeout."""


class ConnectionClosedError(MLLPError):
    """The peer closed the connection before acknowledging."""


@dataclass(frozen=True, slots=True)
class AckResult:
    """The acknowledgement received for one sent message."""

    sent_control_id: str
    text: str
    message: Message | None
    latency_ms: float

    @property
    def code(self) -> str | None:
        """MSA-1, or ``None`` when the reply is not a parseable ACK."""
        if self.message is None:
            return None
        return self.message.get("MSA-1") or None

    @property
    def acked_control_id(self) -> str:
        return self.message.get("MSA-2") if self.message else ""

    @property
    def accepted(self) -> bool:
        return self.code in (AckCode.AA, AckCode.CA)

    @property
    def control_id_matches(self) -> bool:
        return self.acked_control_id == self.sent_control_id

    @property
    def error_text(self) -> str:
        """Best-effort error description from MSA-3 or ERR-8."""
        if self.message is None:
            return ""
        return self.message.get("MSA-3") or self.message.get("ERR-8")


class MllpClient:
    """One TCP connection to an MLLP endpoint; messages are sent one at a time.

    Usage::

        async with MllpClient("localhost", 2575) as client:
            result = await client.send(text)
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float = 10.0,
        encoding: str = "utf-8",
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.encoding = encoding
        self._decoder = FrameDecoder(max_frame_bytes)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: deque[bytes] = deque()
        # Control ids already acknowledged on this connection, to recognise late
        # duplicate ACKs instead of mistaking them for the next message's ACK.
        self._acknowledged: deque[str] = deque(maxlen=1024)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
        except TimeoutError as exc:
            raise MllpTimeoutError(f"timed out connecting to {self.address}") from exc
        log.debug("connected to %s", self.address)

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()

    async def __aenter__(self) -> MllpClient:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def send(self, text: str) -> AckResult:
        """Send one message and return its acknowledgement.

        The text is normalised to ``\\r`` segment terminators. It does not have
        to be valid HL7 - sending malformed messages is a legitimate test.
        """
        if self._writer is None:
            await self.connect()
        assert self._writer is not None
        message_text = normalize_segments(text) + "\r"
        try:
            control_id = Message.parse(message_text).control_id
        except HL7ParseError:
            control_id = ""

        started = time.perf_counter()
        deadline = asyncio.get_running_loop().time() + self.timeout
        try:
            self._writer.write(encode_frame(message_text.encode(self.encoding)))
            await self._writer.drain()
        except ConnectionError as exc:
            raise ConnectionClosedError(f"{self.address} closed the connection: {exc}") from exc
        while True:
            frame = await self._next_frame(deadline)
            reply = frame.decode(self.encoding, errors="replace")
            try:
                ack: Message | None = Message.parse(reply)
            except HL7ParseError:
                ack = None
            acked = ack.get("MSA-2") if ack else ""
            if acked and acked != control_id and acked in self._acknowledged:
                log.warning(
                    "ignoring duplicate ACK for earlier message control_id=%s peer=%s",
                    acked,
                    self.address,
                )
                continue
            break
        if control_id:
            self._acknowledged.append(control_id)
        return AckResult(
            sent_control_id=control_id,
            text=reply,
            message=ack,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def _next_frame(self, deadline: float) -> bytes:
        loop = asyncio.get_running_loop()
        while not self._pending:
            assert self._reader is not None
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise MllpTimeoutError(f"no ACK from {self.address} within {self.timeout:g}s")
            try:
                chunk = await asyncio.wait_for(self._reader.read(READ_CHUNK), remaining)
            except TimeoutError as exc:
                raise MllpTimeoutError(
                    f"no ACK from {self.address} within {self.timeout:g}s"
                ) from exc
            except ConnectionError as exc:
                raise ConnectionClosedError(
                    f"{self.address} closed the connection without an ACK: {exc}"
                ) from exc
            if not chunk:
                raise ConnectionClosedError(f"{self.address} closed the connection without an ACK")
            self._pending.extend(self._decoder.feed(chunk))
        return self._pending.popleft()


def split_messages(text: str) -> list[str]:
    """Split a file's contents into individual messages.

    Each message starts at an ``MSH`` segment. Blank lines, MLLP block
    characters and batch envelope segments (FHS/BHS/BTS/FTS) are ignored.
    """
    text = text.replace("\x0b", "").replace("\x1c", "")
    messages: list[list[str]] = []
    for number, line in enumerate(normalize_segments(text).split("\r"), start=1):
        if not line.strip() or line.startswith(BATCH_ENVELOPE):
            continue
        if line.startswith("MSH"):
            messages.append([line])
        elif not messages:
            raise HL7ParseError(f"segment {number} appears before any MSH segment: {line[:20]!r}")
        else:
            messages[-1].append(line)
    return ["\r".join(segments) + "\r" for segments in messages]
