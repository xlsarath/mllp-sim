# Contributing to mllp-sim

Thanks for taking the time to help. This project aims to stay small, correct and pleasant to
read; contributions that keep it that way are very welcome.

## Ground rules

- **Synthetic data only.** Examples, tests, fixtures, issues and recordings must never contain
  real patient information. Use obviously fake values such as `DOE^JANE`, `TEST^PATIENT`,
  `MRN0001`, `SENDING_APP` or `ACME_LAB`.
- **Write from the specification.** Behaviour should follow the public HL7 v2 standard. When
  the standard leaves room, prefer being lenient in what we accept and strict in what we send,
  and document the choice.
- **Be kind** in issues and reviews.

## Development setup

```bash
git clone https://github.com/xlsarath/mllp-sim
cd mllp-sim
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Run the checks CI runs:

```bash
pytest --cov            # unit and end-to-end tests
ruff check .            # lint
ruff format --check .   # formatting (use `ruff format .` to fix)
mllp-sim validate examples/scenarios/*.yaml examples/compose/*.yaml
docker build -t mllp-sim:dev .
```

## Project layout

| Module | Responsibility |
| --- | --- |
| `hl7.py` | ER7 parsing, delimiters, escapes, field paths, control ids |
| `framing.py` | MLLP envelope encoding and the incremental decoder |
| `ack.py` | building ACK messages |
| `rules.py` | conditions, matchers, actions and the rule engine |
| `scenario.py` | loading and validating scenario YAML into the rule model |
| `templating.py` | follow-up message templates |
| `server.py` | the asyncio MLLP server |
| `client.py` | the MLLP client and file splitting used by `send` |
| `outbound.py` | background follow-up delivery |
| `traffic.py` | in-memory history and JSON Lines recording |
| `web.py` | `/healthz`, `/messages` and the status page |
| `cli.py` | argument parsing and the `serve`, `send`, `validate` commands |

## Making a change

1. Open an issue first for anything larger than a small fix, so we can agree on the shape.
2. Add or update tests. Protocol behaviour should have an end-to-end test in
   `tests/test_e2e.py`; parsing and rules have focused unit tests.
3. Update `README.md` when user-visible behaviour changes, and add a line under
   `[Unreleased]` in `CHANGELOG.md`.
4. Keep logging useful and safe: identifiers, counts and outcomes at `INFO`, message bodies
   only at `DEBUG`.
5. Use clear commit messages in the imperative mood ("Add negated field conditions").

## Releasing (maintainers)

1. Move the `[Unreleased]` notes to a new version section in `CHANGELOG.md` and bump
   `__version__` in `src/mllp_sim/__init__.py`.
2. Tag `vX.Y.Z` and push the tag. The release workflow publishes
   `ghcr.io/xlsarath/mllp-sim:X.Y.Z` (amd64 and arm64) and a GitHub release with the wheel and
   sdist.
