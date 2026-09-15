"""The sole automatic retry owner: inference transport, never the Harness/Slurm."""

from dataclasses import dataclass
import math
import os
import time

from .model_client import ModelErrorCode, ModelUnavailableError
from .prepare_errors import is_retryable_model_error
from .request_trace import attempt_scope, emit, phase, request_scope, set_model


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 2.0
    total_timeout_seconds: float = 90.0

    def __post_init__(self):
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 2:
            raise ValueError("AI model attempts must be 1 or 2")
        for value in (self.initial_backoff_seconds, self.max_backoff_seconds, self.total_timeout_seconds):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError("Invalid model retry policy")
        if not (0 <= self.initial_backoff_seconds <= self.max_backoff_seconds <= 5
                and 0 < self.total_timeout_seconds <= 120):
            raise ValueError("Invalid model retry policy bounds")

    @classmethod
    def from_env(cls):
        try:
            return cls(int(os.environ.get("SBATCH_AGENT_AI_MAX_ATTEMPTS", "2")),
                       float(os.environ.get("SBATCH_AGENT_AI_INITIAL_BACKOFF", "1")),
                       float(os.environ.get("SBATCH_AGENT_AI_MAX_BACKOFF", "2")),
                       float(os.environ.get("SBATCH_AGENT_AI_TOTAL_TIMEOUT", "90")))
        except (ValueError, TypeError):
            raise ModelUnavailableError(code=ModelErrorCode.INVALID_CONFIG) from None


class RetryingModelClient:
    """One prompt/context build, at most two *raw* transport attempts.

    Both direct and remote clients remain single-attempt primitives. In
    particular the laptop relay never wraps its DeepSeek client with retries.
    """
    def __init__(self, client, *, policy=None, sleeper=time.sleep, clock=time.monotonic):
        self.client = client
        self.provider, self.model = client.provider, client.model
        self.policy = policy if policy is not None else RetryPolicy.from_env()
        self.sleeper, self.clock = sleeper, clock

    def generate_structured(self, *, context, schema):
        with request_scope():
            set_model(self.client)
            with phase("model"):
                deadline = self.clock() + self.policy.total_timeout_seconds
                for attempt in range(1, self.policy.max_attempts + 1):
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise ModelUnavailableError(code=ModelErrorCode.TIMEOUT, failure_kind="overall_timeout")
                    with attempt_scope(attempt, remaining, clock=self.clock):
                        try:
                            with phase("model_attempt"):
                                result = self.client.generate_structured(context=context, schema=schema)
                                if self.clock() >= deadline:
                                    raise ModelUnavailableError(code=ModelErrorCode.TIMEOUT, failure_kind="overall_timeout")
                                return result
                        except ModelUnavailableError as exc:
                            delay = min(self.policy.initial_backoff_seconds * 2 ** (attempt - 1),
                                        self.policy.max_backoff_seconds)
                            if (not is_retryable_model_error(exc) or attempt == self.policy.max_attempts
                                    or deadline - self.clock() <= delay):
                                raise
                            emit("retry_backoff", status="retry", duration_ms=delay * 1000, exc=exc)
                            self.sleeper(delay)
