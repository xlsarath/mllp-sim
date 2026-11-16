from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

ADT_A01 = (
    "MSH|^~\\&|SENDING_APP|SENDING_FAC|RECEIVING_APP|RECEIVING_FAC|20260101093000||"
    "ADT^A01^ADT_A01|MSG00001|P|2.5.1\r"
    "EVN|A01|20260101093000\r"
    "PID|1||MRN0001^^^SENDING_FAC^MR~ALT0001^^^OTHER^PI||DOE^JANE^Q||19800101|F\r"
    "PV1|1|I|WARD1^101^A\r"
)

ORM_O01 = (
    "MSH|^~\\&|ORDER_APP|CLINIC_A|ACME_LAB|ACME_LAB|20260101110000||"
    "ORM^O01^ORM_O01|ORD00001|P|2.5.1\r"
    "PID|1||MRN0001^^^CLINIC_A^MR||DOE^JANE||19800101|F\r"
    "ORC|NW|PLC0001\r"
    "OBR|1|PLC0001||2345-7^Glucose^LN\r"
)


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """``main()`` reconfigures the root logger onto a captured stream; undo that."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def adt_a01() -> str:
    return ADT_A01


@pytest.fixture
def orm_o01() -> str:
    return ORM_O01
