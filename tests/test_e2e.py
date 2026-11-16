"""End-to-end tests: real sockets, real server, real client."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from mllp_sim.client import ConnectionClosedError, MllpClient, MllpTimeoutError
from mllp_sim.framing import FrameDecoder, encode_frame
from mllp_sim.hl7 import Message
from mllp_sim.traffic import TrafficLog

from .conftest import ADT_A01, ORM_O01, ServerFactory


def with_control_id(text: str, control_id: str) -> str:
    return text.replace("MSG00001", control_id, 1)


async def test_default_scenario_accepts(make_server: ServerFactory) -> None:
    async with make_server() as server, MllpClient("127.0.0.1", server.port) as client:
        result = await client.send(ADT_A01)
    assert result.accepted
    assert result.code == "AA"
    assert result.control_id_matches
    assert result.message is not None
    assert result.message.get("MSH-5") == "SENDING_APP"
    assert server.traffic.recent()[0].rule == "<default>"


async def test_newline_terminated_input_is_normalised(make_server: ServerFactory) -> None:
    async with make_server() as server, MllpClient("127.0.0.1", server.port) as client:
        result = await client.send(ADT_A01.replace("\r", "\n"))
    assert result.accepted
    assert "\n" not in server.traffic.recent()[0].message


async def test_rule_returns_application_error(make_server: ServerFactory) -> None:
    scenario = {
        "rules": [
            {
                "name": "bad-mrn",
                "match": {"type": "ADT^A01", "fields": {"PID-3.1": {"regex": "^MRN"}}},
                "action": {"ack": "AE", "error": "Patient not found", "error_code": "204"},
            }
        ]
    }
    async with make_server(scenario) as server, MllpClient("127.0.0.1", server.port) as client:
        result = await client.send(ADT_A01)
    assert result.code == "AE"
    assert not result.accepted
    assert result.error_text == "Patient not found"
    assert result.message is not None
    assert result.message.get("ERR-3.1") == "204"
    exchange = server.traffic.recent()[0]
    assert exchange.rule == "bad-mrn"
    assert exchange.ack_code == "AE"


async def test_many_messages_on_one_connection(make_server: ServerFactory) -> None:
    async with make_server() as server, MllpClient("127.0.0.1", server.port) as client:
        for i in range(25):
            result = await client.send(with_control_id(ADT_A01, f"C{i:04d}"))
            assert result.acked_control_id == f"C{i:04d}"
    assert len(server.traffic.recent()) == 25


async def test_pipelined_frames_in_one_write_are_answered_in_order(
    make_server: ServerFactory,
) -> None:
    async with make_server() as server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        payload = b"".join(
            encode_frame(with_control_id(ADT_A01, f"P{i}").encode()) for i in range(3)
        )
        writer.write(payload)
        await writer.drain()
        decoder, frames = FrameDecoder(), []
        while len(frames) < 3:
            frames += decoder.feed(await asyncio.wait_for(reader.read(4096), 2))
        writer.close()
        await writer.wait_closed()
    acked = [Message.parse(f.decode()).get("MSA-2") for f in frames]
    assert acked == ["P0", "P1", "P2"]


async def test_frame_split_across_tcp_writes(make_server: ServerFactory) -> None:
    async with make_server() as server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        frame = encode_frame(ADT_A01.encode())
        for part in (frame[:1], frame[1:30], frame[30:-1], frame[-1:]):
            writer.write(part)
            await writer.drain()
            await asyncio.sleep(0.01)
        data = await asyncio.wait_for(reader.read(4096), 2)
        writer.close()
        await writer.wait_closed()
    (ack,) = FrameDecoder().feed(data)
    assert Message.parse(ack.decode()).get("MSA-1") == "AA"


async def test_unparseable_message_gets_ar(make_server: ServerFactory) -> None:
    async with make_server() as server, MllpClient("127.0.0.1", server.port) as client:
        result = await client.send("this is not hl7")
    assert result.code == "AR"
    assert result.message is not None
    assert result.message.get("ERR-3.1") == "100"
    exchange = server.traffic.recent()[0]
    assert exchange.message_type is None
    assert exchange.error


async def test_delay(make_server: ServerFactory) -> None:
    scenario = {"default": {"delay_ms": 150}}
    async with make_server(scenario) as server, MllpClient("127.0.0.1", server.port) as client:
        result = await client.send(ADT_A01)
    assert result.latency_ms >= 140


async def test_no_reply_times_out_but_connection_stays_usable(make_server: ServerFactory) -> None:
    scenario = {
        "rules": [{"name": "silent", "match": {"type": "ADT^A08"}, "action": {"reply": "none"}}]
    }
    silent = ADT_A01.replace("ADT^A01^ADT_A01", "ADT^A08^ADT_A01")
    async with make_server(scenario) as server:
        async with MllpClient("127.0.0.1", server.port, timeout=0.3) as client:
            with pytest.raises(MllpTimeoutError):
                await client.send(silent)
            result = await client.send(with_control_id(ADT_A01, "AFTER"))
        assert result.acked_control_id == "AFTER"
        assert server.traffic.recent()[1].reply == "none"


async def test_drop_closes_connection(make_server: ServerFactory) -> None:
    scenario = {"default": {"reply": "drop"}}
    async with make_server(scenario) as server, MllpClient("127.0.0.1", server.port) as client:
        with pytest.raises(ConnectionClosedError):
            await client.send(ADT_A01)
    assert server.traffic.recent()[0].reply == "drop"


async def test_duplicate_ack_is_not_mistaken_for_next_ack(
    make_server: ServerFactory, caplog: pytest.LogCaptureFixture
) -> None:
    scenario = {"default": {"reply": "duplicate"}}
    async with make_server(scenario) as server, MllpClient("127.0.0.1", server.port) as client:
        first = await client.send(with_control_id(ADT_A01, "D1"))
        await asyncio.sleep(0.05)  # let the duplicate arrive
        with caplog.at_level(logging.WARNING, logger="mllp_sim.client"):
            second = await client.send(with_control_id(ADT_A01, "D2"))
    assert first.acked_control_id == "D1"
    assert second.acked_control_id == "D2"
    assert "duplicate ACK" in caplog.text


async def test_garbled_reply_is_never_a_frame(make_server: ServerFactory) -> None:
    scenario = {"default": {"reply": "garbled"}}
    async with make_server(scenario) as server:
        async with MllpClient("127.0.0.1", server.port, timeout=0.3) as client:
            with pytest.raises(MllpTimeoutError):
                await client.send(ADT_A01)
        assert server.traffic.recent()[0].reply == "garbled"


async def test_inbound_ack_is_not_acknowledged(make_server: ServerFactory) -> None:
    ack = "MSH|^~\\&|A|B|C|D|20260101||ACK^A01^ACK|X1|P|2.5.1\rMSA|AA|MSG00001\r"
    async with make_server() as server:
        async with MllpClient("127.0.0.1", server.port, timeout=0.3) as client:
            with pytest.raises(MllpTimeoutError):
                await client.send(ack)
        assert server.traffic.recent()[0].reply == "none"


async def test_oversized_message_closes_connection(make_server: ServerFactory) -> None:
    scenario = {"server": {"max_message_bytes": 64}}
    async with make_server(scenario) as server, MllpClient("127.0.0.1", server.port) as client:
        with pytest.raises(ConnectionClosedError):
            await client.send(ADT_A01)


async def test_client_connection_refused() -> None:
    port = await closed_port()
    with pytest.raises((OSError, MllpTimeoutError)):  # refused, or slow to refuse
        await MllpClient("127.0.0.1", port, timeout=1).connect()


async def test_followup_is_sent_with_inbound_values(make_server: ServerFactory) -> None:
    async with make_server() as receiver:
        lab = {
            "rules": [
                {
                    "name": "new-order",
                    "match": {"type": "ORM^O01", "fields": {"ORC-1": "NW"}},
                    "action": {
                        "followup": {
                            "name": "result",
                            "host": "127.0.0.1",
                            "port": receiver.port,
                            "template": (
                                "MSH|^~\\&|ACME_LAB|ACME_LAB|{{ MSH-3 }}|{{ MSH-4 }}|{{ now }}||"
                                "ORU^R01^ORU_R01|{{ control_id }}|P|2.5.1\n"
                                "PID|1||{{ PID-3 }}||{{ PID-5 }}\n"
                                "OBR|1|{{ ORC-2 }}||{{ OBR-4 }}\n"
                            ),
                        }
                    },
                }
            ]
        }
        async with make_server(lab) as server:
            async with MllpClient("127.0.0.1", server.port) as client:
                result = await client.send(ORM_O01)
            assert result.accepted
            await server.followups.wait_idle()
            outbound = server.traffic.recent()[0]
        assert outbound.direction == "outbound"
        assert outbound.ack_code == "AA"
        assert outbound.message_type == "ORU^R01"
        received = Message.parse(receiver.traffic.recent()[0].message)
    assert received.message_type == "ORU^R01"
    assert received.get("MSH-5") == "ORDER_APP"
    assert received.get("PID-3.1") == "MRN0001"
    assert received.get("OBR-2") == "PLC0001"
    assert received.get("OBR-4.1") == "2345-7"


async def closed_port() -> int:
    """A port that was free a moment ago (nothing listens on it)."""
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    return port


async def test_failed_followup_is_recorded(make_server: ServerFactory) -> None:
    # The peer accepts and hangs up immediately: a deterministic failure (a merely
    # closed port could be handed straight back to the simulator by the OS).
    async def hang_up(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    peer = await asyncio.start_server(hang_up, "127.0.0.1", 0)
    scenario = {
        "default": {
            "followup": {
                "host": "127.0.0.1",
                "port": peer.sockets[0].getsockname()[1],
                "timeout_s": 2,
                "template": "MSH|^~\\&|A|B|C|D|{{now}}||ORU^R01|{{control_id}}|P|2.5",
            }
        }
    }
    try:
        async with make_server(scenario) as server:
            async with MllpClient("127.0.0.1", server.port) as client:
                assert (await client.send(ADT_A01)).accepted
            await server.followups.wait_idle()
            outbound = server.traffic.recent()[0]
    finally:
        peer.close()
        await peer.wait_closed()
    assert outbound.direction == "outbound"
    assert outbound.reply == "failed"
    assert outbound.error  # "closed the connection" or "connection reset", per platform


async def test_record_file_contains_json_lines(make_server: ServerFactory, tmp_path: Path) -> None:
    record = tmp_path / "out" / "traffic.jsonl"
    traffic = TrafficLog(history=10, record_path=record)
    async with (
        make_server(traffic=traffic) as server,
        MllpClient("127.0.0.1", server.port) as client,
    ):
        await client.send(ADT_A01)
        await client.send(with_control_id(ADT_A01, "SECOND"))
    traffic.close()
    lines = [json.loads(line) for line in record.read_text().splitlines()]
    assert [line["control_id"] for line in lines] == ["MSG00001", "SECOND"]
    assert lines[0]["ack_code"] == "AA"
    assert lines[0]["message"].startswith("MSH|")
    assert lines[0]["ack"].startswith("MSH|")


async def test_history_is_bounded(make_server: ServerFactory) -> None:
    traffic = TrafficLog(history=3)
    async with (
        make_server(traffic=traffic) as server,
        MllpClient("127.0.0.1", server.port) as client,
    ):
        for i in range(5):
            await client.send(with_control_id(ADT_A01, f"H{i}"))
    assert [e.control_id for e in traffic.recent()] == ["H4", "H3", "H2"]
    assert traffic.counts()["inbound"] == 5


async def test_close_with_open_connection(make_server: ServerFactory) -> None:
    async with make_server() as server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"\x0bMSH|partial")
        await writer.drain()
        await asyncio.sleep(0.05)
        assert server.active_connections == 1
    assert await asyncio.wait_for(reader.read(), 2) == b""
    writer.close()


async def test_warns_when_followup_targets_itself(
    make_server: ServerFactory, caplog: pytest.LogCaptureFixture
) -> None:
    async with make_server() as probe:
        port = probe.port
    scenario = {
        "server": {"port": port},
        "default": {
            "followup": {
                "host": "localhost",
                "port": port,
                "template": "MSH|^~\\&|A|B|C|D|{{now}}||ORU^R01|{{control_id}}|P|2.5",
            }
        },
    }
    with caplog.at_level(logging.WARNING, logger="mllp_sim.server"):
        async with make_server(scenario):
            pass
    assert "may trigger itself in a loop" in caplog.text
