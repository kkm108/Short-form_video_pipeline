"""Manifest loading + validation.

The manifest is the single source of truth for what a run does: step order,
executor, timeout, and retry policy. Nothing about control flow lives in
code - only the *how* of each step lives in the executor classes. This is
what makes the pipeline a deterministic state machine instead of an agent
improvising what to do next.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


class ManifestError(ValueError):
    """Raised when a manifest is missing required fields or malformed."""


@dataclass
class RetryPolicy:
    max_attempts: int = 1
    backoff: str = "none"  # "none" | "fixed" | "exponential"
    base_delay_s: float = 2.0
    retry_on: list[int] = field(default_factory=list)  # HTTP-style codes; empty = retry any ExecutorError


@dataclass
class StepSpec:
    name: str
    executor: str
    timeout_s: int = 300
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    config: dict[str, Any] = field(default_factory=dict)  # everything executor-specific
    on_timeout: Optional[str] = None  # e.g. "escalate" - used by human_checkpoint


@dataclass
class Manifest:
    platforms: list[str]
    steps: list[StepSpec]
    path: str

    def step(self, name: str) -> StepSpec:
        for s in self.steps:
            if s.name == name:
                return s
        raise ManifestError(f"No step named {name!r} in manifest")


_REQUIRED_RUN_KEYS = {"platforms"}
_REQUIRED_STEP_KEYS = {"name", "executor"}
_KNOWN_STEP_KEYS = {"name", "executor", "timeout_s", "retry", "on_timeout"}


def load_manifest(path: str) -> Manifest:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict) or "run" not in raw or "steps" not in raw:
        raise ManifestError(f"{path}: manifest must have top-level 'run' and 'steps' keys")

    run_cfg = raw["run"] or {}
    missing = _REQUIRED_RUN_KEYS - set(run_cfg)
    if missing:
        raise ManifestError(f"{path}: run block missing keys: {sorted(missing)}")

    steps: list[StepSpec] = []
    seen_names: set[str] = set()
    for i, raw_step in enumerate(raw["steps"] or []):
        missing = _REQUIRED_STEP_KEYS - set(raw_step)
        if missing:
            raise ManifestError(f"{path}: steps[{i}] missing keys: {sorted(missing)}")

        name = raw_step["name"]
        if name in seen_names:
            raise ManifestError(f"{path}: duplicate step name {name!r}")
        seen_names.add(name)

        retry_cfg = raw_step.get("retry") or {}
        retry = RetryPolicy(
            max_attempts=int(retry_cfg.get("max_attempts", 1)),
            backoff=retry_cfg.get("backoff", "none"),
            base_delay_s=float(retry_cfg.get("base_delay_s", 2.0)),
            retry_on=list(retry_cfg.get("retry_on", [])),
        )
        config = {k: v for k, v in raw_step.items() if k not in _KNOWN_STEP_KEYS}

        steps.append(
            StepSpec(
                name=name,
                executor=raw_step["executor"],
                timeout_s=int(raw_step.get("timeout_s", 300)),
                retry=retry,
                config=config,
                on_timeout=raw_step.get("on_timeout"),
            )
        )

    if not steps:
        raise ManifestError(f"{path}: manifest has no steps")

    return Manifest(platforms=list(run_cfg["platforms"]), steps=steps, path=path)
