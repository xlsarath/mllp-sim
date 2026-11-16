from __future__ import annotations

from datetime import datetime

import pytest

from mllp_sim.hl7 import (
    ControlIdGenerator,
    FieldPath,
    FieldPathError,
    HL7ParseError,
    Message,
    Separators,
    hl7_timestamp,
    normalize_segments,
    parse_version,
)


def test_parses_header_values(adt_a01: str) -> None:
    msg = Message.parse(adt_a01)
    assert msg.message_type == "ADT^A01"
    assert msg.message_code == "ADT"
    assert msg.trigger_event == "A01"
    assert msg.control_id == "MSG00001"
    assert msg.version == "2.5.1"
    assert [s.name for s in msg] == ["MSH", "EVN", "PID", "PV1"]
    assert len(msg) == 4


@pytest.mark.parametrize("terminator", ["\r", "\n", "\r\n"])
def test_accepts_any_line_ending(adt_a01: str, terminator: str) -> None:
    msg = Message.parse(adt_a01.replace("\r", terminator))
    assert msg.get("PID-5.2") == "JANE"
    assert len(msg) == 4


def test_ignores_blank_lines_and_leading_bom(adt_a01: str) -> None:
    msg = Message.parse("﻿\n" + adt_a01.replace("\r", "\r\n\r\n"))
    assert len(msg) == 4


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("MSH-1", "|"),
        ("MSH-2", "^~\\&"),
        ("MSH-3", "SENDING_APP"),
        ("MSH-9", "ADT^A01^ADT_A01"),
        ("MSH-9.1", "ADT"),
        ("MSH-9.3", "ADT_A01"),
        ("MSH-12", "2.5.1"),
        ("PID-3", "MRN0001^^^SENDING_FAC^MR"),
        ("PID-3.1", "MRN0001"),
        ("PID-3.4", "SENDING_FAC"),
        ("PID-3[2].1", "ALT0001"),
        ("PID-3[2].5", "PI"),
        ("PID-3[3].1", ""),
        ("PID-5", "DOE^JANE^Q"),
        ("PID-5.3", "Q"),
        ("PID-5.9", ""),
        ("PID-99", ""),
        ("PV1-3.2", "101"),
        ("ZZZ-1", ""),
        ("PID[2]-3", ""),
    ],
)
def test_field_paths(adt_a01: str, path: str, expected: str) -> None:
    assert Message.parse(adt_a01).get(path) == expected


def test_msh_delimiter_fields_are_never_split(adt_a01: str) -> None:
    msg = Message.parse(adt_a01)
    assert msg.get("MSH-2.1") == "^~\\&"
    assert msg.get("MSH-2.2") == ""
    assert msg.get("MSH-1[2]") == ""


def test_segment_occurrence_and_subcomponents() -> None:
    msg = Message.parse(
        "MSH|^~\\&|A|B|C|D|20260101||ORU^R01|1|P|2.5\rOBX|1|NM|CODE1||10\rOBX|2|ST|CODE2||abc&def\r"
    )
    assert msg.get("OBX-5") == "10"
    assert msg.get("OBX[2]-5") == "abc&def"
    assert msg.get("OBX[2]-5.1.2") == "def"
    assert msg.get("OBX[3]-5") == ""
    assert len(msg.segments_named("OBX")) == 2


def test_custom_delimiters_are_read_from_msh() -> None:
    msg = Message.parse("MSH#*!@%#SEND#FAC#RECV#RFAC#20260101##ADT*A01#42#P#2.4\rPID#1##X*Y!Z*W")
    assert msg.separators == Separators("#", "*", "!", "@", "%")
    assert msg.message_type == "ADT^A01"
    assert msg.get("PID-3.2") == "Y"
    assert msg.get("PID-3[2].2") == "W"


def test_v27_truncation_character_is_tolerated() -> None:
    msg = Message.parse("MSH|^~\\&#|A|B|C|D|20260101||ADT^A01|1|P|2.7")
    assert msg.separators.encoding_characters == "^~\\&"
    assert msg.control_id == "1"


