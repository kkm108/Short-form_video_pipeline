# Short-form video pipeline

A manifest-driven, resumable pipeline: seed topic -> script -> media generation
-> assembly -> human review -> publish. It's a deterministic state machine, not
an open-ended agent - step order comes entirely from `manifests/short_form.yaml`,
and every step is idempotent, checkpointed, and independently retryable.

Two decisions shape everything else here, both explained further where they're
implemented:

- **Publish uses official platform APIs** (YouTube Data API, Meta Graph API,
  TikTok Content Posting API), not browser automation against a logged-in
  account - see `executors/publishers/*.py`.
- **A run never auto-publishes.** `review_gate` always pauses for a human;
  there's no code path where it succeeds on its own - see
  `executors/human_checkpoint.py`.

## Manifests

Any YAML file matching this schema works - `cli.py start <path> <topic>`
takes the manifest as a plain argument, nothing is hardcoded to
`short_form.yaml`. Three are included, in increasing order of what they need:

| Manifest | Needs | What it exercises |
|---|---|---|
| `manifests/dry_run.yaml` | Nothing | Full pipeline shape end-to-end: real `ffmpeg` render, real pause at `review_gate`, stubbed script/media/publish |
| `manifests/youtube_only.yaml` | LLM key, a media-gen session, YouTube credentials | A real run, one platform, single-endpoint `script` step |
| `manifests/chain_example.yaml` | Same as above, but `script` reads `providers/` | A real run using the multi-provider fallback chain instead of one fixed endpoint |
| `manifests/short_form.yaml` | All of the above, plus Instagram + TikTok credentials | A real run, all three platforms |

Adding your own is the same shape as `youtube_only.yaml` - copy it, adjust
`platforms:` and the matching `publish_*` step(s). `manifest.py` validates
required keys and rejects duplicate step names, so a malformed one fails at
load time with a clear message rather than partway through a run.

## What's tested vs. what needs your own setup

Everything below has a passing test in `tests/` that actually exercises the
code (real HTTP round trips against local mock servers where relevant, or
real `ffmpeg` renders) - not just a class that looks right on paper.

