# System Readiness Notes (pre-requirements)

Quick reference for the machine state behind the `03-OPENC` pipeline.
Kept inside the live repo at `pipeline/System Readiness Notes.md` so agents see
it (currently untracked in git - `??` - commit it or move it out if you'd rather
it not live in the repo; anything machine-specific that changes needs this file
updated too).

Everything below was verified for real during setup (2026-09-02), not assumed.

## Hard requirement: know your Python

| Command | Resolves to | Status |
|---|---|---|
| `python` / `pip` | `C:\Python314\python.exe`, `C:\Python314\Scripts` | Works |
| `python3` | `C:\Users\HP\bin\python3.exe` (shim copy) | Fixed - see below |
| `py` | `C:\Windows\py.exe` (official launcher) | Works |

> The Windows Store `python3` app-execution-alias stub is broken on this box
> (it points at a Store install that doesn't exist). Fixed by copying
> `C:\Python314\python.exe` to `C:\Users\HP\bin\python3.exe` and prepending
> `C:\Users\HP\bin` to the **user** PATH. Verified: `python3 --version` is
> 3.14.2 and `sys.prefix` correctly resolves to `C:\Python314`.

## Core toolchain (all installed and verified)

| Tool | Version | Path | Notes |
|---|---|---|---|
| Python | 3.14.2 (64-bit) | `C:\Python314` | full dev install |
| pip | 26.2.1 | user-site `AppData\Roaming\Python\Python314` + `C:\Python314\Scripts` | |
| Node.js / npm | 24.12.0 / 11.19.0 | `C:\Program Files\nodejs` | |
| Git | 2.53.0.windows.2 | `C:\Program Files\Git` | |
| PowerShell 7 | 7.6.1 | system | |
| FFmpeg / ffprobe / ffplay | 8.0.1 full (gyan.dev) | `C:\Windows\System32` | required by `assembly` |
| VLC | 3.0.23 | installed | |
| 7-Zip | 25.01 | installed | |
| Docker Desktop | 4.87.0 / engine 29.7.2 | `C:\Program Files\Docker\Docker` | daemon NOT auto-starting after reboot - start Docker Desktop manually |

## AutoHotkey (not on PATH originally - now fixed)

- v2: `C:\Users\HP\AppData\Local\Programs\AutoHotkey\v2\AutoHotkey.exe`
  (canonical launcher, created as a copy of `AutoHotkey64.exe`)
- v1.1.37.02: `C:\Users\HP\AppData\Local\Programs\AutoHotkey\v1.1.37.02\AutoHotkeyU64.exe`
- Both `v2` and `v1.1.37.02` dirs are on the user PATH.
- Verified with a real script run (`FileAppend(...)` wrote a file).

## Agentic CLIs (all on PATH)

`opencode` 1.18.26 · `claude` 2.1.144 (`C:\Users\HP\.local\bin`) · `codex` 0.147.0 ·
`pi` 0.84.2 · `copilot` 0.0.374 · `mimo` 0.1.9 · `qwen-code` 0.21.0 ·
`googleworkspace` 0.11.1 · `github-copilot` … plus npm helpers (`http-server`, `serve`).

## Browsers / headless

- **Brave** 152 — `C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe`
- **Microsoft Edge** 152 — registered (UWP path; not verified reachable without elevation)
- No Chrome / Firefox (not required - Brave covers it)
- **Playwright browsers** cached in `%LOCALAPPDATA%\ms-playwright`:
  chromium + chromium_headless_shell builds `1208/1217/1234`, plus its bundled ffmpeg.
  Matches `playwright` 1.58.0 + `browser-use` 0.13.8.
- Pipeline media path is **browser-free by default**: `executors/media_chain.py`
  tries the vendors in `providers/*.json` in priority order per artifact
  (Gemini first at `providers/020-gemini-media.json`, OpenAI fallback at
  `providers/030-openai-media.json`), falling images and narration back
  independently; `executors/gemini_media.py` is the single-vendor Gemini
  variant. Keys come only from `api_key_ref` -> vault -> env var
  (`GEMINI_API_KEY` required, `OPENAI_API_KEY` optional). The old browser path
  (`scripts/export_session.py` -> `profiles/generation_tool.json` ->
  `executors/browser_use_adapter.py`) is still there but only needed for a
  media-gen tool that has no API.

> A dedicated agent browser **BrowserOS neo / browserclaw** was recently
> **uninstalled on purpose** - the pipeline does not need it (publish uses
> official APIs; media generation is API-first with Gemini/OpenAI, and any
> residual browser-only tool still uses Playwright, not a dedicated agent
> browser). Its skill and `browseros-cli`
> PATH entry were removed so agents don't get steered to a dead browser.
> Leftover data dirs (kept, not deleted): `C:\Users\HP\.browseros`,
> `C:\Users\HP\AppData\Local\browseros-cli`. Delete only if never reinstalling.

## Python media/LLM packages (key ones)

`moviepy` 2.2.1, `av` 17.1.0, `imageio-ffmpeg`, `pydub`, `soundfile`, `pedalboard`,
`edge-tts` 7.2.7, `gTTS`, `pyttsx3`, `faster-whisper` 1.2.1, `pysubs2`, `elevenlabs`,
`openai`, `anthropic`, `google-genai`, `groq`, `transformers`+`torch`,
`moviepy`, `playwright` 1.58.0, `browser-use` 0.13.8, `google-api-python-client` 2.188.0,
`google-auth-oauthlib` 1.2.4, `fastapi`/`uvicorn`, `mcp`. Full set: `pip list`.

## Command availability fixes made

Ruff / mypy / keyring were installed but their console scripts sat in
`C:\Users\HP\AppData\Roaming\Python\Python314\Scripts` - **added to user PATH**.
Now `ruff`, `mypy`, `keyring` resolve directly (fallback: `python -m ruff|mypy|keyring`).

## Pipeline repo status (`./`)

- **18/18 test modules pass** (`python -m tests.test_*`)
- `ruff check .` and `mypy .` clean (52 source files)
- **7/7 manifests** load via `orchestrator.manifest.load_manifest`
- `requirements.txt` now pins `keyring==25.7.0` (was an unpinned `==?` placeholder).
  Vault's OS-keychain path verified against the real **Windows Credential Locker**
  (`keyring.backends.Windows.WinVaultKeyring`); README "what changed" updated.
- `browser-use`, `playwright` (+ Chromium), the Gemini/OpenAI media providers,
  and the multi-vendor `media_chain` executor are all wired and green (see the
  README tested table).
- To re-verify: `pip install -r requirements.txt` then run the suite from `./`.

## Remaining project-side setup (not system tools)

- Set `GEMINI_API_KEY` (required) and, to make the media chain resilient,
  `OPENAI_API_KEY` (optional fallback) - vault/env var only, never a committed
  file. Only if you need the browser path instead: mint
  `profiles/generation_tool.json` via `scripts/export_session.py` (login once
  to your media-gen tool) and point the manifest at the `browser_use` executor.
- Fill `canary/check.py` `EXPECTED_ELEMENTS` with the generation tool's
  selectors if you use the browser path (the canary gates scheduled runs).
- Platform credentials via the vault / env vars per `README.md`
  (`youtube_refresh_token`+ids, `instagram_ig_user_id`+token, `tiktok_access_token`).

## Gotchas to not re-learn

- Agent subprocess shells don't inherit your interactive PowerShell env: an
  agent's `python -c` probe saw no `GEMINI_API_KEY` even though it was set for
  you (User/Machine/Process of the agent shell), because per-session vars aren't
  persisted. So run credential-bearing `python cli.py start ...` yourself in your
  own terminal, not by asking an agent - verified the hard way this session.
- Use `python`, not `python3` in any *raw* subprocess context (CreateProcess-style
  calls don't resolve the `.cmd` shim pattern).
- Docker Desktop must be started manually after each reboot.
- Never commit/archive `profiles/*.json` or raw secrets - `scripts/backup.py`
  excludes them by construction; vault or env vars are the only accepted homes.