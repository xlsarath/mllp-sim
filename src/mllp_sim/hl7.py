"""A small, dependency-free HL7 v2 (ER7, "pipe and hat") parser.

Only what a test simulator needs, done carefully:

* delimiters are read from the MSH segment, never assumed;
* segment terminators may be ``\\r`` (the standard), ``\\n`` or ``\\r\\n``;
* values are addressed with Terser-style paths such as ``PID-3.1`` or ``OBX[2]-5``;
* the standard escape sequences are decoded for matching and re-encoded for output.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

SEGMENT_TERMINATOR = "\r"

_SEGMENT_NAME = re.compile(r"[A-Z][A-Z0-9]{2}")


class HL7ParseError(ValueError):
    """Raised when text cannot be interpreted as an HL7 v2 message."""


class FieldPathError(ValueError):
    """Raised when a field path such as ``PID-3.1`` is malformed."""


@dataclass(frozen=True, slots=True)
class Separators:
    """The five HL7 v2 delimiters declared in MSH-1 and MSH-2."""

    field: str = "|"
    component: str = "^"
    repetition: str = "~"
    escape: str = "\\"
    subcomponent: str = "&"

    @property
    def encoding_characters(self) -> str:
        """The value of MSH-2."""
        return f"{self.component}{self.repetition}{self.escape}{self.subcomponent}"

    @classmethod
    def from_msh(cls, msh: str) -> Separators:
        """Read the delimiters from the raw text of an MSH segment."""
        if not msh.startswith("MSH") or len(msh) < 4:
            raise HL7ParseError("message does not start with an MSH segment")
        field = msh[3]
        end = msh.find(field, 4)
        encoding = msh[4:] if end == -1 else msh[4:end]
        # MSH-2 holds four characters; HL7 v2.7+ may append a fifth (truncation).
        if len(encoding) not in (4, 5):
            raise HL7ParseError(
                f"MSH-2 must contain 4 encoding characters, found {len(encoding)}: {encoding!r}"
            )
        separators = cls(field, encoding[0], encoding[1], encoding[2], encoding[3])
        delimiters = (field, *encoding[:4])
        if len(set(delimiters)) != len(delimiters):
            raise HL7ParseError(f"delimiters must be distinct, got {''.join(delimiters)!r}")
        if any(ch.isalnum() or ch.isspace() for ch in delimiters):
            raise HL7ParseError("delimiters must not be letters, digits or whitespace")
        return separators

    def escape_text(self, value: str) -> str:
        """Encode delimiter characters in ``value`` as HL7 escape sequences."""
        e = self.escape
        replacements = {
            self.escape: f"{e}E{e}",
            self.field: f"{e}F{e}",
            self.component: f"{e}S{e}",
            self.subcomponent: f"{e}T{e}",
            self.repetition: f"{e}R{e}",
        }
        return "".join(replacements.get(ch, ch) for ch in value)

    def unescape_text(self, value: str) -> str:
        """Decode the standard HL7 escape sequences in ``value``.

        Supports ``\\F\\ \\S\\ \\T\\ \\R\\ \\E\\``, ``\\.br\\`` (line break) and
        ``\\Xhh..\\`` (hex bytes). Formatting sequences that have no plain-text
        equivalent are kept verbatim, as is an unterminated escape.
        """
        e = self.escape
        if e not in value:
            return value
        simple = {
            "F": self.field,
            "S": self.component,
            "T": self.subcomponent,
            "R": self.repetition,
            "E": self.escape,
            ".br": "\n",
        }
        out: list[str] = []
        i = 0
        while i < len(value):
            ch = value[i]
            end = value.find(e, i + 1) if ch == e else -1
            if end == -1:
                out.append(ch)
                i += 1
                continue
            code = value[i + 1 : end]
            if code in simple:
                out.append(simple[code])
            elif code.startswith("X") and _is_hex(code[1:]):
                out.append(bytes.fromhex(code[1:]).decode("utf-8", errors="replace"))
            else:
                out.append(value[i : end + 1])
            i = end + 1
        return "".join(out)

    def translate_to(self, other: Separators, raw: str) -> str:
        """Re-express an encoded value that uses these delimiters with ``other``'s."""
        if self == other:
            return raw
        table = str.maketrans(
            {
                self.field: other.field,
                self.component: other.component,
                self.repetition: other.repetition,
                self.escape: other.escape,
                self.subcomponent: other.subcomponent,
            }
        )
        return raw.translate(table)