| Component | Status |
|---|---|
| Orchestrator (retry, idempotent resume, approval gate, crash-safety) | Tested |
| State store (checkpoint persistence, execution-order retrieval) | Tested |
| `script` (LLM call) | Tested end-to-end against a mock OpenAI-compatible server |
| `script` via provider chain (`llm_chain`) | Tested end-to-end against mocks for all three request shapes (anthropic/gemini/openai-compatible) plus a real subprocess for the CLI-agent path |
| `assembly` (ffmpeg) | Tested with real ffmpeg renders (crop, captions path, music mix) |
| `review_gate` | Tested (notifies, always parks, survives a dead webhook) |
| Review webhook (`webhook_server.py`) | Tested end-to-end (a fake Slack button payload actually moves a parked run to approved/rejected) |
| Scheduled runner with canary gate (`run_scheduled.py`) | Tested (a failing canary really blocks the run; a passing one proceeds to start) |
| Alerting (`alerting/alerter.py`) | Tested against a local mock webhook (alert is really dispatched; a 5xx is reported; no webhook is a no-op, not an error); wired into `run_scheduled` to page on canary failure / cli crash |
| Credential refresh (`scripts/refresh_tokens.py`) | Tested against a local mock Graph API + fake vault (Instagram token really refreshed and persisted; a 5xx is reported without persisting). TikTok/YouTube have no auto-refresh endpoint and report the human step needed instead |
| Disk-space guardrail (`orchestrator/disk_guard.py`) | Tested (free space read; passes with room, raises `DiskLowError` below threshold; the engine's `start()` refuses to create a run when the guard fires). Runtime partner to the boot probe's boot-time check, threshold via `PIPELINE_MIN_FREE_GB` |
| Concurrency lock (`orchestrator/run_lock.py`) | Tested cross-platform (flock on POSIX / `msvcrt.locking` on Windows): a second acquisition while held raises `LockHeld`, reusable after release, and a held lock makes `cli.py start`/`resume`/`approve` exit non-zero instead of running concurrently. Auto-released by the OS on a crash, so no stale-lock cleanup |
| Backup & restore (`scripts/backup.py`, `scripts/restore.py`) | Tested end-to-end (backup -> wipe -> restore -> `cli.py status` still works); no `profiles/` content or raw secret ever lands in an archive, and restore **refuses** crafted `profiles/`, path-traversal, and link entries |
| `publish_instagram`, `publish_tiktok` | Tested end-to-end against mock APIs; **need your own Meta/TikTok app + tokens to run live** |
| `publish_youtube` | Tested end-to-end including real OAuth credential construction; **needs `pip install google-api-python-client google-auth-oauthlib` + your own OAuth app to run live**. Now quota-aware: tracks a per-day upload budget (`orchestrator/quota_tracker.py`, ledger under `runs/`, budget via `QUOTA_YOUTUBE_BUDGET`) so a scheduled run skips instead of hammering a spent daily quota |
| `credentials.vault` | Tested (env-var fallback path - the one that runs in headless/CI contexts) |
| `media_generation` (browser-use) | Config validation tested; **the actual generation task needs `pip install browser-use`, `playwright install chromium`, and a session file from logging into your chosen tool once** |
| `media_generation` (Gemini, `gemini_media`) | Tested end-to-end against a local mock API (image + TTS request bodies and base64 response parsing write real files); **the live call needs `GEMINI_API_KEY`, no browser** |
| `media_generation` (multi-vendor chain, `media_chain`) | Tested end-to-end against local mocks for both vendors: per-artifact fallback (gemini down for images but fine for voiceover still yields a bundle), missing-credential providers skipped, no-provider-succeeds -> non-retryable error. Live: needs `GEMINI_API_KEY` (primary, `providers/020-gemini-media.json`) and optionally `OPENAI_API_KEY` (`providers/030-openai-media.json`) |
| Dry-run stubs (`executors/stubs.py`) | Exercised via `manifests/dry_run.yaml` end-to-end through the real CLI, not just in isolation |
| `canary` | Not tested - has no target site's selectors filled in yet (see below) |

Run everything that's runnable here:
```
pip install -r requirements.txt
python -m tests.test_orchestrator
python -m tests.test_state
python -m tests.test_llm_executor
python -m tests.test_ffmpeg_assembly
python -m tests.test_human_checkpoint
python -m tests.test_vault
python -m tests.test_instagram_publisher
python -m tests.test_tiktok_publisher
python -m tests.test_youtube_publisher
python -m tests.test_publish_step
python -m tests.test_browser_use_adapter
python -m tests.test_webhook_server
python -m tests.test_log_redaction
python -m tests.test_scheduled_run
python -m tests.test_backup_restore
```

## One-time setup per platform

**Nothing, if you just want to see it run** - `manifests/dry_run.yaml` needs
no accounts at all (see the table above). Everything past here is for a real
run.

**LLM** - any OpenAI-compatible provider or gateway works. Set `LLM_API_KEY`
and point `base_url` in the manifest at it.

**Media generation tool** - pick one (image/video gen + TTS). Export a
logged-in session for it once:
```
pip install playwright && playwright install chromium
python scripts/export_session.py https://your-tool.example.com/login ./profiles/generation_tool.json
```
It opens a real browser, you log in by hand (2FA/CAPTCHA included), press
Enter, and it saves the session `browser_use_adapter.py` needs. Then write
`task_template` in the manifest for what you want the tool to do, set
`llm_provider`/`llm_model` (`openai` or `anthropic`, plus which env var
holds the key - `short_form.yaml` shows the shape) so browser-use has
something to actually drive the browser with, and fill in
`canary/check.py`'s `EXPECTED_ELEMENTS` with a couple of that tool's
selectors so drift gets caught before a real run.

**YouTube** - Google Cloud Console -> enable "YouTube Data API v3" -> OAuth
2.0 credentials (Desktop app type) -> run the OAuth flow once (any
`google-auth-oauthlib` "installed app" flow example works) to get a refresh
token. Store three values via the vault (or the matching env vars for
headless use): `youtube_refresh_token`, `youtube_client_id`,
`youtube_client_secret` (`YOUTUBE_REFRESH_TOKEN` / `YOUTUBE_CLIENT_ID` /
`YOUTUBE_CLIENT_SECRET`). Unverified apps get a low daily quota - request an
audit once you're past testing.

**Instagram** - Professional account linked to a Facebook Page -> Meta app
with the Instagram Graph API product -> long-lived access token. Store
`instagram_ig_user_id` and `instagram_access_token` via the vault (or the
matching env vars, `INSTAGRAM_IG_USER_ID` / `INSTAGRAM_ACCESS_TOKEN`, for
headless use). Note: the Graph API fetches video from a URL you give it, not
a direct upload - stage the rendered file somewhere public (e.g. a presigned
S3 link) before this step runs.

**TikTok** - TikTok for Developers app with the Content Posting API product.
Public posting on your own account works once you've added it as a test
user; posting on behalf of others needs TikTok's audit. Store
`tiktok_access_token` via the vault.

## Running it

Zero-setup first look:
```
python cli.py start manifests/dry_run.yaml "history's shortest war"
python cli.py status run_ab12cd34ef56          # watch it park at review_gate
python cli.py approve run_ab12cd34ef56 review_gate
# open runs/run_ab12cd34ef56/assembled.mp4 - it's a real, playable render
```

A real run, once the setup above is done for at least one platform:
```
python cli.py start manifests/youtube_only.yaml "history's shortest war"
python cli.py status run_ab12cd34ef56
# ... after the review_gate notification arrives ...
python cli.py approve run_ab12cd34ef56 review_gate
# if a step fails after exhausting retries, fix the underlying issue, then:
python cli.py resume run_ab12cd34ef56
```

`approve`/`reject` here are a CLI stand-in for whatever actually receives the
webhook in `human_checkpoint.py` - a Slack slash-command handler or a small
webhook endpoint would call the same `orchestrator.engine.approve()` /
`reject()` functions.

## Slack review webhook (button clicks)

`webhook_server.py` is that "additional way in": a tiny `http.server` endpoint
that receives Slack interactive-message callbacks and calls the same
`engine.approve()`/`reject()` the CLI does, so a reviewer can click a button
on the review message instead of typing `cli.py approve`.

Setup:

1. Give the review message somewhere to live. In your Slack app's **Incoming
   Webhooks** feature, add a webhook to the channel you want reviews posted
   to, and put that URL in the manifest's `review_gate` step under
   `notify_webhook`. The review gate already posts there - it now includes an
   interactive Approve/Reject pair of buttons (see `human_checkpoint.py`'s
   `_build_notification`).
2. Point clicks back at this server. In your Slack app's **Interactivity &
   Shortcuts** feature, set the Request URL to your deployment of
   `webhook_server.py` (must be publicly reachable, e.g. behind a tunnel or
   reverse proxy). Slack POSTs each button click there.
3. Run the endpoint on the same state store as the CLI so both surfaces see
   the same runs:
   ```
   python webhook_server.py --host 127.0.0.1 --port 8080
   ```

   (or call `webhook_server.make_server(pipeline)` from your own process).

> **Security note**: request signature verification (Slack's `X-Slack-Signature`)
> is a documented TODO in `webhook_server.py`, not silently skipped. Until it's
> wired up, keep the endpoint off the public internet or behind auth - an
> unverified callback could approve/reject a run that wasn't meant to be.

## Scheduled runs with canary gating

Everything else in this repo is triggered by hand. For unattended runs,
`run_scheduled.py` is the wrapper your scheduler should call *instead of*
`cli.py` directly: it runs `canary.check.run_canary()` first and only proceeds
to `cli.py start` when the canary passes. On a canary failure it exits non-zero
(return code 2) and logs what failed - it never silently skips the check, and
never launches a real run against a UI that has drifted.

Direct invocation:

```
python run_scheduled.py --manifest manifests/youtube_only.yaml \
    --topic "history's shortest war" \
    --session ./profiles/generation_tool.json \
    --target-url https://your-tool.example.com/generate
```

Wire it into a real scheduler. A cron line (daily at 06:00):

```
0 6 * * * cd /path/to/pipeline && python run_scheduled.py --manifest manifests/youtube_only.yaml --topic "history's shortest war" --session ./profiles/generation_tool.json --target-url https://your-tool.example.com/generate >> runs/scheduled.log 2>&1
```

For systemd, use a timer unit whose `ExecStart` is that `run_scheduled.py`
invocation and add `Environment=...` for whatever secrets the run needs (the
pipeline reads credentials from the OS keychain or env vars). For a GitHub
Actions schedule, put the same invocation in a `schedule`-triggered workflow;
`if: failure()` on a following job can page Slack when the canary blocks a run.

The canary needs `EXPECTED_ELEMENTS` filled in for your generation tool before
it checks anything meaningful (see `canary/check.py`). `tests/`
`test_scheduled_run.py` proves a failing canary actually blocks the run, not
just that the check gets called.

## Docker is not required

There is no Dockerfile, compose file, or container image anywhere in this repo,
and none of the pipeline steps need one. On the supported unattended target
(Windows + Task Scheduler, or cron/systemd on a host), every step runs as a
plain host process: the orchestrator, script and media steps are Python stdlib +
HTTP calls, `assembly` shells out to a host `ffmpeg`, and publishers call the
platform HTTP APIs directly. The only external tools the boot probe guards are
`python`, `ffmpeg`, `git`, and the OS keychain.

Containerizing would actually *fight* this pipeline's design:

- **The vault is bound to the host user.** `credentials/vault.py` stores
  platform tokens in the OS keychain (keyring's `WinVaultKeyring` / Secret
  Service). A container has neither a logged-in user's keychain nor, without
  extra mounting, access to the host's - the whole point of the vault (never a
  committed secret) assumes the same host profile the scheduler runs as.
- **Scheduled-run safeguards are host-process primitives.** The concurrency lock
  (`orchestrator/run_lock.py`) and disk guard (`orchestrator/disk_guard.py`)
  operate on host filesystem/process semantics (advisory file locks,
  `shutil.disk_usage` on the run volume). Shipping them in a container would
  still rely on mounting the host's `runs/` volume, adding no isolation.
- **No step benefits from a sandbox.** `browser_use_adapter.py` drives *your
  own* media-generation session - it is not a multi-tenant service needing
  process isolation.

So: **confirm Docker is NOT required** for this pipeline's scheduled deployment.
If you later want reproducibility across machines, a `requirements.txt` pin +
the `bootstrap_machine.ps1` idempotent setup serves it more directly than a
container, and without the keychain conflict.

## Dependency pinning

`requirements.txt` and `requirements-dev.txt` are pinned to exact (`==`)
versions matching what's installed and green under the test suite. The `>=`
bounds they used to have meant a silent `pip install` could upgrade a library
underneath the code and reintroduce exactly the class of bug this project has
already hit twice - a third-party signature changing without anyone noticing.

The intentional-upgrade process when you decide a bump is warranted:

1. Pick one dependency, bump only that line (leave the rest pinned).
2. Re-run that dependency's test module(s) - e.g. a `google-api-python-client`
   bump means re-running `tests/test_youtube_publisher.py`.
3. Re-verify the library's real signature with `python -c "import inspect;
   print(inspect.signature(...))"` (or the relevant API surface) per AGENTS.md -
   do not trust a blog post or your memory.
4. Run the full suite, then `ruff check .` and `mypy .`.
5. Only after all of that is green is the bump "tested" and worth recording.

`browser-use` is installed and pinned at `browser-use==0.13.8` (it pulls the
google client pins down to 2.188.0 / 1.2.4 - see the note in this section
above); its browser binary comes from a separate `python -m playwright install
chromium`. `keyring` is installed and pinned at `keyring==25.7.0`, with the
vault's OS-keychain path verified against the Windows Credential Locker (see
"What changed while building this").