def test_decoded_and_raw_values() -> None:
    msg = Message.parse(
        "MSH|^~\\&|A|B|C|D|20260101||ORU^R01|1|P|2.5\r"
        "OBX|1|ST|C||a\\F\\b\\S\\c\\T\\d\\R\\e\\E\\f\\.br\\g\\X41\\h\\H\\i"
    )
    assert msg.get_raw("OBX-5").startswith("a\\F\\b")
    assert msg.get("OBX-5") == "a|b^c&d~e\\f\ngAh\\H\\i"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("", "empty"),
        ("PID|1||X", "MSH"),
        ("MSH|^~|A", "4 encoding characters"),
        ("MSH|^^\\&|A", "distinct"),
        ("MSHa^~\\&a", "letters"),
        ("MSH|^~\\&|A\rpid|1", "invalid name"),
    ],
)
def test_rejects_invalid_messages(text: str, error: str) -> None:
    with pytest.raises(HL7ParseError, match=error):
        Message.parse(text)


def test_round_trip_to_er7(adt_a01: str) -> None:
    msg = Message.parse(adt_a01.replace("\r", "\n"))
    assert msg.to_er7() == adt_a01
    assert Message.parse(msg.to_er7()).get("PID-3[2].1") == "ALT0001"


def test_escape_round_trip() -> None:
    seps = Separators()
    text = "a|b^c&d~e\\f"
    assert seps.unescape_text(seps.escape_text(text)) == text
    assert seps.escape_text("x^y") == "x\\S\\y"


def test_unterminated_or_unknown_escape_kept() -> None:
    seps = Separators()
    assert seps.unescape_text("50\\% off") == "50\\% off"
    assert seps.unescape_text("\\Z\\") == "\\Z\\"
    assert seps.unescape_text("\\XZZ\\") == "\\XZZ\\"


def test_translate_between_delimiter_sets() -> None:
    custom = Separators("#", "*", "!", "@", "%")
    assert custom.translate_to(Separators(), "A*B!C*D%E") == "A^B~C^D&E"
    assert Separators().translate_to(Separators(), "A^B") == "A^B"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("PID-3", FieldPath("PID", 3)),
        ("PID-3.1", FieldPath("PID", 3, component=1)),
        ("PID-3[2].4.1", FieldPath("PID", 3, repetition=2, component=4, subcomponent=1)),
        ("OBX[3]-5", FieldPath("OBX", 5, segment_index=3)),
        ("ZX1-1", FieldPath("ZX1", 1)),
        (" MSH-10 ", FieldPath("MSH", 10)),
    ],
)
def test_field_path_parsing(text: str, expected: FieldPath) -> None:
    assert FieldPath.parse(text) == expected


@pytest.mark.parametrize("text", ["PID", "PID-", "pid-3", "PID-0", "PID-3.0", "PID-3.1.2.3", "P-3"])
def test_field_path_errors(text: str) -> None:
    with pytest.raises(FieldPathError):
        FieldPath.parse(text)


@pytest.mark.parametrize("text", ["PID-3", "PID-3[2].4.1", "OBX[3]-5.1"])
def test_field_path_str_round_trip(text: str) -> None:
    assert str(FieldPath.parse(text)) == text


def test_helpers() -> None:
    assert normalize_segments("\r\nA\r\nB\n") == "A\rB"
    assert parse_version("2.5.1") == (2, 5, 1)
    assert parse_version("garbage") == (2, 5)
    stamp = hl7_timestamp(datetime(2026, 1, 2, 3, 4, 5).astimezone())
    assert stamp.startswith("20260102030405")
    assert len(stamp) == 19


def test_control_ids_are_unique_and_fit_msh10() -> None:
    gen = ControlIdGenerator()
    ids = [gen.next() for _ in range(1000)]
    assert len(set(ids)) == 1000
    assert all(len(i) == 20 and i.isdigit() for i in ids)
