"""Command line interface: ``mllp-sim serve | send | validate``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from mllp_sim import __version__
from mllp_sim.client import AckResult, MllpClient, split_messages
from mllp_sim.framing import MLLPError
from mllp_sim.hl7 import HL7ParseError, Message
from mllp_sim.rules import Action, ReplyMode
from mllp_sim.scenario import DEFAULT_PORT, Scenario, ScenarioError, load_scenario
from mllp_sim.server import MllpServer
from mllp_sim.traffic import TrafficLog
from mllp_sim.web import StatusServer

log = logging.getLogger("mllp_sim")

EXIT_OK = 0
EXIT_NEGATIVE_ACK = 1
EXIT_USAGE = 2
EXIT_TRANSPORT = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mllp-sim",
        description="Scriptable HL7 v2 MLLP test server and client.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    logging_opts = argparse.ArgumentParser(add_help=False)
    logging_opts.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        type=str.upper,
        help="log verbosity (default: INFO for serve, WARNING otherwise)",
    )
    logging_opts.add_argument(
        "-v", "--verbose", action="store_true", help="shorthand for --log-level DEBUG"
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    serve = commands.add_parser(
        "serve",
        parents=[logging_opts],
        help="run the simulator (MLLP server)",
        description="Listen for MLLP connections and answer according to a scenario.",
    )
    serve.add_argument("-s", "--scenario", type=Path, help="scenario YAML file (default: ACK AA)")
    serve.add_argument("--host", help="interface to bind [env MLLP_SIM_HOST] (default: 127.0.0.1)")
    serve.add_argument(
        "-p", "--port", type=int, help=f"MLLP port [env MLLP_SIM_PORT] (default: {DEFAULT_PORT})"
    )
    serve.add_argument(
        "--http-port",
        type=int,
        help="serve the HTTP status page on this port [env MLLP_SIM_HTTP_PORT]",
    )
    serve.add_argument("--record", type=Path, metavar="FILE", help="append traffic as JSON lines")
    serve.add_argument("--seed", type=int, help="random seed for reproducible chaos")

    send = commands.add_parser(
        "send",
        parents=[logging_opts],
        help="send messages from files and check the ACKs",
        description=(
            "Send every message in the given files over one connection and print the ACKs. "
            "Exit status: 0 all accepted, 1 negative/invalid ACK, 2 usage or input error, "
            "3 timeout or connection failure."
        ),
    )
    send.add_argument("files", nargs="+", metavar="FILE", help="HL7 file(s); '-' reads stdin")
    send.add_argument("--host", default="localhost", help="server host (default: localhost)")
    send.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help="server port")
    send.add_argument(
        "-t", "--timeout", type=float, default=10.0, help="seconds to wait per ACK (default: 10)"
    )
    send.add_argument("--encoding", default="utf-8", help="wire encoding (default: utf-8)")
    send.add_argument(
        "-k", "--keep-going", action="store_true", help="continue after a failed message"
    )
    output = send.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print one JSON object per message")
    output.add_argument("-q", "--quiet", action="store_true", help="print only the summary line")

    validate = commands.add_parser(
        "validate",
        parents=[logging_opts],
        help="check scenario files without starting a server",
    )
    validate.add_argument("scenarios", nargs="+", type=Path, metavar="SCENARIO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    default_level = "INFO" if args.command == "serve" else "WARNING"
    _configure_logging("DEBUG" if args.verbose else (args.log_level or default_level))
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "send":
        return _cmd_send(args)
    return _cmd_validate(args)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


# -- serve ---------------------------------------------------------------------------


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        scenario = load_scenario(args.scenario) if args.scenario else Scenario()
    except ScenarioError as exc:
        print(f"mllp-sim: {exc}", file=sys.stderr)
        return EXIT_USAGE
    # Precedence: command-line flags, then environment variables, then the scenario file.
    try:
        overrides = {
            "host": args.host or os.environ.get("MLLP_SIM_HOST") or None,
            "port": args.port if args.port is not None else _env_port("MLLP_SIM_PORT"),
            "http_port": (
                args.http_port if args.http_port is not None else _env_port("MLLP_SIM_HTTP_PORT")
            ),
        }
    except ValueError as exc:
        print(f"mllp-sim: {exc}", file=sys.stderr)
        return EXIT_USAGE
    for name in ("port", "http_port"):
        value = overrides[name]
        if value is not None and not 0 <= value <= 65535:
            print(f"mllp-sim: {name} must be between 0 and 65535", file=sys.stderr)
            return EXIT_USAGE
    scenario = scenario.with_server(**overrides)
    if args.seed is not None:
        scenario = replace(scenario, seed=args.seed)
    try:
        asyncio.run(_serve(scenario, args.record))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"mllp-sim: cannot start server: {exc}", file=sys.stderr)
        return EXIT_TRANSPORT
    return EXIT_OK


def _env_port(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a port number, got {raw!r}") from None


async def _serve(scenario: Scenario, record: Path | None) -> None:
    settings = scenario.server
    traffic = TrafficLog(history=settings.history, record_path=record)
    server = MllpServer(scenario, traffic=traffic)
    status = (
        StatusServer(traffic, settings.host, settings.http_port, scenario=scenario.name)
        if settings.http_port is not None
        else None
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await server.start()
        if status is not None:
            await status.start()
        if record is not None:
            log.info("recording traffic to %s", record)
        await stop.wait()
        log.info("shutting down")
    finally:
        if status is not None:
            await status.close()
        await server.close()
        traffic.close()


# -- send ----------------------------------------------------------------------------


def _cmd_send(args: argparse.Namespace) -> int:
    messages: list[tuple[str, str]] = []
    for name in args.files:
        try:
            text = sys.stdin.read() if name == "-" else Path(name).read_text(encoding="utf-8")
            found = split_messages(text)
        except OSError as exc:
            print(f"mllp-sim: cannot read {name}: {exc.strerror or exc}", file=sys.stderr)
            return EXIT_USAGE
        except HL7ParseError as exc:
            print(f"mllp-sim: {name}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if not found:
            print(f"mllp-sim: {name}: no HL7 messages found", file=sys.stderr)
            return EXIT_USAGE
        messages.extend((name, m) for m in found)
    try:
        return asyncio.run(_send_all(args, messages, sys.stdout))
    except KeyboardInterrupt:
        return EXIT_TRANSPORT


async def _send_all(args: argparse.Namespace, messages: list[tuple[str, str]], out: TextIO) -> int:
    worst = EXIT_OK
    accepted = rejected = failed = 0
    client: MllpClient | None = None
    try:
        for index, (source, text) in enumerate(messages, start=1):
            summary = _describe(text)
            if client is None:
                client = MllpClient(
                    args.host, args.port, timeout=args.timeout, encoding=args.encoding
                )
            try:
                result = await client.send(text)
            except (MLLPError, OSError) as exc:
                failed += 1
                worst = max(worst, EXIT_TRANSPORT)
                _report_failure(args, out, index, source, summary, exc)
                await client.close()
                client = None
                if not args.keep_going:
                    break
                continue
            if result.accepted:
                accepted += 1
            else:
                rejected += 1
                worst = max(worst, EXIT_NEGATIVE_ACK)
            _report(args, out, index, source, summary, result)
            if not result.accepted and not args.keep_going:
                break
    finally:
        if client is not None:
            await client.close()
    skipped = len(messages) - accepted - rejected - failed
    if not args.json:
        line = (
            f"{len(messages)} message(s): {accepted} accepted, {rejected} rejected, {failed} failed"
        )
        if skipped:
            line += f", {skipped} not sent"
        print(line, file=out)
    return worst


def _describe(text: str) -> dict[str, str]:
    try:
        message = Message.parse(text)
    except HL7ParseError:
        return {"type": "?", "control_id": ""}
    return {"type": message.message_type, "control_id": message.control_id}


def _report(
    args: argparse.Namespace,
    out: TextIO,
    index: int,
    source: str,
    summary: dict[str, str],
    result: AckResult,
) -> None:
    if args.json:
        record = {
            "index": index,
            "file": source,
            "type": summary["type"],
            "control_id": summary["control_id"],
            "ack_code": result.code,
            "acked_control_id": result.acked_control_id,
            "control_id_matches": result.control_id_matches,
            "error": result.error_text or None,
            "latency_ms": round(result.latency_ms, 3),
            "ack": result.text,
        }
        print(json.dumps(record, ensure_ascii=False), file=out)
        return
    if args.quiet:
        return
    print(f"-> #{index} {summary['type']} control_id={summary['control_id']} ({source})", file=out)
    status = result.code or "INVALID-ACK"
    line = f"<- {status} control_id={result.acked_control_id} {result.latency_ms:.1f} ms"
    if result.error_text:
        line += f" error={result.error_text!r}"
    print(line, file=out)
    if not result.control_id_matches:
        print(
            f"   warning: MSA-2 {result.acked_control_id!r} does not match "
            f"sent MSH-10 {result.sent_control_id!r}",
            file=out,
        )
    for segment in result.text.replace("\n", "\r").split("\r"):
        if segment:
            print(f"   {segment}", file=out)


def _report_failure(
    args: argparse.Namespace,
    out: TextIO,
    index: int,
    source: str,
    summary: dict[str, str],
    exc: BaseException,
) -> None:
    error = str(exc) or type(exc).__name__
    if args.json:
        record = {
            "index": index,
            "file": source,
            "type": summary["type"],
            "control_id": summary["control_id"],
            "ack_code": None,
            "error": error,
        }
        print(json.dumps(record, ensure_ascii=False), file=out)
    elif not args.quiet:
        print(
            f"-> #{index} {summary['type']} control_id={summary['control_id']} ({source})", file=out
        )
        print(f"<- FAILED {error}", file=out)


# -- validate ------------------------------------------------------------------------


def _cmd_validate(args: argparse.Namespace) -> int:
    status = EXIT_OK
    for path in args.scenarios:
        try:
            scenario = load_scenario(path)
        except ScenarioError as exc:
            print(f"FAIL {path}: {exc}")
            status = EXIT_USAGE
            continue
        print(f"ok   {path}: scenario {scenario.name!r}, {len(scenario.rules)} rule(s)")
        for rule in scenario.rules:
            print(
                f"       - {rule.name}: {_summarize_action(rule.action)}"
                + (f" ({rule.probability:.0%} of matches)" if rule.probability < 1 else "")
            )
        print(f"       - <default>: {_summarize_action(scenario.default_action)}")
    return status


def _summarize_action(action: Action) -> str:
    parts = [f"reply={action.reply.value}"]
    if action.reply not in (ReplyMode.NONE, ReplyMode.DROP):
        parts.append(f"ack={action.ack.value}")
    if action.delay:
        delay = action.delay
        parts.append(
            f"delay={delay.min_ms}ms"
            if delay.min_ms == delay.max_ms
            else f"delay={delay.min_ms}-{delay.max_ms}ms"
        )
    if action.followups:
        targets = ", ".join(f"{f.name}->{f.host}:{f.port}" for f in action.followups)
        parts.append(f"followup=[{targets}]")
    return " ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