## Backup & restore

Move machines or survive data loss without ever shipping credentials. A backup
contains everything needed to resume a run, and *only* that:

**Captured (secret-free by design):**
- `pipeline_state.db` - SQLite run history, checkpoints, idempotency store
- `manifests/*.yaml` - run configs
- `providers/*.json` - LLM fallback-chain config (references secrets by name, never embeds them)

**Explicitly NOT captured (must never appear in an archive):**
- `profiles/*.json` - live, logged-in browser sessions. These *are* credentials.
  Re-mint them with `scripts/export_session.py` after restoring.
- `runs/`, `downloads/` - generated media; large and regenerable.
- Raw secret values - they live in the OS keychain / env vars, never in a
  committed or archived file.

`scripts/backup.py` builds one portable `.tar.gz` with a top-level
`backup-manifest.json` listing every included file and every excluded category
(and why):

```
python scripts/backup.py --workspace . --out backup_2026-01-01.tar.gz
```

`scripts/restore.py` unpacks it onto a fresh checkout. It normalizes each
archive member name and then refuses to extract anything under `profiles/`,
anything that would escape the workspace (path traversal / absolute paths), or
any link member - a crafted `./profiles/...` name is normalized and caught like
any other credential path:

```
python scripts/restore.py --archive backup_2026-01-01.tar.gz --workspace .
```

