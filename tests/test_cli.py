"""CLI tests. ``send`` runs its own event loop, so the server lives in a thread."""

from __future__ import annotations

import asyncio
import json
import random
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from mllp_sim import __version__
from mllp_sim.cli import EXIT_NEGATIVE_ACK, EXIT_OK, EXIT_TRANSPORT, EXIT_USAGE, main
from mllp_sim.client import split_messages
from mllp_sim.hl7 import HL7ParseError
from mllp_sim.scenario import Scenario, parse_scenario
from mllp_sim.server import MllpServer

from .conftest import ADT_A01, EXAMPLES


class ThreadedServer:
    """Runs an MllpServer on a private event loop in a background thread."""

    def __init__(self, scenario: dict) -> None:
        data = {**scenario, "server": {"host": "127.0.0.1", "port": 0}}
        self.server = MllpServer(parse_scenario(data, base_dir=EXAMPLES), rng=random.Random(1))
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self) -> int:
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.server.start(), self.loop).result(5)
        return self.server.port

    def __exit__(self, *exc: object) -> None:
        asyncio.run_coroutine_threadsafe(self.server.close(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


@pytest.fixture
def accept_port() -> Iterator[int]:
    with ThreadedServer({}) as port:
        yield port


@pytest.fixture
def adt_file(tmp_path: Path) -> Path:
    path = tmp_path / "adt.hl7"
    path.write_text(ADT_A01.replace("\r", "\n"))
    return path


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_send_accepted(accept_port: int, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["send", str(EXAMPLES / "messages" / "batch.hl7"), "--port", str(accept_port)])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert out.count("<- AA") == 3
    assert "3 message(s): 3 accepted, 0 rejected, 0 failed" in out
    assert "MSA|AA|BATCH0001" in out


def test_send_json(accept_port: int, adt_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["send", str(adt_file), "--port", str(accept_port), "--json"])
    record = json.loads(capsys.readouterr().out.strip())
    assert code == EXIT_OK
    assert record["ack_code"] == "AA"
    assert record["control_id"] == "MSG00001"
    assert record["control_id_matches"] is True


def test_send_quiet(accept_port: int, adt_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["send", str(adt_file), "-p", str(accept_port), "-q"]) == EXIT_OK
    assert capsys.readouterr().out.strip().splitlines() == [
        "1 message(s): 1 accepted, 0 rejected, 0 failed"
    ]


def test_send_from_stdin(
    accept_port: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(ADT_A01))
    assert main(["send", "-", "-p", str(accept_port)]) == EXIT_OK
    assert "<- AA" in capsys.readouterr().out


def test_negative_ack_stops_unless_keep_going(capsys: pytest.CaptureFixture[str]) -> None:
    scenario = {"default": {"ack": "AE", "error": "nope"}}
    batch = str(EXAMPLES / "messages" / "batch.hl7")
    with ThreadedServer(scenario) as port:
        assert main(["send", batch, "-p", str(port)]) == EXIT_NEGATIVE_ACK
        out = capsys.readouterr().out
        assert "0 accepted, 1 rejected, 0 failed, 2 not sent" in out
        assert "error='nope'" in out
        assert main(["send", batch, "-p", str(port), "--keep-going"]) == EXIT_NEGATIVE_ACK
        assert "0 accepted, 3 rejected" in capsys.readouterr().out


def test_timeout_exit_code(adt_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with ThreadedServer({"default": {"reply": "none"}}) as port:
        code = main(["send", str(adt_file), "-p", str(port), "--timeout", "0.2"])
    assert code == EXIT_TRANSPORT
    assert "FAILED no ACK" in capsys.readouterr().out


def test_connection_refused_exit_code(adt_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with ThreadedServer({}) as port:
        pass  # the port is now closed
    assert main(["send", str(adt_file), "-p", str(port), "--json", "-t", "1"]) == EXIT_TRANSPORT
    assert json.loads(capsys.readouterr().out)["ack_code"] is None


def test_keep_going_reconnects_after_drop(capsys: pytest.CaptureFixture[str]) -> None:
    scenario = {"rules": [{"match": {"type": "ADT^A08"}, "action": {"reply": "drop"}}]}
    batch = str(EXAMPLES / "messages" / "batch.hl7")
    with ThreadedServer(scenario) as port:
        assert main(["send", batch, "-p", str(port), "-k"]) == EXIT_TRANSPORT
    assert "2 accepted, 0 rejected, 1 failed" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("content", "error"),
    [("", "no HL7 messages"), ("PID|1\nMSH|^~\\&|", "before any MSH")],
)
def test_send_bad_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str, error: str
) -> None:
    path = tmp_path / "bad.hl7"
    path.write_text(content)
    assert main(["send", str(path)]) == EXIT_USAGE
    assert error in capsys.readouterr().err


def test_send_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["send", "/nonexistent/file.hl7"]) == EXIT_USAGE
    assert "cannot read" in capsys.readouterr().err


def test_validate(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    scenarios = sorted(str(p) for p in (EXAMPLES / "scenarios").glob("*.yaml"))
    assert main(["validate", *scenarios]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.count("ok   ") == len(scenarios)
    assert "followup=[result->localhost:2576]" in out

    bad = tmp_path / "bad.yaml"
    bad.write_text("rules:\n  - action: {ack: NOPE}\n")
    assert main(["validate", str(bad)]) == EXIT_USAGE
    assert "rules[0].action.ack" in capsys.readouterr().out


def test_serve_rejects_invalid_scenario(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("server: {port: -1}\n")
    assert main(["serve", "--scenario", str(bad)]) == EXIT_USAGE
    assert "server.port" in capsys.readouterr().err


def test_serve_reports_bind_failure(capsys: pytest.CaptureFixture[str]) -> None:
    with ThreadedServer({}) as port:
        assert main(["serve", "--port", str(port)]) == EXIT_TRANSPORT
    assert "cannot start server" in capsys.readouterr().err


def test_split_messages() -> None:
    text = "\x0b" + ADT_A01 + "\x1c\r\n\nBHS|^~\\&\r" + ADT_A01.replace("MSG00001", "TWO") + "BTS|2"
    messages = split_messages(text)
    assert len(messages) == 2
    assert "TWO" in messages[1]
    assert all(m.endswith("\r") and m.startswith("MSH") for m in messages)
    with pytest.raises(HL7ParseError):
        split_messages("PID|1\r")
    assert split_messages("\n\n") == []


def test_env_overrides_scenario_and_flags_override_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict = {}

    async def fake_serve(scenario: Scenario, record: Path | None) -> None:
        captured["server"] = scenario.server

    monkeypatch.setattr("mllp_sim.cli._serve", fake_serve)
    path = tmp_path / "s.yaml"
    path.write_text("server: {host: 127.0.0.1, port: 3000, http_port: 3001}\n")
    monkeypatch.setenv("MLLP_SIM_HOST", "0.0.0.0")
    monkeypatch.setenv("MLLP_SIM_HTTP_PORT", "9090")
    assert main(["serve", "-s", str(path), "--http-port", "9191"]) == EXIT_OK
    server = captured["server"]
    assert (server.host, server.port, server.http_port) == ("0.0.0.0", 3000, 9191)


@pytest.mark.parametrize(("value", "error"), [("abc", "port number"), ("70000", "between")])
def test_invalid_env_port(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str, error: str
) -> None:
    monkeypatch.setenv("MLLP_SIM_PORT", value)
    assert main(["serve"]) == EXIT_USAGE
    assert error in capsys.readouterr().err
