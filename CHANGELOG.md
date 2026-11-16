# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-16

First public release.

### Added

- MLLP server driven by a YAML scenario: rules matched on message type globs and field
  paths (`equals`, `regex`, `in`, `exists`), first match wins, optional probability per rule.
- Actions: `AA`/`AE`/`AR` and `CA`/`CE`/`CR` acknowledgements with error text and HL7 table
  0357 codes; fixed or random delays; `none`, `drop`, `duplicate` and `garbled` replies.
- ACK builder that mirrors the inbound header and writes `ERR` in the layout of the
  message's HL7 version.
- Follow-up messages rendered from templates (`{{ PID-3 }}`, `{{ now }}`, `{{ uuid }}`,
  `{{ control_id }}`) and sent to another endpoint after a rule fires.
- `mllp-sim send`: multi-message files, stdin, JSON output, `--keep-going`, and exit codes
  for negative ACKs (1), input errors (2) and transport failures (3).
- `mllp-sim validate` for scenario files, with precise error locations.
- JSON Lines recording (`--record`), `/healthz`, `/messages` and a live traffic page.
- Incremental MLLP decoder tolerant of split frames, junk bytes, missing trailing `CR` and
  restarted frames, with a frame size limit.
- Container image (non-root, health check), `docker compose` example, CI and release
  workflows.

[Unreleased]: https://github.com/xlsarath/mllp-sim/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/xlsarath/mllp-sim/releases/tag/v0.1.0