**Manual steps after a restore** (the backup correctly can't do these):
1. `python scripts/export_session.py <login-url> ./profiles/generation_tool.json`
   to mint a fresh logged-in browser session.
2. Re-seed the vault secrets (OS keychain or env vars) for each `*_ref` your
   providers/publishers use.

`tests/test_backup_restore.py` proves the whole loop: it backs up a seeded
workspace, wipes the working directory, restores, and asserts
`cli.py status <run>` still reports the run - and separately asserts no
`profiles/*.json` content or raw secret value ever appears inside a produced
archive (checking each decompressed member, not just the gzip bytes). It also
feeds `restore.py` crafted archives (`./profiles/...`, `../`, absolute paths)
and asserts they're refused, and that a legitimate dotfile like `.env` survives
a restore with its name intact.

## Project structure

```
pipeline/
  orchestrator/
    manifest.py     # YAML -> validated StepSpec list
    models.py        # RunState, StepResult, StepStatus
    state.py          # SQLite checkpoint + idempotency store
    engine.py          # the state machine: retry, resume, approval gate
    quota_tracker.py    # per-day per-platform quota ledger (JSON under runs/), backs off before the YouTube wall
    disk_guard.py       # runtime disk-space guardrail: refuses to start a run when free space is too low
    run_lock.py         # mutual-exclusion lock (flock/msvcrt) so two runs never execute at once
  executors/
    base.py             # Executor interface, ExecutorError, AwaitingApproval
    llm.py                # script: provider-agnostic OpenAI-compatible call
    llm_chain.py            # script: multi-provider fallback chain, see providers/
    browser_use_adapter.py # media_generation: browser-use wrapper
    gemini_media.py          # media_generation: Gemini API (images+voiceover), no browser
    media_providers.py       # per-vendor images+voiceover handlers (gemini/openai), shared
    media_chain.py           # media_generation: multi-vendor per-artifact fallback chain, see providers/
    ffmpeg_assembly.py       # assembly: deterministic render, no AI calls
    human_checkpoint.py       # review_gate: notifies, always parks
    publish_step.py            # wraps one platform Publisher as an Executor
    stubs.py                     # zero-setup stand-ins - see manifests/dry_run.yaml
    publishers/
      base.py                    # Publisher interface
      youtube.py                   # YouTube Data API v3
      instagram.py                   # Meta Graph API (Content Publishing)
      tiktok.py                        # TikTok Content Posting API
  credentials/
    vault.py             # OS keychain via keyring, env-var fallback - never plaintext
  canary/
    check.py             # scheduled UI-drift check, separate from real runs
  alerting/
    alerter.py           # webhook alert on canary failure / cli crash (opt-in, non-crashing)
  providers/
    000-gemini.json        # multi-provider chain config - see credentials note below
  manifests/
    dry_run.yaml          # zero-setup, full pipeline shape
    youtube_only.yaml       # real run, one platform
    chain_example.yaml        # real run, multi-provider script step
    short_form.yaml             # real run, all three platforms
  scripts/
    export_session.py    # one-time interactive login -> browser-use session file
    backup.py            # portable, credential-free backup archive + manifest
    restore.py           # unpacks a backup onto a fresh checkout (guards credentials/path-traversal)
    bootstrap_machine.ps1 # idempotent python3-shim + PATH setup
    boot_probe.ps1        # boot-time readiness check (Task Scheduler)
    register_boot_probe.ps1 # registers the boot probe as a scheduled task (elevated)
    refresh_tokens.py     # per-platform credential refresh (IG real, TT/YT report)
    register_tokens.ps1   # registers the Instagram refresh task (elevated)
  tests/                  # one file per component, all passing
  cli.py                   # start / resume / approve / reject / status / list
  webhook_server.py        # Slack review_gate button-click callback receiver
  run_scheduled.py         # canary-gated wrapper for cron/systemd schedules
  .github/workflows/ci.yml  # CI: each test module on its own step + lint + manifest validation
```

## A note on what changed while building this

The manifest sketch from the architecture discussion had one `publish` step
covering all platforms. Building `publish_step.py` end-to-end surfaced why
that's wrong: with one grouped step, a retry after a TikTok failure would
have re-published to YouTube and Instagram too, since the engine's
idempotency is per-step. `tests/test_publish_step.py` is written specifically
against that failure mode - three platforms are three manifest steps, so a
retry only ever touches the one that actually failed.

Running `manifests/dry_run.yaml` through the real CLI (not just unit tests)
surfaced a second one: `state.py`'s step query had no `ORDER BY`, so on a
multi-step run the `.steps` dict could come back in whatever order SQLite
felt like returning rows in, not execution order - `cli.py` printed
"completed through 'script'" for a run that had actually gone on to park at
`review_gate`. Fixed with `ORDER BY rowid`, and `tests/test_state.py` checks
it directly.

`browser_use_adapter.py` was the one piece of the pipeline never checked
against the real library it wraps - installing `browser-use` 0.13.8 to
verify signatures turned up two real problems, not just naming mismatches:
`Agent(llm=None)` doesn't mean "no LLM," it silently falls back to
browser-use's own hosted default and demands a `BROWSER_USE_API_KEY` this
project never asked for; and `agent.run()` returning without raising doesn't
mean the task succeeded - it can stall or run out of steps and still hand
back a normal-looking result. `build_llm()` and `interpret_history()` now
handle both explicitly, and are plain functions specifically so that logic
is unit-testable without a real browser at all.

All three bugs share a pattern worth naming: none were visible from any
single component's isolated unit tests or from reasoning about the code -
each only showed up by either running the full real sequence once, or by
actually installing and inspecting the real library being wrapped instead of
trusting how its API was remembered.

`executors/llm_chain.py` came from outside this project - a working
multi-provider fallback chain (HTTP APIs plus a local CLI-agent path) that
did real work, but discovered providers by scanning a directory of JSON
files with the API key embedded directly in each one. That's the exact
plaintext-credential pattern `credentials/vault.py` exists to avoid
everywhere else in this project, so the logic was kept and the storage was
not: `providers/*.json` now name a vault key (`api_key_ref`) instead of a
literal secret, which also means, unlike the original, these config files
are safe to commit.

Running the suite on Windows turned up one more: `llm_chain`'s CLI-provider
path runs through the shell on Windows (`shell=(os.name == "nt")`), so a
missing binary returned a nonzero shell exit instead of the `OSError` the
non-shell path raises on POSIX - which made a genuinely unrecoverable "command
can't start" failure look retryable. It was hidden on Linux, where the
`OSError` path labeled it correctly. `_call_cli()` now does an up-front
`shutil.which(cmd)` check and raises non-retryable, so a missing CLI provider
behaves the same on both platforms.

A secret-safe-logging audit turned up a real leak in the same file: the gemini
provider embeds its API key directly in the request URL (`?key=...`), and a
failing `requests` call reproduces that URL - key included - in the exception
message. That string then flowed into `ExecutorError` and straight into
`logger.warning(...)`, so a DEBUG-level bug report could have contained a live
LLM key. `_call_http` now redacts the key from the failure message (and the
error-response body) before it enters the error/log stream; `tests/`
`test_log_redaction.py` pins this down by running a real provider call with a
fake key and asserting the key never appears in captured logs.

Backup/restore was added as the portability answer for resumability: a backup
contains the SQLite run-history DB, manifests, and providers configs, and
nothing else. The sharp edge worth recording is `profiles/*.json` - a live
browser-session export is a logged-in credential, so a backup that swept up
"everything" would ship a working login. `scripts/backup.py` excludes it by
construction (the same reason the `.gitignore` does), and `test_backup_restore.py`
asserts the exclusion against each decompressed archive member, not just the
gzip bytes (raw-substring checks would be vacuously green under compression).

`requirements.txt` and `requirements-dev.txt` were changed from `>=` bounds to
exact `==` pins, matching the versions actually installed and green. The `>=`
bounds were the mechanism by which a silent upgrade could reintroduce the
signature-drift bugs this section keeps documenting; pinning removes that
specific silent path. The intentional-upgrade process is spelled out in the
README 'Dependency pinning' section. The optional deps that aren't installed or
tested stay unpinned on purpose - there's no tested version to record for a
library this repo doesn't currently exercise. (`keyring` used to be the example
of that; it's now installed, pinned at `keyring==25.7.0`, and its
WinVaultKeyring backend is verified - see the note at the end of this section.)