def _is_hex(text: str) -> bool:
    return bool(text) and len(text) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in text)


@dataclass(frozen=True, slots=True)
class FieldPath:
    """A Terser-style location inside a message, all indexes 1-based.

    ``PID-3`` (first repetition of field 3), ``PID-3.1`` (component 1),
    ``PID-3[2].1`` (second repetition), ``OBX[2]-5`` (second OBX segment) and
    ``PID-3.4.2`` (sub-component 2 of component 4).
    """

    segment: str
    field: int
    segment_index: int = 1
    repetition: int = 1
    component: int | None = None
    subcomponent: int | None = None

    _PATTERN = re.compile(
        r"(?P<seg>[A-Z][A-Z0-9]{2})(?:\[(?P<segidx>\d+)\])?"
        r"-(?P<field>\d+)(?:\[(?P<rep>\d+)\])?"
        r"(?:\.(?P<comp>\d+)(?:\.(?P<sub>\d+))?)?"
    )

    @classmethod
    def parse(cls, text: str) -> FieldPath:
        """Parse ``text``; raises :class:`FieldPathError` with a helpful message."""
        return _parse_field_path(text.strip())

    def __str__(self) -> str:
        out = self.segment
        if self.segment_index != 1:
            out += f"[{self.segment_index}]"
        out += f"-{self.field}"
        if self.repetition != 1:
            out += f"[{self.repetition}]"
        if self.component is not None:
            out += f".{self.component}"
        if self.subcomponent is not None:
            out += f".{self.subcomponent}"
        return out


@lru_cache(maxsize=1024)
def _parse_field_path(text: str) -> FieldPath:
    match = FieldPath._PATTERN.fullmatch(text)
    if match is None:
        raise FieldPathError(
            f"invalid field path {text!r} (expected e.g. PID-3, PID-3.1, PID-3[2].1, OBX[2]-5)"
        )

    def num(group: str) -> int | None:
        raw = match.group(group)
        if raw is None:
            return None
        value = int(raw)
        if value < 1:
            raise FieldPathError(f"invalid field path {text!r}: indexes are 1-based")
        return value

    field = num("field")
    assert field is not None  # the regex makes the field mandatory
    return FieldPath(
        segment=match.group("seg"),
        field=field,
        segment_index=num("segidx") or 1,
        repetition=num("rep") or 1,
        component=num("comp"),
        subcomponent=num("sub"),
    )


@dataclass(frozen=True, slots=True)
class Segment:
    """One segment. ``fields[n]`` is field *n*; ``fields[0]`` is the segment name.

    For MSH the list is normalised so that ``fields[1]`` is the field separator
    and ``fields[2]`` the encoding characters, matching HL7 numbering.
    """

    name: str
    fields: tuple[str, ...]

    def field(self, number: int) -> str:
        return self.fields[number] if 0 <= number < len(self.fields) else ""


