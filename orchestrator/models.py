"""Core data models for the pipeline orchestrator."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"


@dataclass
class StepResult:
    run_id: str
    step_name: str
    status: StepStatus
    attempt: int = 1
    output_ref: Optional[str] = None  # path or key pointing at this step's output
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


@dataclass
class RunState:
    run_id: str
    seed_topic: str
    platforms: list[str]
    manifest_path: str
    created_at: float = field(default_factory=time.time)
    steps: dict[str, StepResult] = field(default_factory=dict)

    @staticmethod
    def new(seed_topic: str, platforms: list[str], manifest_path: str) -> "RunState":
        return RunState(
            run_id=f"run_{uuid.uuid4().hex[:12]}",
            seed_topic=seed_topic,
            platforms=platforms,
            manifest_path=manifest_path,
        )