Installing `browser-use` 0.13.8 as part of preparing a real run surfaced a
third, silent correctness issue in the browser path that had been invisible
for the same reason as the first two: the code that wraps it had never been
checked against the real library it wraps. `canary/check.py` used playwright's
`page.locator(...).count()` API - that page object in browser-use 0.13.8 is its
own `Page` (from `browser_use/actor/page.py`), which has no `locator()`; the
canary would have crashed the first time it actually ran against a real login.
It now uses `page.get_elements_by_css_selector(selector)` (a real method on
that class). `browser_use_adapter.py`'s `Agent(...)` likewise needed an
explicit type annotation once mypy could resolve the real `Agent[Context,
AgentStructuredOutput]` class instead of the `# type: ignore[import-not-found]`
placeholder. Fixing these was free while the suite, lint, and mypy are all
green - but only because installing the library made the drift visible.

The install also forced a dependency backtrack: browser-use 0.13.8's tree pins
`google-auth-oauthlib` down to 1.2.4 and `google-api-python-client` to 2.188.0,
so the earlier pins (1.3.0 / 2.192.0) would not survive a fresh
`pip install -r requirements.txt`. `requirements.txt` now records the versions
the resolver actually lands on and the suite is green under (2.188.0 / 1.2.4 /
requests 2.33.0), with browser-use pinned for real instead of left as a comment.

