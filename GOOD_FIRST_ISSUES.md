# Good first issues

Five well-scoped starter issues, ready to be filed. Each lists where to look and what "done"
means. Suggested labels: `good first issue`, plus the one noted.

---

## 1. Match any repetition of a field with `[*]`

**Label:** `enhancement`

Today `PID-3.1` reads the first repetition of PID-3 only. Patient identifiers are often
repeated (`MRN0001^^^A^MR~ALT0001^^^B^PI`), so a rule that wants "any identifier starting with
`TEST`" cannot be written.

**Proposal:** allow `*` as a repetition index in rule field paths, e.g.
`PID-3[*].1: { regex: "^TEST" }`. The condition holds if it holds for at least one repetition.

**Where to look:** `FieldPath` and `Message.get_raw` in `src/mllp_sim/hl7.py`,
`FieldMatcher` in `src/mllp_sim/rules.py`.

**Done when:**
- `FieldPath.parse("PID-3[*].1")` works and `str()` round-trips it;
- matching tests cover one, several and zero repetitions;
- templates reject `[*]` with a clear error (a template needs a single value);
- README "Field paths" table documents it.

---

## 2. Negated conditions: `not_equals`, `not_regex`, `not_in`

**Label:** `enhancement`

Some partners reject everything *except* a known list, e.g. "AR unless MSH-11 is `P`".
That is awkward to express with only positive conditions.

**Proposal:** add `not_equals`, `not_regex` and `not_in` operators.

**Where to look:** `_condition` in `src/mllp_sim/scenario.py`; condition classes in
`src/mllp_sim/rules.py` (`Equals`, `Matches`, `OneOf` - a small `Not` wrapper may be the
neatest approach).

**Done when:** the operators load, validate and match; invalid values give located errors;
`tests/test_rules.py` and `tests/test_scenario.py` cover them; README tables list them.

---

## 3. `mllp-sim send --interval-ms` to pace messages

**Label:** `enhancement`

When replaying a file of hundreds of messages into a system under test, sending them back to
back can overload it or make logs hard to follow.

**Proposal:** an `--interval-ms N` option that waits N milliseconds between messages (not
before the first one).

**Where to look:** `_send_all` and the `send` parser in `src/mllp_sim/cli.py`.

**Done when:** the option exists with help text, negative values are rejected, a test proves
the pacing (measure elapsed time with a generous bound), and the README CLI reference lists it.

---

## 4. Filter `/messages` by direction, type and ACK code

**Label:** `enhancement`

`GET /messages` returns the most recent exchanges. In CI you often want "all ORU^R01 we sent"
or "every AE".

**Proposal:** optional query parameters `direction=inbound|outbound`, `type=ORU^R01` (exact)
and `ack=AE`, combinable with `limit`. Unknown `direction` values return 400.

**Where to look:** `StatusServer._messages` in `src/mllp_sim/web.py`; `TrafficLog.recent` in
`src/mllp_sim/traffic.py`.

**Done when:** filters work together, `limit` applies after filtering, `tests/test_web.py`
covers each filter and the 400 case, and the README "HTTP endpoints" section documents them.
Remember that `^` arrives URL-encoded as `%5E`.

---

## 5. Example scenario: an ADT feed receiver with realistic business rules

**Label:** `documentation`

The examples cover the happy path, chaos and order-to-result. A fourth example modelled on a
typical ADT receiver would help new users see rules composed together.

**Proposal:** `examples/scenarios/adt-receiver.yaml` that, for example:
accepts A01/A04/A08; answers A03 for an unknown visit (`PV1-19` empty) with `AE` and code 101;
rejects anything with `MSH-11` other than `P`/`T` with `AR` and code 202; and is slow (500 ms)
for 10% of A08s. Add matching sample messages under `examples/messages/` - **synthetic data
only** (`DOE^JANE`, `TEST^PATIENT`, fake MRNs, `SENDING_APP`).

**Where to look:** existing files in `examples/`; `tests/test_scenario.py` already validates
every example automatically.

**Done when:** the scenario validates, a short comment block at its top explains how to try
it, and the README mentions it next to the other examples.