class Message:
    """A parsed HL7 v2 message."""

    __slots__ = ("_by_name", "segments", "separators")

    def __init__(self, segments: Sequence[Segment], separators: Separators) -> None:
        if not segments or segments[0].name != "MSH":
            raise HL7ParseError("message does not start with an MSH segment")
        self.segments: tuple[Segment, ...] = tuple(segments)
        self.separators = separators
        by_name: dict[str, list[Segment]] = {}
        for segment in self.segments:
            by_name.setdefault(segment.name, []).append(segment)
        self._by_name = by_name

    @classmethod
    def parse(cls, text: str) -> Message:
        """Parse ER7 text. Accepts ``\\r``, ``\\n`` or ``\\r\\n`` segment terminators."""
        lines = [
            line for line in normalize_segments(text).split(SEGMENT_TERMINATOR) if line.strip()
        ]
        if not lines:
            raise HL7ParseError("message is empty")
        separators = Separators.from_msh(lines[0])
        segments: list[Segment] = []
        for number, line in enumerate(lines, start=1):
            parts = line.split(separators.field)
            name = parts[0]
            if not _SEGMENT_NAME.fullmatch(name):
                raise HL7ParseError(f"segment {number} has an invalid name {name!r}")
            if name == "MSH":
                # MSH-1 is the separator itself, so field n sits at parts[n - 1].
                parts = [name, separators.field, *parts[1:]]
            segments.append(Segment(name, tuple(parts)))
        return cls(segments, separators)

    # -- navigation -----------------------------------------------------------------

    def segments_named(self, name: str) -> list[Segment]:
        return list(self._by_name.get(name, ()))

    def get_raw(self, path: FieldPath | str) -> str:
        """Return the still-encoded value at ``path``, or ``""`` when absent."""
        if isinstance(path, str):
            path = FieldPath.parse(path)
        candidates = self._by_name.get(path.segment, ())
        if path.segment_index > len(candidates):
            return ""
        segment = candidates[path.segment_index - 1]
        value = segment.field(path.field)
        if segment.name == "MSH" and path.field in (1, 2):
            # Delimiter fields are opaque: never split them.
            first = path.repetition == 1 and path.component in (None, 1)
            return value if first and path.subcomponent in (None, 1) else ""
        seps = self.separators
        value = _nth(value.split(seps.repetition), path.repetition)
        if path.component is None:
            return value
        value = _nth(value.split(seps.component), path.component)
        if path.subcomponent is None:
            return value
        return _nth(value.split(seps.subcomponent), path.subcomponent)

    def get(self, path: FieldPath | str) -> str:
        """Return the decoded (unescaped) value at ``path``, or ``""`` when absent."""
        return self.separators.unescape_text(self.get_raw(path))

    # -- common header values -------------------------------------------------------

    @property
    def message_code(self) -> str:
        return self.get("MSH-9.1")

    @property
    def trigger_event(self) -> str:
        return self.get("MSH-9.2")

    @property
    def message_type(self) -> str:
        """``CODE^TRIGGER`` (e.g. ``ORM^O01``), always with ``^`` for readability."""
        code, trigger = self.message_code, self.trigger_event
        return f"{code}^{trigger}" if trigger else code

    @property
    def control_id(self) -> str:
        return self.get("MSH-10")

    @property
    def version(self) -> str:
        return self.get("MSH-12.1")

    # -- output ---------------------------------------------------------------------

    def to_er7(self) -> str:
        """Serialise with ``\\r`` segment terminators (including the last segment)."""
        return "".join(
            _render_segment(s, self.separators) + SEGMENT_TERMINATOR for s in self.segments
        )

    def __iter__(self) -> Iterator[Segment]:
        return iter(self.segments)

    def __len__(self) -> int:
        return len(self.segments)

    def __repr__(self) -> str:
        return (
            f"Message(type={self.message_type!r}, control_id={self.control_id!r}, "
            f"segments={len(self.segments)})"
        )


def _nth(parts: list[str], index: int) -> str:
    return parts[index - 1] if index <= len(parts) else ""


def _render_segment(segment: Segment, separators: Separators) -> str:
    if segment.name == "MSH":
        return "MSH" + separators.field + separators.field.join(segment.fields[2:])
    return separators.field.join(segment.fields)


def normalize_segments(text: str) -> str:
    """Convert ``\\r\\n`` and ``\\n`` segment terminators to ``\\r`` and trim edges."""
    text = text.replace("\r\n", "\r").replace("\n", "\r")
    return text.strip("\r﻿ \t")


def parse_version(version: str) -> tuple[int, ...]:
    """``"2.5.1"`` -> ``(2, 5, 1)``. Unparseable versions are treated as 2.5."""
    try:
        parts = tuple(int(p) for p in version.strip().split("."))
    except ValueError:
        return (2, 5)
    return parts if parts else (2, 5)


def hl7_timestamp(moment: datetime | None = None) -> str:
    """Format a DTM/TS value with second precision and UTC offset, e.g. ``20260101093000+0000``."""
    moment = (moment or datetime.now()).astimezone()
    return moment.strftime("%Y%m%d%H%M%S%z")


class ControlIdGenerator:
    """Generates unique, sortable message control ids that fit MSH-10 (20 chars).

    Format: ``YYYYMMDDHHMMSS`` followed by a 6-digit rolling sequence.
    """

    def __init__(self, clock: type[datetime] = datetime) -> None:
        self._clock = clock
        self._sequence = itertools.count(1)

    def next(self) -> str:
        stamp = self._clock.now().strftime("%Y%m%d%H%M%S")
        return f"{stamp}{next(self._sequence) % 1_000_000:06d}"
