"""Fail-closed state machine for one Go2 audiohub upload transaction."""
from __future__ import annotations

from dataclasses import dataclass
import math

from .speaker_protocol import AudioHubPlan, AudioHubRequest


class AudioHubTransactionError(RuntimeError):
    """The speaker transaction is ambiguous and must not continue."""


@dataclass(frozen=True)
class PendingRequest:
    identity: int
    api_id: int
    parameter: str


class AudioHubTransaction:
    """Serialize requests and require an exact successful response for each."""

    def __init__(self, *, timeout_s: float = 2.0, identity_seed: int = 1) -> None:
        if not 0.1 <= float(timeout_s) <= 10.0 or not math.isfinite(timeout_s):
            raise ValueError("audiohub timeout_s must be finite and in [0.1, 10]")
        if int(identity_seed) < 0:
            raise ValueError("identity_seed must be non-negative")
        self.timeout_s = float(timeout_s)
        self._identity = int(identity_seed)
        self._requests: tuple[AudioHubRequest, ...] = ()
        self._next_index = 0
        self._inflight: PendingRequest | None = None
        self._inflight_since: float | None = None
        self._latched_fault: str | None = None
        self.completed_total = 0
        self.responses_ok_total = 0

    @property
    def busy(self) -> bool:
        return bool(self._requests) or self._inflight is not None

    @property
    def latched_fault(self) -> str | None:
        return self._latched_fault

    @property
    def inflight_identity(self) -> int | None:
        return self._inflight.identity if self._inflight else None

    def submit(self, plan: AudioHubPlan) -> None:
        if self._latched_fault:
            raise AudioHubTransactionError(
                f"audiohub transaction is latched fail-closed: {self._latched_fault}")
        if self.busy:
            raise AudioHubTransactionError("another audiohub upload is already active")
        if not plan.requests or plan.requests[0].api_id != 4001:
            raise AudioHubTransactionError("audiohub plan does not begin with API 4001")
        if any(item.api_id != 4003 for item in plan.requests[1:]):
            raise AudioHubTransactionError("audiohub plan contains an unreviewed API ID")
        self._requests = tuple(plan.requests)
        self._next_index = 0

    def dispatch_next(self, now_steady_s: float) -> PendingRequest | None:
        now = self._time(now_steady_s)
        if self._latched_fault or self._inflight is not None or not self._requests:
            return None
        if self._next_index >= len(self._requests):
            self._finish()
            return None
        self._identity += 1
        source = self._requests[self._next_index]
        self._inflight = PendingRequest(
            identity=self._identity,
            api_id=source.api_id,
            parameter=source.parameter,
        )
        self._inflight_since = now
        return self._inflight

    def acknowledge(self, *, identity: int, api_id: int, status: int) -> None:
        if self._inflight is None:
            return
        if int(identity) != self._inflight.identity:
            return
        if int(api_id) != self._inflight.api_id:
            self._latch(
                f"audiohub response API ID {api_id} did not match "
                f"request {self._inflight.api_id}")
            return
        if int(status) != 0:
            self._latch(f"audiohub API {self._inflight.api_id} returned status {status}")
            return
        self.responses_ok_total += 1
        self._next_index += 1
        self._inflight = None
        self._inflight_since = None
        if self._next_index == len(self._requests):
            self._finish()

    def check_timeout(self, now_steady_s: float) -> None:
        now = self._time(now_steady_s)
        if self._inflight is None:
            return
        assert self._inflight_since is not None
        if now < self._inflight_since:
            self._latch("audiohub steady clock moved backwards")
        elif now - self._inflight_since > self.timeout_s:
            self._latch(f"audiohub API {self._inflight.api_id} response timed out")

    def _finish(self) -> None:
        self.completed_total += 1
        self._requests = ()
        self._next_index = 0
        self._inflight = None
        self._inflight_since = None

    def _latch(self, reason: str) -> None:
        self._latched_fault = str(reason)[:256]
        self._requests = ()
        self._next_index = 0
        self._inflight = None
        self._inflight_since = None

    @staticmethod
    def _time(value: float) -> float:
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise AudioHubTransactionError("audiohub steady time is invalid")
        return result
