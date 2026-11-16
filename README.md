# mllp-sim

**A scriptable HL7 v2 partner system in one YAML file.** Point your interface engine at it and
make it ACK, reject, stall, hang up or reply with results — exactly when you want it to.

[![CI](https://github.com/xlsarath/mllp-sim/actions/workflows/ci.yml/badge.svg)](https://github.com/xlsarath/mllp-sim/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](pyproject.toml)
[![Container](https://img.shields.io/badge/ghcr.io-xlsarath%2Fmllp--sim-2496ED?logo=docker&logoColor=white)](https://github.com/xlsarath/mllp-sim/pkgs/container/mllp-sim)

```console
$ mllp-sim serve --scenario examples/scenarios/chaos.yaml &
INFO    mllp_sim.server: MLLP server listening on 127.0.0.1:2575 scenario='chaos' rules=6

$ mllp-sim send examples/messages/adt_a01.hl7 examples/messages/adt_a08.hl7 --keep-going
-> #1 ADT^A01 control_id=MSG00001 (examples/messages/adt_a01.hl7)
<- AA control_id=MSG00001 31.1 ms
   MSH|^~\&|RECEIVING_APP|RECEIVING_FAC|SENDING_APP|SENDING_FAC|20260101093001+0000||ACK^A01^ACK|20260101093001000001|P|2.5.1
   MSA|AA|MSG00001
-> #2 ADT^A08 control_id=MSG00002 (examples/messages/adt_a08.hl7)
<- AE control_id=MSG00002 0.3 ms error='Patient not found'
   MSH|^~\&|RECEIVING_APP|RECEIVING_FAC|SENDING_APP|SENDING_FAC|20260101093001+0000||ACK^A08^ACK|20260101093001000002|P|2.5.1
   MSA|AE|MSG00002|Patient not found
   ERR|||204^Unknown key identifier^HL70357|E||||Patient not found
2 message(s): 1 accepted, 1 rejected, 0 failed

$ echo $?
1
```

## Quickstart

```bash
docker run --rm -p 2575:2575 -p 8080:8080 ghcr.io/xlsarath/mllp-sim
```

That is a working HL7 endpoint on port 2575 that answers every message with `AA`, plus a live
traffic page at <http://localhost:8080>. Bring your own scenario:

```bash
docker run --rm -p 2575:2575 -p 8080:8080 -v "$PWD/scenarios:/scenarios:ro" \
  ghcr.io/xlsarath/mllp-sim serve --scenario /scenarios/my-partner.yaml
```

Or install the CLI (Python 3.11+, one dependency: PyYAML):

```bash
pipx install git+https://github.com/xlsarath/mllp-sim
mllp-sim serve                                   # AA for everything on 127.0.0.1:2575
mllp-sim send examples/messages/orm_o01.hl7      # in another terminal
```

## Why this exists

Every HL7 v2 interface eventually has to cope with a partner that rejects a message, answers
slowly, drops the connection mid-conversation, or never answers at all. Those are the paths that
break in production, and they are the hardest to reproduce: the real partner's test system is
shared, rate-limited, behind a VPN, or simply always says `AA`.

`mllp-sim` stands in for that partner. You describe its behaviour in a short YAML file - which
messages to accept, which to reject and why, how slow to be, how often to misbehave - and it
plays that role on a TCP port, deterministically if you give it a seed. Because it is a single
process with a CLI and a container image, it fits in a laptop terminal, a `docker compose` stack
and a CI job equally well.

## Features

- **Correct ACKs out of the box.** Delimiters are read from the inbound `MSH`; the ACK swaps
  sending/receiving application and facility, echoes `MSH-11`/`MSH-12`, puts the inbound
  `MSH-10` in `MSA-2`, gets a fresh control id and timestamp, and `ERR` is laid out for the
  message's HL7 version (v2.5+ `ERR-3`/`ERR-4`/`ERR-8`, earlier `ERR-1`).
- **Rules** that match on message type (`ORM^O01`, `ADT^A0*`, `ORU`) and on any field path
  (`PID-3.1`, `OBR-4`, `OBX[2]-5`) with `equals`, `regex`, `in` or `exists`.
- **Actions**: `AA`/`AE`/`AR` (and `CA`/`CE`/`CR`) with error text and HL7 table 0357 codes,
  fixed or random delays, no reply, dropped connection, duplicate ACK, garbled framing.
- **Chaos with a dial**: any rule can fire for a percentage of matching messages; `--seed`
  makes a run reproducible.
- **Follow-up messages**: after an order, send a result (or anything else) to another
  endpoint, built from a template with `{{ PID-3 }}`, `{{ now }}`, `{{ uuid }}`, `{{ control_id }}`.
- **A CI-friendly client**: `mllp-sim send` sends one or many messages per file, prints the
  ACKs (or JSON), and exits non-zero on `AE`/`AR`, timeouts and connection failures.
- **Recording**: every message and its ACK as JSON Lines with `--record traffic.jsonl`.
- **Inspection**: `/healthz`, `/messages` and a small live traffic page over HTTP.
- **Robust MLLP framing**: frames split across TCP reads, several frames in one read, junk
  between frames, a missing trailing `CR`, a sender that restarts mid-frame, oversize frames.
  `\r`, `\n` and `\r\n` segment terminators are all accepted.
- **Strict scenario validation** with precise error locations (`rules[2].action.ack: must be
  one of AA, AE, AR, CA, CE, CR`), and `mllp-sim validate` to check files in CI.

## Examples

| File | What it shows |
| --- | --- |
| [`examples/scenarios/happy-path.yaml`](examples/scenarios/happy-path.yaml) | accept everything with `AA` |
| [`examples/scenarios/chaos.yaml`](examples/scenarios/chaos.yaml) | business errors plus slow, dropped, silent and duplicate replies |
| [`examples/scenarios/order-to-result.yaml`](examples/scenarios/order-to-result.yaml) | ACK an `ORM^O01`, then send an `ORU^R01` result built from the order |
| [`docker-compose.yml`](docker-compose.yml) | a "lab" and an "ehr" simulator wired together |
| [`examples/messages/`](examples/messages) | synthetic ADT, ORM and ORU messages, including a multi-message file |

## Scenario reference

A scenario has four optional parts. An empty file (or no file) means "listen on
127.0.0.1:2575 and answer everything with `AA`".

```yaml
name: my-partner               # shown in logs and on /healthz
description: What this partner does.
seed: 42                       # optional; makes probability and random delays reproducible

server:
  host: 127.0.0.1              # 0.0.0.0 to accept remote connections
  port: 2575
  http_port: 8080              # omit to disable the HTTP status endpoints
  encoding: utf-8              # wire encoding, e.g. latin-1 / cp1252 for legacy systems
  max_message_bytes: 16777216  # larger frames close the connection
  history: 500                 # exchanges kept in memory for /messages
  application: MLLP_SIM        # MSH-3 of ACKs when the inbound MSH-5 is empty

rules:                         # evaluated top to bottom; the first match wins
  - name: unknown-patient
    match:
      type: ADT^A08            # or a list: [ADT^A08, ADT^A31]
      fields:
        PID-3.1: { regex: "^TEST" }
    action:
      ack: AE
      error: Patient not found
      error_code: 204          # HL7 table 0357; defaults to 207

  - name: flaky-network
    probability: 10%           # or 0.1
    action:
      reply: drop

default:                       # used when no rule matches
  ack: AA
  delay_ms: [5, 50]
```

### Matching

| Key | Meaning |
| --- | --- |
| `type` | Glob pattern(s) for the message type. With `^` it is compared to `CODE^TRIGGER` (`ORM^O01`, `ADT^A0*`, `*^A08`); without, to the code only (`ORU`). |
| `fields` | Map of field path to condition. All must hold. |

| Condition | Example | True when |
| --- | --- | --- |
| *(plain value)* | `PV1-2: I` | the value equals the text exactly |
| `equals` | `MSH-11: { equals: P }` | same as above |
| `regex` | `PID-3.1: { regex: "^TEST" }` | the pattern is found (`re.search`; anchor with `^`/`$`) |
| `in` | `PV1-2: { in: [I, E] }` | the value is one of the options |
| `exists` | `OBR-4: { exists: true }` | the value is non-empty (`false`: empty or absent) |

Values are compared after HL7 escape sequences are decoded, so `\F\` matches `|`.

A rule matches when its criteria hold **and** its `probability` roll succeeds. If the roll
fails, evaluation continues with the next rule - so `20%` slow, then `5%` dropped, then the
default, behaves like you would expect.

### Field paths

| Path | Selects |
| --- | --- |
| `PID-3` | field 3 of the first PID (first repetition) |
| `PID-3.1` | component 1 |
| `PID-3.4.2` | sub-component 2 of component 4 |
| `PID-3[2].1` | component 1 of the second repetition |
| `OBX[2]-5` | field 5 of the second OBX segment |
| `MSH-9.2` | trigger event (`MSH-1` is the field separator, `MSH-2` the encoding characters) |

Indexes are 1-based. Missing segments, fields or components read as empty.

### Actions

| Key | Default | Meaning |
| --- | --- | --- |
| `ack` | `AA` | `AA`, `AE`, `AR` (original mode) or `CA`, `CE`, `CR` |
| `error` | - | Text for `MSA-3` and `ERR`; only used with a negative `ack` |
| `error_code` | `207` | HL7 table 0357 code, e.g. `100`, `101`, `103`, `200`, `204`, `206` |
| `delay_ms` | `0` | Wait before replying: a number, or `[min, max]` for a random delay |
| `reply` | `ack` | `ack`, `none` (stay silent; the sender times out), `drop` (close the connection), `duplicate` (send the ACK twice), `garbled` (send the ACK without a valid MLLP envelope) |
| `followup` | - | One follow-up or a list (below) |

Messages that cannot be parsed (no `MSH`, bad delimiters) are always answered with `AR`, error
code `100`, and an empty `MSA-2`. Inbound `ACK` messages are logged and never acknowledged.

### Follow-up messages

```yaml
rules:
  - name: new-order
    match:
      type: ORM^O01
      fields: { ORC-1: NW }
    action:
      ack: AA
      followup:
        name: result
        host: localhost
        port: 2576
        delay_ms: 2000           # after the ACK; never delays it
        timeout_s: 10            # wait for the receiver's ACK
        template_file: ../templates/oru_r01.hl7   # relative to the scenario file
        # or inline:  template: |
        #               MSH|^~\&|ACME_LAB|...
```

Template placeholders:

| Placeholder | Value |
| --- | --- |
| `{{ PID-3 }}`, `{{ OBR-4.1 }}`, ... | the value from the inbound message, structure preserved and re-encoded for the template's delimiters |
| `{{ now }}` | current time, `YYYYMMDDHHMMSS+ZZZZ` |
| `{{ uuid }}` | a random UUID |
| `{{ control_id }}` | a fresh message control id (the same within one message) |

Unknown placeholders are rejected when the scenario loads, not when the first order arrives.
Follow-ups run in the background; their outcome is logged and recorded like any other exchange.

## CLI reference

```text
mllp-sim serve    [-s SCENARIO] [--host H] [-p PORT] [--http-port PORT]
                  [--record FILE] [--seed N] [--log-level LEVEL | -v]
mllp-sim send     FILE... [--host H] [-p PORT] [-t SECONDS] [--encoding ENC]
                  [-k/--keep-going] [--json | -q/--quiet]
mllp-sim validate SCENARIO...
mllp-sim --version
```

**`serve`** settings are resolved as: command-line flag, then environment variable
(`MLLP_SIM_HOST`, `MLLP_SIM_PORT`, `MLLP_SIM_HTTP_PORT`), then the scenario file, then defaults.
The container image sets `MLLP_SIM_HOST=0.0.0.0` and `MLLP_SIM_HTTP_PORT=8080`.
`SIGINT`/`SIGTERM` shut down cleanly.

**`send`** reads one or more files (`-` for stdin). Each file may contain several messages; a new
message starts at every `MSH` segment, blank lines are ignored, and batch envelopes
(`FHS`/`BHS`/`BTS`/`FTS`) are skipped. All messages go over one connection, one at a time. It
stops at the first failure unless `--keep-going` is given (after a dropped connection it
reconnects). If a late duplicate ACK arrives for an earlier message, it is recognised and
skipped rather than mistaken for the next ACK.

| Exit status | Meaning |
| --- | --- |
| `0` | every message was accepted (`AA`/`CA`) |
| `1` | at least one negative or unparseable ACK |
| `2` | usage or input error (bad file, invalid scenario) |
| `3` | timeout, connection refused or connection closed |

**`validate`** loads scenario files exactly as `serve` would and prints the rules it found.

## HTTP endpoints

Enabled with `--http-port` (or `server.http_port`):

| Route | Returns |
| --- | --- |
| `GET /healthz` | `{"status": "ok", "version", "scenario", "uptime_s", "counts"}` |
| `GET /messages?limit=50` | the most recent exchanges, newest first |
| `GET /` | a self-contained page that polls `/messages`; click a row for the full message and ACK |

Each exchange looks like this (the same shape is written by `--record`):

```json
{
  "id": 2, "timestamp": "2026-01-01T09:30:01.123+00:00", "direction": "inbound",
  "peer": "127.0.0.1:53833", "message_type": "ADT^A08", "control_id": "MSG00002",
  "rule": "unknown-patient", "reply": "ack", "ack_code": "AE", "latency_ms": 0.31,
  "message": "MSH|^~\\&|SENDING_APP|...", "ack": "MSH|^~\\&|RECEIVING_APP|...", "error": null
}
```

`direction` is `outbound` for follow-ups, where `ack` is what the other side answered.

## Use in CI

As a stand-in partner for integration tests (GitHub Actions):

```yaml
jobs:
  integration:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Start the simulated partner
        run: |
          docker run -d --name partner -p 2575:2575 -p 8080:8080 \
            -v "$PWD/test/scenarios:/scenarios:ro" \
            ghcr.io/xlsarath/mllp-sim serve --scenario /scenarios/partner.yaml --seed 1
          until curl -fsS localhost:8080/healthz; do sleep 0.5; done
      - run: ./run-your-integration-tests.sh   # point them at localhost:2575
      - name: Assert what the partner received
        run: curl -fsS 'localhost:8080/messages?limit=500' > partner-traffic.json
```

As a smoke test for your own listener, where the exit status fails the job:

```bash
mllp-sim send test/messages/*.hl7 --host my-listener --port 6661 --timeout 5 --json > acks.jsonl
```

And keep scenario files honest:

```bash
mllp-sim validate test/scenarios/*.yaml
```

## How it compares

There are mature tools in this space; they focus on different jobs.

- **HAPI TestPanel** is a Java desktop application from the HAPI HL7v2 project for composing,
  sending and receiving messages interactively.
- **7Edit** is a commercial desktop HL7 editor and viewer that can also send and receive
  messages over MLLP.
- **Integration engines such as Mirth Connect** can be configured to act as a test endpoint,
  and are the right choice when you need real routing and transformation.
- **Libraries** such as python-hl7 and HAPI provide parsers and MLLP clients for writing your
  own test harness.

`mllp-sim` is narrower: headless, configured entirely by a version-controllable YAML file, built
for scripted failure modes (rejections, delays, drops, silence, duplicates, chaos percentages),
and packaged to run the same way on a laptop, in `docker compose` and in CI. It does not try to
be a message editor, a validator against HL7 conformance profiles, or an integration engine.

## Roadmap

- Publish to PyPI (`pip install mllp-sim`).
- Enhanced acknowledgement mode: honour `MSH-15`/`MSH-16` with separate commit and application ACKs.
- "Any repetition" field matching (`PID-3[*].1`) and negated conditions.
- Batch protocol (`FHS`/`BHS`) handling on the server side.
- TLS for MLLP connections.
- Scheduled outbound messages (send an `ADT` feed every N seconds without an inbound trigger).
- Hot reload of the scenario file.

Ideas and votes are welcome in the issues.

## Contributing

Bug reports, scenarios that broke your interface (with synthetic data), and pull requests are
all welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup; the short
version is:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest && ruff check . && ruff format --check .
```

All examples and test fixtures use synthetic data only (`DOE^JANE`, `TEST^PATIENT`,
`ACME_LAB`). Please keep it that way: never put real patient information in an issue, a test or
a recording you share.

## License

[MIT](LICENSE)
