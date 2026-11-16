"""Rule model and evaluation: which action answers a given inbound message."""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Protocol

from mllp_sim.ack import DEFAULT_ERROR_CODE, AckCode
from mllp_sim.hl7 import FieldPath, Message
from mllp_sim.templating import Template

# -- conditions ----------------------------------------------------------------------


class Condition(Protocol):
    """A predicate over one decoded field value (``""`` when the field is absent)."""

    def test(self, value: str) -> bool: ...

    def describe(self) -> str: ...


@dataclass(frozen=True, slots=True)
class Equals:
    expected: str

    def test(self, value: str) -> bool:
        return value == self.expected

    def describe(self) -> str:
        return f"== {self.expected!r}"


@dataclass(frozen=True, slots=True)
class OneOf:
    options: frozenset[str]

    def test(self, value: str) -> bool:
        return value in self.options

    def describe(self) -> str:
        return f"in {sorted(self.options)!r}"


@dataclass(frozen=True, slots=True)
class Matches:
    """``re.search`` semantics: anchor the pattern with ``^``/``$`` when needed."""

    pattern: re.Pattern[str]

    def test(self, value: str) -> bool:
        return self.pattern.search(value) is not None

    def describe(self) -> str:
        return f"~ /{self.pattern.pattern}/"


@dataclass(frozen=True, slots=True)
class Exists:
    present: bool

    def test(self, value: str) -> bool:
        return bool(value) is self.present

    def describe(self) -> str:
        return "exists" if self.present else "is empty"


@dataclass(frozen=True, slots=True)
class FieldMatcher:
    path: FieldPath
    condition: Condition

    def matches(self, message: Message) -> bool:
        return self.condition.test(message.get(self.path))


@dataclass(frozen=True, slots=True)
class MessageMatcher:
    """All configured criteria must hold. An empty matcher matches everything.

    ``types`` are glob patterns. A pattern with ``^`` is compared with
    ``CODE^TRIGGER`` (``ADT^A0*``); without it, with the message code only (``ADT``).
    """

    types: tuple[str, ...] = ()
    fields: tuple[FieldMatcher, ...] = ()

    def matches(self, message: Message) -> bool:
        if self.types and not any(_type_matches(p, message) for p in self.types):
            return False
        return all(f.matches(message) for f in self.fields)


def _type_matches(pattern: str, message: Message) -> bool:
    if "^" in pattern:
        return fnmatchcase(f"{message.message_code}^{message.trigger_event}", pattern)
    return fnmatchcase(message.message_code, pattern)


# -- actions -------------------------------------------------------------------------


class ReplyMode(StrEnum):
    """How the simulator answers once it has decided on an ACK."""

    ACK = "ack"  # send the ACK normally
    NONE = "none"  # send nothing; keep the connection open (sender times out)
    DROP = "drop"  # close the connection without replying
    DUPLICATE = "duplicate"  # send the same ACK twice
    GARBLED = "garbled"  # send the ACK without a valid MLLP envelope


@dataclass(frozen=True, slots=True)
class Delay:
    """A fixed delay, or a uniformly random one between ``min_ms`` and ``max_ms``."""

    min_ms: int = 0
    max_ms: int = 0

    def sample_seconds(self, rng: random.Random) -> float:
        if self.max_ms <= self.min_ms:
            return self.min_ms / 1000
        return rng.uniform(self.min_ms, self.max_ms) / 1000

    def __bool__(self) -> bool:
        return self.max_ms > 0 or self.min_ms > 0


@dataclass(frozen=True, slots=True)
class FollowUp:
    """A message to send to another endpoint after a rule fires."""

    host: str
    port: int
    template: Template
    delay: Delay = Delay()
    timeout_s: float = 10.0
    name: str = "follow-up"


@dataclass(frozen=True, slots=True)
class Action:
    ack: AckCode = AckCode.AA
    reply: ReplyMode = ReplyMode.ACK
    error: str | None = None
    error_code: str = DEFAULT_ERROR_CODE
    delay: Delay = Delay()
    followups: tuple[FollowUp, ...] = ()


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    matcher: MessageMatcher = field(default_factory=MessageMatcher)
    action: Action = field(default_factory=Action)
    probability: float = 1.0


# -- evaluation ----------------------------------------------------------------------

UNPARSEABLE_RULE = "<unparseable>"
DEFAULT_RULE = "<default>"
UNPARSEABLE_ACTION = Action(
    ack=AckCode.AR, error="Message could not be parsed as HL7 v2", error_code="100"
)


@dataclass(frozen=True, slots=True)
class Decision:
    rule: str
    action: Action


class RuleEngine:
    """First matching rule wins.

    A rule matches when its matcher accepts the message *and* its probability
    roll succeeds; otherwise evaluation continues with the next rule. When no
    rule matches, the default action applies. Messages that cannot be parsed
    are always rejected with AR, because no rule can be evaluated against them.
    """

    def __init__(
        self,
        rules: Sequence[Rule],
        default_action: Action | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.rules = tuple(rules)
        self.default_action = default_action or Action()
        self.rng = rng or random.Random()

    def evaluate(self, message: Message | None) -> Decision:
        if message is None:
            return Decision(UNPARSEABLE_RULE, UNPARSEABLE_ACTION)
        for rule in self.rules:
            if not rule.matcher.matches(message):
                continue
            if rule.probability >= 1.0 or self.rng.random() < rule.probability:
                return Decision(rule.name, rule.action)
        return Decision(DEFAULT_RULE, self.default_action)
