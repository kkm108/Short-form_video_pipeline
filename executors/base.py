"""Executor interface every pipeline step implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


class ExecutorError(Exception):
    """Raised by an executor on a failure that should count against its retry
    budget. `status_code` lets the engine match against a step's `retry_on`
    list; `retryable=False` short-circuits retries entirely (e.g. missing
    config - retrying won't fix that)."""

    def __init__(self, message: str, *, retryable: bool = True, status_code: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class AwaitingApproval(Exception):
    """Raised by an executor (specifically human_checkpoint) to pause the run
    without counting as a failure. The engine catches this and parks the run
    in AWAITING_APPROVAL until something outside the pipeline calls approve()
    or reject()."""


@dataclass
class ExecutorOutput:
    output_ref: str  # path or identifier the next step (or a human) can consume
    data: dict[str, Any] = field(default_factory=dict)  # structured payload for the next step's context


class Executor(ABC):
    name: str

    @abstractmethod
    def run(self, context: "StepContext") -> ExecutorOutput:
        """Do the work for one step. Raise ExecutorError on failure,
        AwaitingApproval to pause for a human."""


class StepExecutor(Protocol):
    """Structural type for a pipeline step's executor. The concrete executors
    (llm, llm_chain, ffmpeg, etc.) are duck-typed: they implement `name` and
    `run()` but don't all inherit from the Executor ABC. This Protocol lets
    the engine and CLI hold a heterogeneous mix in one mapping (dict[str,
    StepExecutor]) without forcing every class onto the nominal ABC."""

    name: str

    def run(self, context: "StepContext") -> ExecutorOutput: ...


@dataclass
class StepContext:
    run_id: str
    seed_topic: str
    platforms: list[str]
    step_config: dict[str, Any]
    upstream: dict[str, ExecutorOutput]  # every prior step's output, keyed by step name
    workdir: str
    # The manifest step name this executor is running as. Empty when a
    # StepContext is built by hand (e.g. in tests); the engine always sets it
    # so executors that need their own step name (review_gate's Slack button
    # value) can read it back.
    step_name: str = ""