The media step got a second, browser-free path: `executors/gemini_media.py`
generates the images and the voiceover by calling the Gemini API directly over
HTTP with the same key the script step already uses (`api_key_ref` ->
`GEMINI_API_KEY`), so a Google/Gemini setup no longer needs browser-use or a
session profile at all. Its request/response shapes were verified against the
live ai.google.dev docs before writing it (per AGENTS.md), not remembered: the
image endpoint is `models/gemini-3.1-flash-image:generateContent` with
`responseModalities: ["IMAGE"]` and base64 `inlineData` back, and the voiceover
is `models/gemini-3.1-flash-tts-preview:generateContent` with
`responseModalities: ["AUDIO"]` + `speechConfig.voiceConfig
.prebuiltVoiceConfig.voiceName` returning a WAV. Notably, Imagen 4 (`:predict`)
is deliberately avoided - it shut down on Aug 17 2026 and requires billing,
whereas the flash-image path works on the free tier (~500 images/day). The
`gemini_real.yaml` manifest wires script -> gemini media -> assembly -> a
parked review gate, no publishing. The mock-server test (`tests/
test_gemini_media.py`) caught one bug of its own: it initially returned a path
from inside a `with TemporaryDirectory()`, which deletes the directory on
return - an easy trap worth naming in the same spirit as the rest of this
section.

