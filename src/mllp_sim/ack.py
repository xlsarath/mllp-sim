"""Build HL7 v2 acknowledgements (ACK messages) from an inbound message."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from mllp_sim.hl7 import SEGMENT_TERMINATOR, Message, Separators, hl7_timestamp, parse_version

DEFAULT_APPLICATION = "MLLP_SIM"
DEFAULT_VERSION = "2.5"

# HL7 table 0357 (message error condition codes) - the entries a simulator needs.
ERROR_CODES: dict[str, str] = {
    "0": "Message accepted",
    "100": "Segment sequence error",
    "101": "Required field missing",
    "102": "Data type error",
    "103": "Table value not found",
    "200": "Unsupported message type",
    "201": "Unsupported event code",
    "202": "Unsupported processing id",
    "203": "Unsupported version id",
    "204": "Unknown key identifier",
    "205": "Duplicate key identifier",
    "206": "Application record locked",
    "207": "Application internal error",
}
DEFAULT_ERROR_CODE = "207"


class AckCode(StrEnum):
    """MSA-1 acknowledgment codes (HL7 table 0008)."""

    AA = "AA"  # application accept
    AE = "AE"  # application error
    AR = "AR"  # application reject
    CA = "CA"  # commit accept (enhanced mode)
    CE = "CE"  # commit error
    CR = "CR"  # commit reject

    @property
    def is_positive(self) -> bool:
        return self in (AckCode.AA, AckCode.CA)


def build_ack(
    inbound: Message | None,
    code: AckCode,
    *,
    control_id: str,
    error_text: str | None = None,
    error_code: str = DEFAULT_ERROR_CODE,
    timestamp: datetime | None = None,
    application: str = DEFAULT_APPLICATION,
) -> str:
    """Return the ER7 text of an ACK answering ``inbound``.

    The header mirrors the inbound one: sending and receiving application and
    facility are swapped, MSH-11 (processing id) and MSH-12 (version) are echoed,
    and MSA-2 carries the inbound MSH-10. ``inbound`` may be ``None`` when the
    message could not be parsed; the ACK then uses default delimiters,
    ``application`` as the sender and an empty MSA-2.

    When ``error_text`` is given with a negative code, it is written to MSA-3 (for
    older receivers) and to an ERR segment in the layout of the inbound version.
    """
    seps = inbound.separators if inbound else Separators()
    version_text = inbound.get_raw("MSH-12") if inbound else DEFAULT_VERSION
    version = parse_version(inbound.version if inbound else DEFAULT_VERSION)

    if inbound is not None:
        sending_app = inbound.get_raw("MSH-5") or application
        sending_facility = inbound.get_raw("MSH-6")
        receiving_app = inbound.get_raw("MSH-3")
        receiving_facility = inbound.get_raw("MSH-4")
        processing_id = inbound.get_raw("MSH-11") or "P"
        charset = inbound.get_raw("MSH-18")
        trigger = inbound.get_raw("MSH-9.2")
        acked_control_id = inbound.get_raw("MSH-10")
    else:
        sending_app, sending_facility = application, ""
        receiving_app = receiving_facility = charset = trigger = acked_control_id = ""
        processing_id = "P"

    message_type = "ACK"
    if trigger:
        message_type += f"{seps.component}{trigger}"
        if version >= (2, 3, 1):
            message_type += f"{seps.component}ACK"

    msh = [
        sending_app,
        sending_facility,
        receiving_app,
        receiving_facility,
        hl7_timestamp(timestamp),
        "",
        message_type,
        seps.escape_text(control_id),
        processing_id,
        version_text or DEFAULT_VERSION,
    ]
    if charset:
        msh += ["", "", "", "", "", charset]  # MSH-13..17 empty, MSH-18 echoed

    has_error = bool(error_text) and not code.is_positive
    msa = ["MSA", code.value, acked_control_id]
    if has_error:
        msa.append(seps.escape_text(error_text or ""))

    segments = [
        "MSH" + seps.field + seps.encoding_characters + seps.field + seps.field.join(msh),
        seps.field.join(msa),
    ]
    if has_error:
        segments.append(_err_segment(seps, version, error_code, error_text or ""))
    return SEGMENT_TERMINATOR.join(segments) + SEGMENT_TERMINATOR


def _err_segment(seps: Separators, version: tuple[int, ...], code: str, text: str) -> str:
    description = ERROR_CODES.get(code, "")
    escaped_text = seps.escape_text(text)
    if version >= (2, 5):
        # ERR-3 HL7 error code (CWE), ERR-4 severity, ERR-8 user message.
        error = seps.component.join([code, description, "HL70357"])
        fields = ["ERR", "", "", error, "E", "", "", "", escaped_text]
    else:
        # ERR-1 error code and location (ELD); its 4th component is a CE, so the
        # coded value goes in sub-components.
        coded = seps.subcomponent.join([code, description, "HL70357"])
        fields = ["ERR", seps.component.join(["", "", "", coded])]
    return seps.field.join(fields)
