"""``{{ placeholder }}`` templates for outbound (follow-up) messages.

Placeholders:

* any field path (``{{ PID-3.1 }}``, ``{{ OBR-4 }}``) - copied from the inbound
  message, re-encoded for the template's own delimiters;
* ``{{ now }}`` - current timestamp in HL7 format;
* ``{{ uuid }}`` - a random UUID;
* ``{{ control_id }}`` - a fresh message control id (same value everywhere
  it appears in one rendering, so it can be used in MSH-10 and elsewhere).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from mllp_sim.hl7 import (
    FieldPath,
    FieldPathError,
    HL7ParseError,
    Message,
    Separators,
    hl7_timestamp,
    normalize_segments,
)

_PLACEHOLDER = re.compile(r"\{\{\s*(?P<name>[^{}]*?)\s*\}\}")
BUILTINS = ("now", "uuid", "control_id")


class TemplateError(ValueError):
    """The template text or one of its placeholders is invalid."""


@dataclass(frozen=True, slots=True)
class RenderContext:
    """Everything a template can draw values from."""

    inbound: Message
    new_control_id: Callable[[], str]
    clock: Callable[[], datetime] = datetime.now


class Template:
    """A pre-validated message template; placeholders are checked at load time."""

    def __init__(self, source: str) -> None:
        self.source = normalize_segments(source)
        if not self.source.startswith("MSH"):
            raise TemplateError("template must start with an MSH segment")
        try:
            self.separators = Separators.from_msh(self.source.split("\r", 1)[0])
        except HL7ParseError as exc:
            raise TemplateError(f"template MSH is invalid: {exc}") from exc
        self.placeholders: tuple[str, ...] = tuple(
            m.group("name") for m in _PLACEHOLDER.finditer(self.source)
        )
        for name in self.placeholders:
            if name in BUILTINS:
                continue
            try:
                FieldPath.parse(name)
            except FieldPathError as exc:
                raise TemplateError(
                    f"unknown placeholder {{{{ {name} }}}}: use a field path such as PID-3.1 "
                    f"or one of {', '.join(BUILTINS)}"
                ) from exc

    def render(self, context: RenderContext) -> str:
        """Render to ER7 text with ``\\r`` segment terminators."""
        control_id: str | None = None
        inbound = context.inbound

        def substitute(match: re.Match[str]) -> str:
            nonlocal control_id
            name = match.group("name")
            if name == "now":
                return hl7_timestamp(context.clock())
            if name == "uuid":
                return str(uuid.uuid4())
            if name == "control_id":
                if control_id is None:
                    control_id = context.new_control_id()
                return control_id
            raw = inbound.get_raw(FieldPath.parse(name))
            return inbound.separators.translate_to(self.separators, raw)

        rendered = _PLACEHOLDER.sub(substitute, self.source)
        return normalize_segments(rendered) + "\r"