`gemini_media` grew into a full multi-vendor chain for the media step:
`executors/media_providers.py` holds the per-vendor image/voiceover handlers
(gemini + openai), and `executors/media_chain.py` tries each media-capable
provider config in `providers/*.json` in priority order, per artifact - so a
Gemini outage or rate-limit can fall images or narration back to OpenAI (or
vice versa) without halting the run, the same spirit as `llm_chain` for the
script. The provider configs (`providers/020-gemini-media.json`,
`030-openai-media.json`) declare a `vendor` so script providers and media
providers never get mixed up even though they share the directory, and keys
come only from `api_key_ref` -> vault -> env var. One real design bug surfaced
while writing the mock tests: the chain resolved every provider's credential up
front without catching `CredentialNotFound`, so a provider without its env var
set killed the whole run instead of being skipped like `llm_chain` skips it.
That's fixed - unresolved-credential providers are logged and skipped. The
`manifests/media_chain_real.yaml` wire-up (script -> media_chain -> assembly ->
parked review gate) validates via `load_manifest()` like every other manifest.

`keyring` got installed and promoted from an unpinned `==?` placeholder to an
active `keyring==25.7.0` pin in `requirements.txt`. The vault's OS-keychain path
was verified for real against the Windows Credential Locker
(`keyring.backends.Windows.WinVaultKeyring`), not just assumed: a set/get/delete
round-trip succeeded and `tests/test_vault.py` stays green with the backend
present. Headless/CI fallback to env vars is unchanged and still covered by the
suite.

Alerting for the unattended gap: `alerting/alerter.py` posts plain-text to a
Slack-compatible webhook and **never raises to its caller** - a failed alert
mustn't also crash whatever was already failing. It no-ops when no webhook is
configured (default unchanged) and reads the URL only from the vault
(`pipeline_alert_webhook`) or `PIPELINE_ALERT_WEBHOOK` env var, never a
committed file. `run_scheduled.py` now takes `--alert-webhook` and pages on both
failure modes that used to be silent: a red canary (blocked run) and a
`cli.py start` that exits non-zero or can't be launched. The mock-server test
(`tests/test_alerter.py`) proved delivery for real and that a 5xx is reported,
not swallowed.

Credential expiry was the other silent gap: `scripts/refresh_tokens.py` handles
per-platform refresh, but the three platforms turn out to be very different.
Instagram genuinely refreshes (Meta's `ig_exchange_token` endpoint re-issues a
long-lived token, which is written straight back to the keychain via
`credentials.vault.set` - never to a file). TikTok's 24-hour token has no such
endpoint; it needs a full OAuth authorization-code refresh with the app's
client_id/client_secret, so the script reports what must happen rather than
faking it. YouTube has no long-lived refresh endpoint at all - google-auth
self-refreshes on the first API call, so the only genuine task is the one-time
human check that the OAuth consent screen is in Production/Published, not
Testing (which expires tokens in 7 days). The Instagram job is registered as a
scheduled task via `scripts/register_tokens.ps1` (every 15 days by default,
inside the 60-day window). Tests in `tests/test_refresh_tokens.py` hit a local
mock Graph API and prove the token is really re-issued and persisted, and that
a 5xx is reported without persisting garbage.

