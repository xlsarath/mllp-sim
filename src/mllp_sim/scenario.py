"""Load and validate scenario files.

A scenario is a YAML document describing how the simulator listens and how it
answers. Validation is strict: unknown keys are rejected (they are almost always
typos) and every error names its location, e.g. ``rules[2].action.ack``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from mllp_sim.ack import DEFAULT_APPLICATION, DEFAULT_ERROR_CODE, AckCode
from mllp_sim.framing import DEFAULT_MAX_FRAME_BYTES
from mllp_sim.hl7 import FieldPath, FieldPathError
from mllp_sim.rules import (
    Action,
    Condition,
    Delay,
    Equals,
    Exists,
    FieldMatcher,
    FollowUp,
    Matches,
    MessageMatcher,
    OneOf,
    ReplyMode,
    Rule,
)
from mllp_sim.templating import Template, TemplateError

DEFAULT_PORT = 2575
DEFAULT_HOST = "127.0.0.1"
DEFAULT_HISTORY = 500


class ScenarioError(ValueError):
    """The scenario file is invalid. The message names the offending location."""


@dataclass(frozen=True, slots=True)
class ServerSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    http_port: int | None = None
    encoding: str = "utf-8"
    max_message_bytes: int = DEFAULT_MAX_FRAME_BYTES
    history: int = DEFAULT_HISTORY
    application: str = DEFAULT_APPLICATION


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str = "default"
    description: str = ""
    server: ServerSettings = field(default_factory=ServerSettings)
    rules: tuple[Rule, ...] = ()
    default_action: Action = field(default_factory=Action)
    seed: int | None = None
    source: Path | None = None

    def with_server(self, **overrides: Any) -> Scenario:
        """Return a copy with server settings overridden (``None`` values are ignored)."""
        changes = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, server=replace(self.server, **changes)) if changes else self


def load_scenario(path: str | Path) -> Scenario:
    """Read and validate a scenario file. Relative template files resolve next to it."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioError(f"cannot read scenario {path}: {exc.strerror or exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: invalid YAML: {exc}") from exc
    scenario = parse_scenario(data if data is not None else {}, base_dir=path.parent)
    return replace(scenario, source=path)


def parse_scenario(data: Any, *, base_dir: Path | None = None) -> Scenario:
    """Validate an already-decoded scenario document."""
    base_dir = base_dir or Path.cwd()
    top = _mapping(data, "scenario", {"name", "description", "seed", "server", "default", "rules"})
    seed = top.get("seed")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
        raise ScenarioError("seed: must be an integer")

    raw_rules = _optional(top, "rules", [])
    if not isinstance(raw_rules, list):
        raise ScenarioError("rules: must be a list")
    rules = tuple(_rule(r, f"rules[{i}]", i, base_dir) for i, r in enumerate(raw_rules))
    seen: set[str] = set()
    for rule in rules:
        if rule.name in seen:
            raise ScenarioError(f"rules: duplicate rule name {rule.name!r}")
        seen.add(rule.name)

    return Scenario(
        name=_str(top.get("name", "default"), "name"),
        description=_str(top.get("description", ""), "description"),
        server=_server(_optional(top, "server", {}), "server"),
        rules=rules,
        default_action=_action(_optional(top, "default", {}), "default", base_dir),
        seed=seed,
    )


# -- sections ------------------------------------------------------------------------


def _server(data: Any, where: str) -> ServerSettings:
    m = _mapping(
        data,
        where,
        {"host", "port", "http_port", "encoding", "max_message_bytes", "history", "application"},
    )
    defaults = ServerSettings()
    http_port = m.get("http_port")
    encoding = _str(m.get("encoding", defaults.encoding), f"{where}.encoding")
    try:
        "".encode(encoding)
    except LookupError as exc:
        raise ScenarioError(f"{where}.encoding: unknown encoding {encoding!r}") from exc
    return ServerSettings(
        host=_str(m.get("host", defaults.host), f"{where}.host"),
        port=_port(m.get("port", defaults.port), f"{where}.port"),
        http_port=None if http_port is None else _port(http_port, f"{where}.http_port"),
        encoding=encoding,
        max_message_bytes=_int(
            m.get("max_message_bytes", defaults.max_message_bytes),
            f"{where}.max_message_bytes",
            minimum=1,
        ),
        history=_int(m.get("history", defaults.history), f"{where}.history", minimum=1),
        application=_str(m.get("application", defaults.application), f"{where}.application"),
    )


def _rule(data: Any, where: str, index: int, base_dir: Path) -> Rule:
    m = _mapping(data, where, {"name", "match", "probability", "action"})
    name = _str(m.get("name", f"rule-{index + 1}"), f"{where}.name")
    if not name:
        raise ScenarioError(f"{where}.name: must not be empty")
    return Rule(
        name=name,
        matcher=_matcher(_optional(m, "match", {}), f"{where}.match"),
        action=_action(_optional(m, "action", {}), f"{where}.action", base_dir),
        probability=_probability(m.get("probability", 1.0), f"{where}.probability"),
    )


def _matcher(data: Any, where: str) -> MessageMatcher:
    m = _mapping(data, where, {"type", "fields"})
    raw_types = m.get("type", [])
    types = [raw_types] if isinstance(raw_types, str) else raw_types
    if not isinstance(types, list) or not all(isinstance(t, str) and t for t in types):
        raise ScenarioError(f"{where}.type: must be a message type or a list of them")

    fields = _mapping(_optional(m, "fields", {}), f"{where}.fields", None)
    matchers: list[FieldMatcher] = []
    for raw_path, spec in fields.items():
        loc = f"{where}.fields.{raw_path}"
        try:
            path = FieldPath.parse(str(raw_path))
        except FieldPathError as exc:
            raise ScenarioError(f"{loc}: {exc}") from exc
        matchers.append(FieldMatcher(path, _condition(spec, loc)))
    return MessageMatcher(types=tuple(types), fields=tuple(matchers))


_OPERATORS = ("equals", "regex", "in", "exists")


def _condition(spec: Any, where: str) -> Condition:
    if isinstance(spec, str | int | float) and not isinstance(spec, bool):
        return Equals(str(spec))
    m = _mapping(spec, where, set(_OPERATORS))
    if len(m) != 1:
        raise ScenarioError(f"{where}: use exactly one of {', '.join(_OPERATORS)}")
    ((operator, value),) = m.items()
    if operator == "equals":
        return Equals(_scalar(value, f"{where}.equals"))
    if operator == "regex":
        try:
            return Matches(re.compile(_str(value, f"{where}.regex")))
        except re.error as exc:
            raise ScenarioError(f"{where}.regex: invalid regular expression: {exc}") from exc
    if operator == "in":
        if not isinstance(value, list) or not value:
            raise ScenarioError(f"{where}.in: must be a non-empty list")
        return OneOf(frozenset(_scalar(v, f"{where}.in") for v in value))
    if not isinstance(value, bool):
        raise ScenarioError(f"{where}.exists: must be true or false")
    return Exists(value)


def _action(data: Any, where: str, base_dir: Path) -> Action:
    m = _mapping(data, where, {"ack", "reply", "error", "error_code", "delay_ms", "followup"})
    ack_text = _str(m.get("ack", "AA"), f"{where}.ack").upper()
    try:
        ack = AckCode(ack_text)
    except ValueError as exc:
        choices = ", ".join(c.value for c in AckCode)
        raise ScenarioError(f"{where}.ack: must be one of {choices}") from exc
    reply_text = _str(m.get("reply", "ack"), f"{where}.reply").lower()
    try:
        reply = ReplyMode(reply_text)
    except ValueError as exc:
        choices = ", ".join(r.value for r in ReplyMode)
        raise ScenarioError(f"{where}.reply: must be one of {choices}") from exc
    error = m.get("error")
    raw_followups = _optional(m, "followup", [])
    if isinstance(raw_followups, Mapping):
        raw_followups = [raw_followups]
    if not isinstance(raw_followups, list):
        raise ScenarioError(f"{where}.followup: must be a mapping or a list of mappings")
    return Action(
        ack=ack,
        reply=reply,
        error=None if error is None else _str(error, f"{where}.error"),
        error_code=_scalar(m.get("error_code", DEFAULT_ERROR_CODE), f"{where}.error_code"),
        delay=_delay(m.get("delay_ms", 0), f"{where}.delay_ms"),
        followups=tuple(
            _followup(f, f"{where}.followup[{i}]", base_dir) for i, f in enumerate(raw_followups)
        ),
    )


def _followup(data: Any, where: str, base_dir: Path) -> FollowUp:
    m = _mapping(
        data, where, {"name", "host", "port", "delay_ms", "timeout_s", "template", "template_file"}
    )
    for key in ("host", "port"):
        if key not in m:
            raise ScenarioError(f"{where}.{key}: is required")
    if ("template" in m) == ("template_file" in m):
        raise ScenarioError(f"{where}: set exactly one of template or template_file")
    if "template_file" in m:
        file = base_dir / _str(m["template_file"], f"{where}.template_file")
        try:
            source = file.read_text(encoding="utf-8")
        except OSError as exc:
            raise ScenarioError(
                f"{where}.template_file: cannot read {file}: {exc.strerror or exc}"
            ) from exc
    else:
        source = _str(m["template"], f"{where}.template")
    try:
        template = Template(source)
    except TemplateError as exc:
        raise ScenarioError(f"{where}: {exc}") from exc
    timeout = m.get("timeout_s", 10.0)
    if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0:
        raise ScenarioError(f"{where}.timeout_s: must be a positive number")
    port = _port(m["port"], f"{where}.port")
    if port == 0:
        raise ScenarioError(f"{where}.port: must be between 1 and 65535")
    return FollowUp(
        host=_str(m["host"], f"{where}.host"),
        port=port,
        template=template,
        delay=_delay(m.get("delay_ms", 0), f"{where}.delay_ms"),
        timeout_s=float(timeout),
        name=_str(m.get("name", "follow-up"), f"{where}.name"),
    )


# -- primitives ----------------------------------------------------------------------


def _mapping(data: Any, where: str, allowed: set[str] | None) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ScenarioError(f"{where}: must be a mapping, got {type(data).__name__}")
    if allowed is not None:
        unknown = sorted(str(k) for k in data if k not in allowed)
        if unknown:
            raise ScenarioError(
                f"{where}: unknown key(s) {', '.join(unknown)} "
                f"(allowed: {', '.join(sorted(allowed))})"
            )
    return dict(data)


def _optional(mapping: Mapping[str, Any], key: str, default: Any) -> Any:
    """``mapping[key]``, treating a missing key and an explicit YAML null alike."""
    value = mapping.get(key)
    return default if value is None else value


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ScenarioError(f"{where}: must be a string")
    return value


def _scalar(value: Any, where: str) -> str:
    """Strings and numbers both compare as text (YAML may type ``123`` as int)."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise ScenarioError(f"{where}: must be a string or number")
    return str(value)


def _int(value: Any, where: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ScenarioError(f"{where}: must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f">= {minimum}"
        raise ScenarioError(f"{where}: must be {bound}")
    return value


def _port(value: Any, where: str) -> int:
    return _int(value, where, minimum=0, maximum=65535)


def _delay(value: Any, where: str) -> Delay:
    if isinstance(value, list):
        if len(value) != 2:
            raise ScenarioError(f"{where}: a range must be [min, max]")
        low, high = (_int(v, where) for v in value)
        if low > high:
            raise ScenarioError(f"{where}: min must not exceed max")
        return Delay(low, high)
    ms = _int(value, where)
    return Delay(ms, ms)


def _probability(value: Any, where: str) -> float:
    if isinstance(value, str) and value.strip().endswith("%"):
        try:
            number = float(value.strip()[:-1]) / 100
        except ValueError as exc:
            raise ScenarioError(f"{where}: invalid percentage {value!r}") from exc
    elif isinstance(value, int | float) and not isinstance(value, bool):
        number = float(value)
    else:
        raise ScenarioError(f"{where}: must be a number between 0 and 1, or a percentage")
    if not 0.0 <= number <= 1.0:
        raise ScenarioError(f"{where}: must be between 0 and 1 (or 0% and 100%)")
    return number
