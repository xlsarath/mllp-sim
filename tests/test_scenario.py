from __future__ import annotations

from pathlib import Path

import pytest

from mllp_sim.ack import AckCode
from mllp_sim.rules import Delay, Equals, Exists, Matches, OneOf, ReplyMode
from mllp_sim.scenario import Scenario, ScenarioError, load_scenario, parse_scenario

from .conftest import EXAMPLES


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*/*.yaml")), ids=str)
def test_examples_are_valid(path: Path) -> None:
    scenario = load_scenario(path)
    assert scenario.source == path
    assert scenario.name


def test_empty_document_gives_defaults() -> None:
    scenario = parse_scenario({})
    assert scenario == Scenario()
    assert scenario.default_action.ack is AckCode.AA
    assert scenario.server.port == 2575


def test_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("")
    assert load_scenario(path).rules == ()


def test_full_rule_is_parsed() -> None:
    scenario = parse_scenario(
        {
            "name": "x",
            "seed": 3,
            "server": {"host": "0.0.0.0", "port": 3000, "http_port": 8081, "encoding": "latin-1"},
            "rules": [
                {
                    "name": "r1",
                    "probability": "50%",
                    "match": {
                        "type": "ADT^A08",
                        "fields": {
                            "PID-3.1": "X",
                            "PID-5": {"regex": "^DOE"},
                            "PV1-2": {"in": ["I", "O"]},
                            "OBR-4": {"exists": True},
                        },
                    },
                    "action": {
                        "ack": "ae",
                        "error": "boom",
                        "error_code": 207,
                        "delay_ms": [10, 20],
                        "reply": "duplicate",
                        "followup": {
                            "host": "localhost",
                            "port": 2576,
                            "template": "MSH|^~\\&|A|B|C|D|{{now}}||ORU^R01|{{control_id}}|P|2.5",
                        },
                    },
                }
            ],
        }
    )
    assert scenario.seed == 3
    assert scenario.server.encoding == "latin-1"
    rule = scenario.rules[0]
    assert rule.probability == 0.5
    assert rule.matcher.types == ("ADT^A08",)
    conditions = [f.condition for f in rule.matcher.fields]
    assert isinstance(conditions[0], Equals)
    assert isinstance(conditions[1], Matches)
    assert isinstance(conditions[2], OneOf)
    assert isinstance(conditions[3], Exists)
    assert rule.action.ack is AckCode.AE
    assert rule.action.error_code == "207"
    assert rule.action.delay == Delay(10, 20)
    assert rule.action.reply is ReplyMode.DUPLICATE
    assert rule.action.followups[0].port == 2576


def test_with_server_overrides_only_given_values() -> None:
    scenario = parse_scenario({"server": {"port": 1000, "http_port": 9000}})
    updated = scenario.with_server(port=2000, host=None)
    assert updated.server.port == 2000
    assert updated.server.http_port == 9000
    assert scenario.with_server() is scenario


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ([], "scenario: must be a mapping"),
        ({"rulez": []}, "unknown key"),
        ({"seed": "x"}, "seed: must be an integer"),
        ({"rules": {}}, "rules: must be a list"),
        ({"server": {"port": 70000}}, r"server\.port: must be between"),
        ({"server": {"encoding": "nope"}}, "unknown encoding"),
        ({"server": {"history": 0}}, r"server\.history"),
        ({"rules": [{"action": {"ack": "OK"}}]}, r"rules\[0\]\.action\.ack: must be one of"),
        ({"rules": [{"action": {"reply": "maybe"}}]}, r"rules\[0\]\.action\.reply"),
        ({"rules": [{"action": {"delay_ms": -1}}]}, "delay_ms"),
        ({"rules": [{"action": {"delay_ms": [5, 1]}}]}, "min must not exceed max"),
        ({"rules": [{"action": {"delay_ms": [1, 2, 3]}}]}, r"\[min, max\]"),
        ({"rules": [{"probability": 2}]}, "between 0 and 1"),
        ({"rules": [{"probability": "abc%"}]}, "invalid percentage"),
        ({"rules": [{"probability": True}]}, "probability"),
        ({"rules": [{"match": {"type": 5}}]}, r"match\.type"),
        ({"rules": [{"match": {"fields": {"PID3": "x"}}}]}, "invalid field path"),
        ({"rules": [{"match": {"fields": {"PID-3": {"regex": "("}}}}]}, "invalid regular"),
        ({"rules": [{"match": {"fields": {"PID-3": {"regx": "a"}}}}]}, "unknown key"),
        ({"rules": [{"match": {"fields": {"PID-3": {"equals": "a", "regex": "b"}}}}]}, "exactly"),
        ({"rules": [{"match": {"fields": {"PID-3": {"in": []}}}}]}, "non-empty list"),
        ({"rules": [{"match": {"fields": {"PID-3": {"exists": "yes"}}}}]}, "true or false"),
        ({"rules": [{"name": "a"}, {"name": "a"}]}, "duplicate rule name"),
        ({"rules": [{"name": ""}]}, "must not be empty"),
        ({"rules": [{"action": {"followup": {"port": 1, "template": "MSH|^~\\&|"}}}]}, "host"),
        (
            {"rules": [{"action": {"followup": {"host": "h", "port": 1}}}]},
            "exactly one of template or template_file",
        ),
        (
            {"rules": [{"action": {"followup": {"host": "h", "port": 1, "template": "PID|1"}}}]},
            "must start with an MSH",
        ),
        (
            {
                "rules": [
                    {
                        "action": {
                            "followup": {"host": "h", "port": 1, "template": "MSH|^~\\&|{{ nope }}"}
                        }
                    }
                ]
            },
            "unknown placeholder",
        ),
        (
            {
                "rules": [
                    {"action": {"followup": {"host": "h", "port": 0, "template": "MSH|^~\\&|"}}}
                ]
            },
            "between 1 and 65535",
        ),
        (
            {
                "rules": [
                    {
                        "action": {
                            "followup": {
                                "host": "h",
                                "port": 1,
                                "timeout_s": 0,
                                "template": "MSH|^~\\&|",
                            }
                        }
                    }
                ]
            },
            "timeout_s",
        ),
    ],
)
def test_invalid_scenarios(data: object, error: str) -> None:
    with pytest.raises(ScenarioError, match=error):
        parse_scenario(data)


def test_template_file_resolves_relative_to_scenario(tmp_path: Path) -> None:
    (tmp_path / "t.hl7").write_text("MSH|^~\\&|A|B|C|D|{{now}}||ORU^R01|{{control_id}}|P|2.5\n")
    (tmp_path / "s.yaml").write_text(
        "rules:\n  - action:\n      followup: {host: localhost, port: 2576, template_file: t.hl7}\n"
    )
    scenario = load_scenario(tmp_path / "s.yaml")
    assert scenario.rules[0].action.followups[0].template.placeholders == ("now", "control_id")


def test_missing_template_file(tmp_path: Path) -> None:
    (tmp_path / "s.yaml").write_text(
        "rules:\n  - action:\n      followup: {host: h, port: 1, template_file: nope.hl7}\n"
    )
    with pytest.raises(ScenarioError, match="cannot read"):
        load_scenario(tmp_path / "s.yaml")


def test_unreadable_and_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="cannot read scenario"):
        load_scenario(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("rules: [\n")
    with pytest.raises(ScenarioError, match="invalid YAML"):
        load_scenario(bad)