Secrets hygiene hardened independently of `scripts/backup.py`'s exclusion (which
only protects backups, not commits): the `.gitignore` already covered
`profiles/*.json`, `runs/`, and the run DB; it now also hard-ignores `.env`,
`.env.*`, `*.env`, `.vault/`, and raw key/cert files (`*.key`, `*.pem`, `*.pfx`,
`*.p12`) so a stray secrets file can never be `git add`ed by mistake. Verified
via `git check-ignore` (`.env`, `profiles/storage_state.json`, `runs/...` all
ignored) and a disk scan found no stray `.env`/key files. No secret-like file
is tracked.

Retry/backoff was already a first-class engine feature - `ExecutorError` carries
a deliberate `retryable=` and optional `status_code`, and the engine only retries
when the error is retryable *and* its status code is in the step's `retry_on`
list, pacing with none/fixed/exponential backoff. P0-6's real gap was keeping
score of quota: an unattended scheduled run could hit YouTube's daily upload
quota (a concrete wall) and burn the whole budget on retries that can never
succeed. That's now `orchestrator/quota_tracker.py`, a tiny per-day per-platform
JSON ledger under `runs/` (gitignored, atomic temp-then-rename writes). The
YouTube publisher checks the remaining budget before uploading, records its cost
on success, and - the important bit - marks the day exhausted and reports
non-retryable when the API answers `quotaExceeded`/`dailyLimitExceeded`, so the
next scheduled trigger backs off instead of thrashing. The budget is a
scheduler-config value (`QUOTA_YOUTUBE_BUDGET`, defaulting to `YOUTUBE_DEFAULT_BUDGET`),
read from env, never a secret. `tests/test_quota_tracker.py` (state/round-trip/
corrupt-ledger behavior) and the quota cases in `tests/test_youtube_publisher.py`
(preexisting exhausted budget blocks, cost recorded on success, quotaExceeded
marks exhausted + non-retryable) prove the behavior against a fake client.

Disk space was the silent failure that boot-time checks alone can't catch: a run
can start when the boot probe passed hours earlier, then fill the drive producing
media + assembling with ffmpeg. So on top of the boot probe's `>=5GB` check,
`orchestrator/disk_guard.py` is a runtime guardrail - stdlib `shutil.disk_usage`,
threshold from `PIPELINE_MIN_FREE_GB` (default 2GB) - called from the engine's
`start()` and `resume()` *before* creating a run. A too-low result raises
`DiskLowError` rather than creating the run, so an unattended scheduler fails
loudly (and `run_scheduled.py` pages on the non-zero exit) instead of silently
filling the disk. `tests/test_disk_guard.py` proves free-space reads, the
pass/raise boundary, and that the engine never reaches `create_run` when the
guard fires.

Concurrent triggers were the other unscheduled gap: a scheduler firing twice in
one window (or a manual trigger colliding with a scheduled one) would share
`runs/`, the SQLite state DB, and - worse - double-spend platform quota on the
same seeded topic. `orchestrator/run_lock.py` is an advisory file lock held for
the whole `cli.py start`/`resume`/`approve` call, using the OS's own mechanism
(`fcntl.flock` on POSIX, `msvcrt.locking` on Windows) so it's auto-released if
the process dies mid-run - there's no stale-lock file to clean up by hand, and
advisory locking means only cooperating runs honor it (which is all we have).
A `LockHeld` result makes the CLI exit 3 with a clear "another pipeline run is
already in progress" message instead of starting a second run. `tests/test_run_lock.py`
proves the acquire/release round trip, that a second acquisition while held
raises `LockHeld`, and that the lock is reusable once released - and a manual
check confirmed a held lock makes `cli.py start` refuse (exit 3).

The `python3` shim was a classic "verified under `python`, silently broken under
`python3`" trap. On this machine `python3` didn't point at Python at all - it
fell through to the Microsoft Store *App Execution Alias* stub
(`AppData\...\WindowsApps\python3.exe`), which prints "Python was not found"
and runs nothing, while `python` (3.14.2) worked fine. Worse, the bootstrap's
old `Get-Command python3` check treated that broken stub as "found" and printed
PASS. `scripts/bootstrap_machine.ps1` now has a `[3b/3]` step that runs
`python3 --version` and requires it to both actually run and match `python --version`,
FAILING loudly (stub or stale-copy message, exit 1) otherwise - so a git-style
"same version, different interpreter" drift can never silently pass. The shim
itself (`~/bin/python3.exe` -> `C:\Python314\python.exe`) was also created here as
the non-elevated copy fallback so `python3` resolves to the real interpreter
until the bootstrap is re-run elevated to upgrade it to a symlink/hardlink.
"# Short-form_video_pipeline"
