from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mllp_sim.ack import AckCode, build_ack
from mllp_sim.hl7 import Message

WHEN = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def ack_for(text: str, code: AckCode = AckCode.AA, **kwargs: object) -> Message:
    inbound = Message.parse(text)
    return Message.parse(build_ack(inbound, code, control_id="ACK0001", timestamp=WHEN, **kwargs))


def test_accept_ack_mirrors_header(adt_a01: str) -> None:
    ack = ack_for(adt_a01)
    assert ack.get("MSH-3") == "RECEIVING_APP"
    assert ack.get("MSH-4") == "RECEIVING_FAC"
    assert ack.get("MSH-5") == "SENDING_APP"
    assert ack.get("MSH-6") == "SENDING_FAC"
    assert ack.get("MSH-9") == "ACK^A01^ACK"
    assert ack.control_id == "ACK0001"
    assert ack.get("MSH-11") == "P"
    assert ack.get("MSH-12") == "2.5.1"
    assert ack.get("MSA-1") == "AA"
    assert ack.get("MSA-2") == "MSG00001"
    assert ack.get("MSA-3") == ""
    assert ack.segments_named("ERR") == []
    assert ack.get("MSH-7").startswith("20260101")


def test_raw_text_layout(adt_a01: str) -> None:
    text = build_ack(Message.parse(adt_a01), AckCode.AA, control_id="X1", timestamp=WHEN)
    lines = text.split("\r")
    assert lines[0].startswith("MSH|^~\\&|RECEIVING_APP|RECEIVING_FAC|SENDING_APP|SENDING_FAC|")
    assert lines[0].endswith("||ACK^A01^ACK|X1|P|2.5.1")
    assert lines[1] == "MSA|AA|MSG00001"
    assert text.endswith("\r")


def test_error_ack_has_err_segment_v25(adt_a01: str) -> None:
    ack = ack_for(adt_a01, AckCode.AE, error_text="Patient not found", error_code="204")
    assert ack.get("MSA-1") == "AE"
    assert ack.get("MSA-3") == "Patient not found"
    assert ack.get("ERR-3.1") == "204"
    assert ack.get("ERR-3.2") == "Unknown key identifier"
    assert ack.get("ERR-3.3") == "HL70357"
    assert ack.get("ERR-4") == "E"
    assert ack.get("ERR-8") == "Patient not found"


def test_error_ack_uses_eld_before_v25() -> None:
    inbound = "MSH|^~\\&|A|B|C|D|20260101||ADT^A01|42|P|2.3\r"
    ack = ack_for(inbound, AckCode.AR, error_text="nope", error_code="200")
    assert ack.get("MSH-9") == "ACK^A01"  # message structure component is 2.3.1+
    assert ack.get("ERR-1.4.1") == "200"
    assert ack.get("ERR-1.4.3") == "HL70357"
    assert ack.get("MSA-3") == "nope"


def test_error_text_is_escaped(adt_a01: str) -> None:
    ack = ack_for(adt_a01, AckCode.AE, error_text="bad | value ^ here")
    assert ack.get("MSA-3") == "bad | value ^ here"
    assert "\\F\\" in ack.get_raw("MSA-3")


def test_error_text_ignored_on_positive_ack(adt_a01: str) -> None:
    ack = ack_for(adt_a01, AckCode.AA, error_text="ignored")
    assert ack.get("MSA-3") == ""
    assert ack.segments_named("ERR") == []


def test_custom_delimiters_are_reused() -> None:
    inbound = "MSH#*!@%#SEND#FAC#RECV#RFAC#20260101##ADT*A01#42#P#2.5\r"
    text = build_ack(Message.parse(inbound), AckCode.AA, control_id="9", timestamp=WHEN)
    assert text.startswith("MSH#*!@%#RECV#RFAC#SEND#FAC#")
    assert "ACK*A01*ACK" in text
    assert "MSA#AA#42" in text


def test_charset_is_echoed() -> None:
    inbound = "MSH|^~\\&|A|B|C|D|20260101||ADT^A01|42|P|2.5||||||UNICODE UTF-8\r"
    ack = ack_for(inbound)
    assert ack.get("MSH-18") == "UNICODE UTF-8"


def test_empty_receiving_application_falls_back() -> None:
    inbound = "MSH|^~\\&|A|B|||20260101||ADT^A01|42|P|2.5\r"
    ack = ack_for(inbound, application="SIM")
    assert ack.get("MSH-3") == "SIM"


def test_no_trigger_event() -> None:
    ack = ack_for("MSH|^~\\&|A|B|C|D|20260101||QRY|42|P|2.5\r")
    assert ack.get("MSH-9") == "ACK"


def test_ack_for_unparseable_message() -> None:
    text = build_ack(None, AckCode.AR, control_id="1", error_text="garbage", error_code="100")
    ack = Message.parse(text)
    assert ack.get("MSA-1") == "AR"
    assert ack.get("MSA-2") == ""
    assert ack.get("MSH-3") == "MLLP_SIM"
    assert ack.get("ERR-3.1") == "100"


@pytest.mark.parametrize(
    ("code", "positive"),
    [(AckCode.AA, True), (AckCode.CA, True), (AckCode.AE, False), (AckCode.CR, False)],
)
def test_ack_code_polarity(code: AckCode, positive: bool) -> None:
    assert code.is_positive is positive
