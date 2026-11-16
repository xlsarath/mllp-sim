"""MLLP (Minimal Lower Layer Protocol) framing.

A frame is ``<VT> payload <FS><CR>`` where VT = 0x0B, FS = 0x1C and CR = 0x0D.
TCP is a byte stream, so a frame may arrive split across many reads and one
read may carry several frames; :class:`FrameDecoder` handles both.
"""

from __future__ import annotations

from dataclasses import dataclass

START_BLOCK = b"\x0b"
END_BLOCK = b"\x1c"
CARRIAGE_RETURN = b"\x0d"
TRAILER = END_BLOCK + CARRIAGE_RETURN

DEFAULT_MAX_FRAME_BYTES = 16 * 1024 * 1024


class MLLPError(Exception):
    """Base class for MLLP transport problems."""


class FrameTooLargeError(MLLPError):
    """A frame exceeded the configured size limit (protects memory)."""


def encode_frame(payload: bytes) -> bytes:
    """Wrap ``payload`` in an MLLP envelope."""
    if START_BLOCK in payload or END_BLOCK in payload:
        raise MLLPError("payload must not contain MLLP block characters (0x0B or 0x1C)")
    return START_BLOCK + payload + TRAILER


@dataclass(slots=True)
class DecoderStats:
    """Protocol deviations seen by a decoder; useful for diagnosing peers."""

    frames: int = 0
    discarded_bytes: int = 0
    abandoned_frames: int = 0
    missing_carriage_returns: int = 0


class FrameDecoder:
    """Incremental MLLP decoder.

    Feed it whatever ``read()`` returned; it yields complete payloads.

    Tolerances (each one counted in :attr:`stats`):

    * bytes outside a frame (e.g. a stray newline between frames) are discarded;
    * a new start block before the end of the current frame abandons the partial
      frame, since the sender evidently restarted;
    * an end block not followed by CR still ends the frame, so a sloppy peer
      does not cause a hang.
    """

    def __init__(self, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> None:
        if max_frame_bytes < 1:
            raise ValueError("max_frame_bytes must be positive")
        self.max_frame_bytes = max_frame_bytes
        self.stats = DecoderStats()
        self._buffer = bytearray()
        self._expect_cr = False

    @property
    def has_partial_frame(self) -> bool:
        """True while bytes of an unfinished frame are buffered."""
        return bool(self._buffer)

    def feed(self, data: bytes) -> list[bytes]:
        """Consume ``data`` and return every frame it completed, in order."""
        buf = self._buffer
        buf += data
        frames: list[bytes] = []
        while buf:
            if self._expect_cr:
                self._expect_cr = False
                if buf[0] == CARRIAGE_RETURN[0]:
                    del buf[0]
                    continue
                self.stats.missing_carriage_returns += 1

            start = buf.find(START_BLOCK)
            if start == -1:
                self.stats.discarded_bytes += len(buf)
                buf.clear()
                break
            if start:
                self.stats.discarded_bytes += start
                del buf[:start]

            end = buf.find(END_BLOCK, 1)
            restart = buf.find(START_BLOCK, 1, end if end != -1 else len(buf))
            if restart != -1:
                self.stats.abandoned_frames += 1
                del buf[:restart]
                continue
            if end == -1:
                if len(buf) - 1 > self.max_frame_bytes:
                    size = len(buf) - 1
                    buf.clear()
                    raise FrameTooLargeError(
                        f"frame exceeds {self.max_frame_bytes} bytes (at least {size} buffered)"
                    )
                break

            payload = bytes(buf[1:end])
            del buf[: end + 1]
            if len(payload) > self.max_frame_bytes:
                raise FrameTooLargeError(
                    f"frame of {len(payload)} bytes exceeds {self.max_frame_bytes} bytes"
                )
            self._expect_cr = True
            self.stats.frames += 1
            frames.append(payload)
        return frames
