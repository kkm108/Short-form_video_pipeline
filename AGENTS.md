# AGENTS.md

Manifest-driven pipeline: seed topic -> script -> media generation -> assembly
-> human review -> publish. `README.md` has full setup and architecture;
this file is the standing contract for any agent working in this repo -
read it as the definition of "correct," not just "context."

## Non-negotiable decisions

Do not silently reverse these, even if they'd simplify a task:

- **Publish uses official platform APIs** (`executors/publishers/`), never
  browser automation against a real, audience-facing account.
  `executors/browser_use_adapter.py` is only for driving your own login
  session on a media-*generation* tool - never a publishing platform.
- **`review_gate` never auto-approves.** No code path in
  `executors/human_checkpoint.py` may return success on its own; only an
  external `engine.approve()` call does.
- **Control flow lives in the manifest, not in an agent loop.** The
  orchestrator (`orchestrator/engine.py`) is a deterministic state machine.
  LLM/browser calls happen inside a step's executor, never at the
  orchestration layer.
- **Credentials only via `credentials/vault.py`.** Never embed a secret in
  a file that gets committed - not in a manifest, not in `providers/*.json`,
  not in a test fixture. New credential need = a `*_ref` / env-var-name
  config field, resolved through the vault.

## The one habit that matters most here

Every real bug found in this codebase came from an unverified assumption
about a third-party library or the orchestrator's own SQL - never from
reasoning about code in the abstract. See "what changed while building
this" at the bottom of README.md for specifics (an ffmpeg `-map` flag, a
Google SDK silently falling back to hidden default credentials, browser-use
treating a stalled run as success, an unordered SQL query that misreported
run state). Before treating any integration with an external library as done:

1. Check the real signature - `pip install` the package (or confirm it's
   already installed) and `python3 -c "import inspect; print(inspect.signature(...))"`.
   Not what you remember, not what a blog post says.
2. Write a test that exercises it for real: a local mock HTTP server for
   network calls (any `tests/test_*_publisher.py`), a real subprocess for
   CLI calls (`tests/test_llm_chain.py`), real `ffmpeg` for media
   (`tests/test_ffmpeg_assembly.py`).
3. Every executor failure becomes `ExecutorError` with a deliberate
   `retryable=` value. Never let a raw exception from a dependency escape
   uncaught - the engine has a catch-all safety net, but it means "unknown,
   don't retry," not "handled well."

## Commands

```bash
pip install -r requirements.txt
python3 -m tests.test_orchestrator          # one test module
for t in tests/test_*.py; do python3 -m "$(echo "$t" | sed 's/\.py$//; s#/#.#')"; done   # whole suite
python3 -c "from orchestrator.manifest import load_manifest; load_manifest('manifests/short_form.yaml')"  # validate a manifest
python3 cli.py start manifests/dry_run.yaml "topic"   # zero-setup real run, no credentials needed
```

## Layout

`orchestrator/` state machine (manifest, models, state, engine) ·
`executors/` one file per pipeline step, `publishers/` per platform ·
`credentials/vault.py` secrets, OS keychain + env-var fallback, never
plaintext · `canary/` UI-drift check, separate from real runs ·
`manifests/` YAML run configs · `providers/` LLM fallback-chain configs
(config only, no secrets) · `scripts/` one-off interactive helpers ·
`tests/` one file per component, all passing. Full map in README.md.

## Definition of done

- Full suite green, including any new tests you added - run them, don't
  just write them.
- Every manifest in `manifests/` still loads via `load_manifest()`.
- Diff contains nothing that looks like a live secret before you finish -
  none belongs in a committed file.
- Behavior change -> add a short note to "what changed while building
  this" in README.md. It's the project's running record of real bugs found
  and why, and it's already prevented at least one from being
  reintroduced.
