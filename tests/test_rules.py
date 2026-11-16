from __future__ import annotations

import random
from collections import Counter

import pytest

from mllp_sim.ack import AckCode
from mllp_sim.hl7 import Message
from mllp_sim.rules import DEFAULT_RULE, UNPARSEABLE_RULE, Delay, RuleEngine
from mllp_sim.scenario import parse_scenario


def engine_for(rules: list[dict], seed: int = 0, default: dict | None = None) -> RuleEngine:
    data: dict = {"rules": rules}
    if default:
        data["default"] = default
    scenario = parse_scenario(data)
    return RuleEngine(scenario.rules, scenario.default_action, random.Random(seed))


@pytest.mark.parametrize(
    ("pattern", "matches"),
    [
        ("ADT^A01", True),
        ("ADT^A0*", True),
        ("ADT", True),
        ("AD?", True),
        ("ADT^A08", False),
        ("ORM^O01", False),
        ("adt^a01", False),
        ("*^A01", True),
    ],
)
def test_type_patterns(adt_a01: str, pattern: str, matches: bool) -> None:
    engine = engine_for([{"name": "r", "match": {"type": pattern}}])
    decision = engine.evaluate(Message.parse(adt_a01))
    assert (decision.rule == "r") is matches


def test_type_list(adt_a01: str) -> None:
    engine = engine_for([{"name": "r", "match": {"type": ["ORM^O01", "ADT^A01"]}}])
    assert engine.evaluate(Message.parse(adt_a01)).rule == "r"


@pytest.mark.parametrize(
    ("fields", "matches"),
    [
        ({"PID-3.1": "MRN0001"}, True),
        ({"PID-3.1": "MRN0002"}, False),
        ({"PID-3.1": {"equals": "MRN0001"}}, True),
        ({"PID-3.1": {"regex": "^MRN\\d+$"}}, True),
        ({"PID-3.1": {"regex": "^TEST"}}, False),
        ({"PID-5.1": {"regex": "OE"}}, True),  # search, not fullmatch
        ({"PV1-2": {"in": ["I", "E"]}}, True),
        ({"PV1-2": {"in": ["O"]}}, False),
        ({"PID-8": {"exists": True}}, True),
        ({"PID-30": {"exists": True}}, False),
        ({"PID-30": {"exists": False}}, True),
        ({"PID-3.1": "MRN0001", "PV1-2": "O"}, False),  # all must hold
        ({"PID-1": 1}, True),  # YAML integers compare as text
    ],
)
def test_field_conditions(adt_a01: str, fields: dict, matches: bool) -> None:
    engine = engine_for([{"name": "r", "match": {"fields": fields}}])
    assert (engine.evaluate(Message.parse(adt_a01)).rule == "r") is matches


def test_first_match_wins(adt_a01: str) -> None:
    engine = engine_for(
        [
            {"name": "first", "match": {"type": "ADT"}, "action": {"ack": "AE"}},
            {"name": "second", "match": {"type": "ADT^A01"}, "action": {"ack": "AR"}},
        ]
    )
    decision = engine.evaluate(Message.parse(adt_a01))
    assert decision.rule == "first"
    assert decision.action.ack is AckCode.AE


def test_default_when_nothing_matches(adt_a01: str) -> None:
    engine = engine_for([{"name": "r", "match": {"type": "ORM"}}], default={"ack": "AR"})
    decision = engine.evaluate(Message.parse(adt_a01))
    assert decision.rule == DEFAULT_RULE
    assert decision.action.ack is AckCode.AR


def test_unparseable_is_rejected() -> None:
    decision = engine_for([]).evaluate(None)
    assert decision.rule == UNPARSEABLE_RULE
    assert decision.action.ack is AckCode.AR


def test_probability_is_roughly_honoured_and_falls_through(adt_a01: str) -> None:
    engine = engine_for(
        [{"name": "chaos", "probability": "25%", "action": {"reply": "drop"}}], seed=42
    )
    message = Message.parse(adt_a01)
    counts = Counter(engine.evaluate(message).rule for _ in range(4000))
    assert 900 < counts["chaos"] < 1100
    assert counts["chaos"] + counts[DEFAULT_RULE] == 4000


def test_probability_is_reproducible_with_seed(adt_a01: str) -> None:
    message = Message.parse(adt_a01)
    rules = [{"name": "c", "probability": 0.5}]
    first = [engine_for(rules, seed=7).evaluate(message).rule for _ in range(1)]
    runs = []
    for _ in range(2):
        engine = engine_for(rules, seed=7)
        runs.append([engine.evaluate(message).rule for _ in range(50)])
    assert runs[0] == runs[1]
    assert first[0] == runs[0][0]


def test_probability_zero_never_fires(adt_a01: str) -> None:
    engine = engine_for([{"name": "never", "probability": 0}])
    message = Message.parse(adt_a01)
    assert all(engine.evaluate(message).rule == DEFAULT_RULE for _ in range(100))


def test_delay_sampling() -> None:
    rng = random.Random(3)
    assert Delay(250, 250).sample_seconds(rng) == 0.25
    samples = [Delay(100, 200).sample_seconds(rng) for _ in range(200)]
    assert all(0.1 <= s <= 0.2 for s in samples)
    assert not Delay()
    assert Delay(1, 1)
