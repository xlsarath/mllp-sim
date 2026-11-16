from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from mllp_sim.hl7 import Message
from mllp_sim.templating import RenderContext, Template, TemplateError

TEMPLATE = (
    "MSH|^~\\&|ACME_LAB|ACME_LAB|{{MSH-3}}|{{MSH-4}}|{{ now }}||ORU^R01|{{ control_id }}|P|2.5.1\n"
    "PID|1||{{ PID-3 }}||{{ PID-5 }}\n"
    "OBR|1|{{ ORC-2 }}|{{ uuid }}|{{ OBR-4 }}\n"
    "NTE|1||ref {{ control_id }} missing=[{{ ZZZ-1 }}]\n"
)


def render(template: Template, inbound: str) -> Message:
    ids = iter(["NEW0001", "NEW0002"])
    context = RenderContext(
        inbound=Message.parse(inbound),
        new_control_id=lambda: next(ids),
        clock=lambda: datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    )
    text = template.render(context)
    assert "\n" not in text
    assert text.endswith("\r")
    return Message.parse(text)


def test_render_copies_fields_and_builtins(orm_o01: str) -> None:
    out = render(Template(TEMPLATE), orm_o01)
    assert out.get("MSH-5") == "ORDER_APP"
    assert out.get("MSH-6") == "CLINIC_A"
    assert out.get("MSH-7").startswith("20260101")
    assert out.control_id == "NEW0001"
    assert out.get("PID-3") == "MRN0001^^^CLINIC_A^MR"  # structure is preserved
    assert out.get("PID-5.2") == "JANE"
    assert out.get("OBR-2") == "PLC0001"
    assert out.get("OBR-4.1") == "2345-7"
    uuid.UUID(out.get("OBR-3"))
    # control_id is stable within one rendering; absent fields render empty.
    assert out.get("NTE-3") == "ref NEW0001 missing=[]"


def test_values_are_translated_to_template_delimiters() -> None:
    inbound = "MSH#*!@%#SEND#FAC#RECV#RFAC#20260101##ORM*O01#42#P#2.5\rPID#1##A*B*C"
    out = render(
        Template("MSH|^~\\&|X|Y|Z|W|{{now}}||ORU^R01|1|P|2.5\rPID|1||{{ PID-3 }}"), inbound
    )
    assert out.get("PID-3") == "A^B^C"
    assert out.get("PID-3.2") == "B"


def test_placeholders_are_listed() -> None:
    assert Template(TEMPLATE).placeholders[:3] == ("MSH-3", "MSH-4", "now")


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ("PID|1", "must start with an MSH"),
        ("MSH|^~|", "template MSH is invalid"),
        ("MSH|^~\\&|{{ today }}", "unknown placeholder"),
        ("MSH|^~\\&|{{ }}", "unknown placeholder"),
    ],
)
def test_invalid_templates(source: str, error: str) -> None:
    with pytest.raises(TemplateError, match=error):
        Template(source)
