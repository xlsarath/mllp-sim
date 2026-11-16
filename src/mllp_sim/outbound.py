"""Follow-up messages: send a templated message elsewhere after a rule fires."""

from __future__ import annotations

import asyncio
import logging
import random

from mllp_sim.client import MllpClient
from mllp_sim.framing import MLLPError
from mllp_sim.hl7 import ControlIdGenerator, HL7ParseError, Message
from mllp_sim.rules import FollowUp
from mllp_sim.templating import RenderContext
from mllp_sim.traffic import TrafficLog

log = logging.getLogger(__name__)


class FollowUpDispatcher:
    """Runs follow-ups in the background so they never delay the ACK.

    Each follow-up opens its own connection, sends one message and waits for
    its ACK. Failures are logged and recorded; they never affect the inbound
    connection. Pending follow-ups are cancelled on :meth:`aclose`.
    """

    def __init__(
        self,
        traffic: TrafficLog,
        control_ids: ControlIdGenerator,
        *,
        rng: random.Random,
        encoding: str = "utf-8",
    ) -> None:
        self._traffic = traffic
        self._control_ids = control_ids
        self._rng = rng
        self._encoding = encoding
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def dispatch(self, followup: FollowUp, inbound: Message, rule: str) -> None:
        task = asyncio.create_task(
            self._run(followup, inbound, rule), name=f"followup:{rule}:{followup.name}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Wait until every dispatched follow-up has finished (handy in tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(self, followup: FollowUp, inbound: Message, rule: str) -> None:
        if followup.delay:
            await asyncio.sleep(followup.delay.sample_seconds(self._rng))
        target = f"{followup.host}:{followup.port}"
        text = followup.template.render(
            RenderContext(inbound=inbound, new_control_id=self._control_ids.next)
        )
        try:
            rendered: Message | None = Message.parse(text)
        except HL7ParseError:
            rendered = None
        message_type = rendered.message_type if rendered else None
        control_id = rendered.control_id if rendered else None
        context = (
            f"followup={followup.name} rule={rule} target={target} "
            f"type={message_type} control_id={control_id} "
            f"in_reply_to={inbound.control_id}"
        )
        log.debug("follow-up body %s:\n%s", context, text.replace("\r", "\n"))

        ack_text: str | None = None
        ack_code: str | None = None
        latency: float | None = None
        error: str | None = None
        try:
            async with MllpClient(
                followup.host, followup.port, timeout=followup.timeout_s, encoding=self._encoding
            ) as client:
                result = await client.send(text)
            ack_text, ack_code, latency = result.text, result.code, result.latency_ms
            if result.accepted:
                log.info("follow-up sent %s ack=%s latency_ms=%.1f", context, ack_code, latency)
            else:
                error = result.error_text or "negative or unparseable acknowledgement"
                log.warning("follow-up rejected %s ack=%s error=%r", context, ack_code, error)
        except (MLLPError, OSError) as exc:
            error = str(exc) or type(exc).__name__
            log.warning("follow-up failed %s error=%r", context, error)

        self._traffic.add(
            direction="outbound",
            peer=target,
            message_type=message_type,
            control_id=control_id,
            rule=rule,
            reply="sent" if ack_text is not None else "failed",
            ack_code=ack_code,
            latency_ms=latency,
            message=text,
            ack=ack_text,
            error=error,
        )
