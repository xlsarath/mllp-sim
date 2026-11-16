from __future__ import annotations

import pytest

from mllp_sim.framing import FrameDecoder, FrameTooLargeError, MLLPError, encode_frame

MSG_A = b"MSH|^~\\&|A|B|C|D|20260101||ADT^A01|1|P|2.5\r"
MSG_B = b"MSH|^~\\&|A|B|C|D|20260101||ADT^A08|2|P|2.5\r"


def test_encode_frame() -> None:
    assert encode_frame(b"abc") == b"\x0babc\x1c\x0d"


@pytest.mark.parametrize("bad", [b"a\x0bb", b"a\x1cb"])
def test_encode_rejects_block_characters(bad: bytes) -> None:
    with pytest.raises(MLLPError):
        encode_frame(bad)


def test_single_frame() -> None:
    decoder = FrameDecoder()
    assert decoder.feed(encode_frame(MSG_A)) == [MSG_A]
    assert not decoder.has_partial_frame
    assert decoder.stats.frames == 1


def test_frame_split_at_every_byte_boundary() -> None:
    data = encode_frame(MSG_A) + encode_frame(MSG_B)
    for cut in range(1, len(data)):
        decoder = FrameDecoder()
        frames = decoder.feed(data[:cut]) + decoder.feed(data[cut:])
        assert frames == [MSG_A, MSG_B], f"cut at {cut}"
        assert decoder.stats.missing_carriage_returns == 0


def test_byte_by_byte() -> None:
    decoder = FrameDecoder()
    frames: list[bytes] = []
    for byte in encode_frame(MSG_A) + encode_frame(MSG_B):
        frames += decoder.feed(bytes([byte]))
    assert frames == [MSG_A, MSG_B]


def test_multiple_frames_in_one_read() -> None:
    decoder = FrameDecoder()
    data = b"".join(encode_frame(m) for m in (MSG_A, MSG_B, MSG_A))
    assert decoder.feed(data) == [MSG_A, MSG_B, MSG_A]


def test_junk_between_frames_is_discarded_and_counted() -> None:
    decoder = FrameDecoder()
    data = b"noise" + encode_frame(MSG_A) + b"\n" + encode_frame(MSG_B) + b"tail"
    assert decoder.feed(data) == [MSG_A, MSG_B]
    assert decoder.stats.discarded_bytes == len(b"noise") + len(b"\n") + len(b"tail")
    assert decoder.stats.missing_carriage_returns == 0


def test_missing_carriage_return_still_ends_frame() -> None:
    decoder = FrameDecoder()
    assert decoder.feed(b"\x0b" + MSG_A + b"\x1c") == [MSG_A]
    assert decoder.feed(b"\x0b" + MSG_B + b"\x1c\x0d") == [MSG_B]
    assert decoder.stats.missing_carriage_returns == 1


def test_carriage_return_arriving_in_next_read_is_consumed() -> None:
    decoder = FrameDecoder()
    assert decoder.feed(b"\x0b" + MSG_A + b"\x1c") == [MSG_A]
    assert decoder.feed(b"\x0d") == []
    assert decoder.stats.discarded_bytes == 0
    assert decoder.stats.missing_carriage_returns == 0


def test_restart_abandons_partial_frame() -> None:
    decoder = FrameDecoder()
    assert decoder.feed(b"\x0bMSH|partial") == []
    assert decoder.has_partial_frame
    assert decoder.feed(encode_frame(MSG_A)) == [MSG_A]
    assert decoder.stats.abandoned_frames == 1


def test_restart_within_one_read() -> None:
    decoder = FrameDecoder()
    assert decoder.feed(b"\x0bpartial\x0b" + MSG_A + b"\x1c\x0d") == [MSG_A]
    assert decoder.stats.abandoned_frames == 1


def test_empty_frame() -> None:
    assert FrameDecoder().feed(b"\x0b\x1c\x0d") == [b""]


def test_oversized_unterminated_frame_raises() -> None:
    decoder = FrameDecoder(max_frame_bytes=10)
    decoder.feed(b"\x0b" + b"x" * 10)
    with pytest.raises(FrameTooLargeError):
        decoder.feed(b"x")


def test_oversized_complete_frame_raises() -> None:
    decoder = FrameDecoder(max_frame_bytes=10)
    with pytest.raises(FrameTooLargeError):
        decoder.feed(encode_frame(b"y" * 11))


def test_invalid_limit() -> None:
    with pytest.raises(ValueError, match="positive"):
        FrameDecoder(0)
