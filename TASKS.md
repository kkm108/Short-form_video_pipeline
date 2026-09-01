## 1. CI pipeline

Add a GitHub Actions workflow that runs the full test suite and validates
every manifest on push and PR. It should reproduce exactly what AGENTS.md's
"whole suite" command does locally - if a test passes locally and fails in
CI (or vice versa), that's a bug in the workflow, not the tests.

Acceptance: workflow file added under `.github/workflows/`; every test
module runs individually (not swallowed into one opaque step, so a failure
points at the specific module); manifest validation runs as its own step;
you've confirmed the commands in the workflow file match what AGENTS.md
documents.

---

## 2. Lint and type-check pass

Add `ruff` and `mypy` (or explain in your summary why not, if you find a
better-fit tool already implied by the codebase) and get both clean. Wire
them into the CI workflow from Task 1 if it exists yet.

Acceptance: `ruff check .` and `mypy .` both exit clean; full test suite
still green after any changes; any suppressions are narrow and commented
with why, not blanket-disabled.

---

## 3. YouTube upload-loop test coverage

`executors/publishers/youtube.py`'s credential-handling is tested
end-to-end (`tests/test_youtube_publisher.py`), but the actual upload loop
- `youtube.videos().insert(...)` and the `next_chunk()` polling loop - has
never been exercised, mocked or otherwise. Every other publisher's HTTP
flow has a real mocked round trip; this is the one gap left in that
pattern.

Acceptance: new test(s) using `unittest.mock` (consistent with this file's
existing `build_client` injection point) that exercise a successful
multi-chunk upload and a simulated `HttpError` mid-upload, asserting the
resulting `ExecutorError`'s `status_code`/`retryable` are correct. Full
suite green.

---

## 4. Slack webhook receiver for review_gate

Right now `review_gate` approvals only happen via `cli.py approve <run_id>
review_gate` typed by hand. Build a small HTTP endpoint that receives a
Slack interactive-message callback (button click) and calls the same
`orchestrator.engine.approve()`/`reject()` functions the CLI does - an
additional way in, not a replacement for the CLI path.

Acceptance: new module (your choice of a stdlib `http.server` handler or a
lightweight framework, consistent with this project's minimal-dependency
style); a test that posts a realistic fake Slack payload at the endpoint
and asserts the target run's state actually changes; signature verification
noted as a TODO with a clear comment if not implemented, not silently
skipped; README updated with setup steps.

---

## 5. Secret-safe logging audit

Every `logger.*` call in the codebase should be safe to run with
`DEBUG`-level logging on and paste into a bug report. Audit every one
(`orchestrator/engine.py`, all `executors/`, `credentials/vault.py`) for
any path where a credential, token, or session value could end up
interpolated into a log message - directly or via an exception's `str()`.

Acceptance: any unsafe call sites fixed (redact or omit the sensitive
value); a regression test that runs a representative slice of the pipeline
with fake-but-realistic-looking secrets in play, captures all log output,
and asserts none of those values appear in it.

---

## 6. Scheduled runs with canary gating

Everything today is triggered manually. Build a wrapper (a script your
task runner of choice - cron, systemd timer, GitHub Actions schedule - can
call) that runs `canary.check.run_canary()` first and only proceeds to
`cli.py start` if it passes; on a canary failure, it should exit non-zero
and log why, not silently skip or silently run anyway.

Acceptance: script exists with a documented invocation; a test that proves
a failing canary actually blocks the run (not just that the canary function
gets called); README documents how to wire it into an actual scheduler.
