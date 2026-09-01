"""The mandatory gate before publish. Sends a notification with the assembled
video + a summary, then PAUSES the run - approval/rejection comes from
outside the pipeline (Slack button, CLI command), never automatically.

If nothing happens before the manifest's timeout_s, the run does NOT
auto-publish. It stays parked in AWAITING_APPROVAL; `on_timeout: escalate`
in the manifest is a signal for whatever's polling run state (see
canary/README) to re-notify or page someone. Silence is never treated as
approval - that's enforced structurally: this executor has no code path
that returns a success ExecutorOutput at all.
"""
from __future__ import annotations

import json

import requests

from executors.base import AwaitingApproval, ExecutorOutput, StepContext


class HumanCheckpointExecutor:
    name = "human_checkpoint"

    def run(self, context: StepContext) -> ExecutorOutput:
        cfg = context.step_config
        webhook_url = cfg.get("notify_webhook")
        assembled = context.upstream.get("assembly")
        video_path = assembled.output_ref if assembled else "(no assembled video found)"
        script_step = context.upstream.get("script")
        script_preview = (script_step.data.get("script", "") if script_step else "")[:200]

        step_name = context.step_name or "review_gate"
        message = self._build_message(context.run_id, context.seed_topic, video_path, script_preview)
        payload = self._build_notification(context.run_id, step_name, message)

        if webhook_url:
            try:
                requests.post(webhook_url, json=payload, timeout=15)
            except requests.RequestException:
                # A failed notification must not crash the pipeline or silently
                # skip the gate - the run still parks in AWAITING_APPROVAL and
                # is visible to anyone polling run state, just without a push.
                pass

        # Always raises. This step never "succeeds" from inside this class -
        # only engine.approve(), called from outside by a human, can do that.
        raise AwaitingApproval()

    @staticmethod
    def _build_message(run_id: str, seed_topic: str, video_path: str, script_preview: str) -> str:
        return (
            f"Run `{run_id}` ready for review\n"
            f"Topic: {seed_topic}\n"
            f"Script preview: {script_preview}...\n"
            f"Video: {video_path}\n"
            f"Approve:  pipeline approve {run_id} review_gate\n"
            f"Reject:   pipeline reject {run_id} review_gate \"<reason>\""
        )

    @staticmethod
    def _build_notification(run_id: str, step_name: str, text: str) -> dict:
        """Full Slack message payload: the human-readable `text` plus a pair of
        interactive buttons whose `value` round-trips run_id/step_name to the
        callback receiver (webhook_server.py) when someone clicks one. Kept as
        a separate helper so it's unit-testable without HTTP."""
        value = json.dumps({"run_id": run_id, "step_name": step_name})
        return {
            "text": text,
            "attachments": [
                {
                    "fallback": f"Approve or reject run {run_id}",
                    "actions": [
                        {"type": "button", "text": "Approve", "style": "primary", "action_id": "approve", "value": value},
                        {"type": "button", "text": "Reject", "style": "danger", "action_id": "reject", "value": value},
                    ],
                }
            ],
        }
