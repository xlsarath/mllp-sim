"""Traffic history: an in-memory ring buffer plus an optional JSON Lines recording."""

from __future__ import annotations

import itertools
import json
import logging
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

log = logging.getLogger(__name__)

Direction = Literal["inbound", "outbound"]


@dataclass(frozen=True, slots=True)
class Exchange:
    """One message and what happened to it.

    ``inbound`` exchanges are messages the server received (``ack`` is what it
    sent back); ``outbound`` exchanges are follow-up messages it sent (``ack``
    is what the peer answered).
    """

    id: int
    timestamp: str
    direction: Direction
    peer: str
    message_type: str | None
    control_id: str | None
    rule: str | None
    reply: str
    ack_code: str | None
    latency_ms: float | None
    message: str
    ack: str | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TrafficLog:
    """Keeps the last ``history`` exchanges and optionally appends each to a file."""

    def __init__(self, history: int = 500, record_path: str | Path | None = None) -> None:
        self._recent: deque[Exchange] = deque(maxlen=history)
        self._ids = itertools.count(1)
        self._counts: Counter[str] = Counter()
        self._record: TextIO | None = None
        self.record_path = Path(record_path) if record_path else None
        if self.record_path:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            # Line-buffered so a crash or kill loses at most the line being written.
            self._record = self.record_path.open("a", encoding="utf-8", buffering=1)
        self.started_at = datetime.now(UTC)

    def add(self, **fields: Any) -> Exchange:
        """Create, store and (if recording) persist an :class:`Exchange`."""
        exchange = Exchange(
            id=next(self._ids),
            timestamp=datetime.now(UTC).isoformat(timespec="milliseconds"),
            **fields,
        )
        self._recent.append(exchange)
        self._counts[exchange.direction] += 1
        # e.g. "inbound:AA", "inbound:drop", "outbound:failed"
        self._counts[f"{exchange.direction}:{exchange.ack_code or exchange.reply}"] += 1
        if self._record is not None:
            try:
                self._record.write(json.dumps(exchange.to_dict(), ensure_ascii=False) + "\n")
            except OSError:
                log.exception("recording failed; disabling record file %s", self.record_path)
                self._close_record()
        return exchange

    def recent(self, limit: int | None = None) -> list[Exchange]:
        """Most recent first."""
        items = list(reversed(self._recent))
        return items if limit is None else items[:limit]

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def close(self) -> None:
        self._close_record()

    def _close_record(self) -> None:
        if self._record is not None:
            self._record.close()
            self._record = None
