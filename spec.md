# Faster-Whisper Local STT — Implementation Spec

**Branch:** `faster-whisper-stt`
**Fork:** `agbocsardi/nanobot` (upstream: `HKUDS/nanobot`)
**Started:** 2026-04-15
**Status:** Implementation complete (Phases 1–6). E2E smoke test and commit pending.

## Goal

Add a local `faster-whisper` transcription provider to nanobot so Telegram (and other channels) can transcribe voice notes on-device, with no API key, replacing the same capability currently provided by a custom skill in the user's openclaw setup.

## Non-goals

- Replacing the existing `groq` / `openai` HTTP providers (they stay as defaults and options)
- Adding `faster-whisper` as a hard dependency of nanobot itself (isolated venv at runtime)
- Introducing `uv.lock` to the nanobot repo (separate decision, separate PR)
- Upstreaming to `HKUDS/nanobot` in this round — this lives on the personal fork first

## Design decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Integration style | Provider (not skill) | Runs in `channels/*.transcribe_audio` before the LLM sees the message — automatic for voice notes, no LLM decision needed |
| Execution | Subprocess shell-out to an isolated venv | Smallest blast radius; keeps `faster-whisper` out of nanobot's own deps; matches openclaw's working pattern |
| Venv location | `~/.nanobot/whisper-env` | Fits nanobot's existing `~/.nanobot/*` data-dir convention |
| Env/dep tooling | `uv` exclusively | User preference (saved to memory) |
| Default model | `small` / `cpu` / `int8` | Matches user's openclaw setup; proven working |
| Language | Auto-detect, no hint | User preference |
| Transcript framing | None in provider; let Telegram's existing `[transcription: …]` wrapper stand | Avoids double-wrapping; preserves CONTRIBUTING.md's "simple, clear, decoupled" style |
| Provider selection | `transcription_provider: "faster_whisper"` in config | Matches existing `"groq"` / `"openai"` pattern |
| Contribution target (if upstreamed) | `nightly` | It's a new feature per CONTRIBUTING.md branching rules |

## File change inventory

| # | File | Action | Purpose |
|---|------|--------|---------|
| 1 | `nanobot/providers/whisper/__init__.py` | **new** | Package marker |
| 2 | `nanobot/providers/whisper/transcribe.py` | **new** | Standalone script run by the venv python; prints clean transcript to stdout |
| 3 | `nanobot/providers/transcription.py` | edit | Add `FasterWhisperTranscriptionProvider` class |
| 4 | `nanobot/config/schema.py` | edit | Add `FasterWhisperConfig`; widen `transcription_provider` docstring |
| 5 | `nanobot/config/paths.py` | edit | Add `get_whisper_env_dir()` helper |
| 6 | `nanobot/channels/base.py` | edit | Third branch in `transcribe_audio`; fix empty-key short-circuit for local providers |
| 7 | `nanobot/channels/manager.py` | edit | Extend `_resolve_transcription_key` to handle `faster_whisper` |
| 8 | `tests/providers/test_transcription.py` | **new** | Unit tests with mocked subprocess (9 tests) |
| 9 | `tests/channels/test_base_channel.py` | edit | Transcription wiring tests: key short-circuit, config injection (4 new tests) |

**Net diff:** 6 edited files + 3 new files = 286 insertions, 45 deletions.

**No changes to:** bus, agent loop, any channel code besides `base.py`/`manager.py`, `pyproject.toml`.

## Todo list

### Phase 0 — Setup (done ✅)

- [x] **2026-04-15** — Fork `HKUDS/nanobot` to `agbocsardi/nanobot` via `gh repo fork --remote`
- [x] **2026-04-15** — Create feature branch `faster-whisper-stt` off `main`
- [x] **2026-04-15** — Read-only exploration: `config/schema.py`, `channels/manager.py`, `config/paths.py`, tests layout
- [x] **2026-04-15** — Create isolated venv: `uv venv ~/.nanobot/whisper-env --python 3.11`
- [x] **2026-04-15** — Install dependency: `uv pip install --python ~/.nanobot/whisper-env/bin/python faster-whisper`

### Phase 1 — Standalone script & smoke test

- [x] **2026-04-15** — Write `nanobot/providers/whisper/transcribe.py`
  - Ported from `~/.openclaw/skills/voice-transcribe/scripts/transcribe.py`
  - Signature: `transcribe.py <audio_file> [language]`
  - Output: clean transcript to stdout (**no** `[Voice note from Gergő …]` framing — Telegram channel wraps it)
  - Exit codes: `0` success, `1` bad args / file not found, `2` transcription error
- [x] **2026-04-15** — Add `nanobot/providers/whisper/__init__.py` (empty package marker)
- [x] **2026-04-15** — Smoke test the script standalone: run `~/.nanobot/whisper-env/bin/python nanobot/providers/whisper/transcribe.py <sample.ogg>`
  - First run downloads the `small` model (~900MB) to `~/.cache/huggingface/` (shared with openclaw — no duplicate)
  - Verify clean transcript on stdout, nothing on stderr

### Phase 2 — Config layer

- [x] **2026-04-15** — Add `get_whisper_env_dir()` to `nanobot/config/paths.py`
  - Returns `Path.home() / ".nanobot" / "whisper-env"`
  - No `ensure_dir` — this one is user-managed, not auto-created
- [x] **2026-04-15** — Add `FasterWhisperConfig` to `nanobot/config/schema.py`
  - Nested under `ChannelsConfig` like `DreamConfig` is nested under `AgentDefaults`
  - Fields (all optional with defaults):
    - `venv_python: str = "~/.nanobot/whisper-env/bin/python"`
    - `script_path: str = ""` (empty → resolve to bundled `nanobot/providers/whisper/transcribe.py`)
    - `model: str = "small"`
    - `device: str = "cpu"`
    - `compute_type: str = "int8"`
- [x] **2026-04-15** — Update `transcription_provider` comment to list all three options

### Phase 3 — Provider class

- [x] **2026-04-15** — Add `FasterWhisperTranscriptionProvider` to `nanobot/providers/transcription.py`
  - Accepts config (venv python path, script path, model, device, compute_type)
  - `async def transcribe(file_path)`:
    - Resolves venv python path (`expanduser`)
    - Falls back to bundled `transcribe.py` via `importlib.resources` / `Path(__file__).parent / "whisper" / "transcribe.py"` if `script_path` empty
    - Runs via `asyncio.create_subprocess_exec` (not shell=True)
    - Passes model/device/compute_type as env vars (`NANOBOT_WHISPER_MODEL` etc.) so script can pick them up without CLI arg sprawl
    - Captures stdout → returns stripped transcript
    - Logs stderr on non-zero exit, returns `""` (matches groq/openai failure behavior)
    - Timeout: 120s (long enough for a long voice note on CPU)
- [x] **2026-04-15** — Update `transcribe.py` to read model/device/compute_type from env vars with fallbacks to the openclaw defaults

### Phase 4 — Wiring

- [x] **2026-04-15** — Edit `nanobot/channels/base.py:transcribe_audio`
  - Add `elif self.transcription_provider == "faster_whisper"` branch
  - Instantiate `FasterWhisperTranscriptionProvider` with config injected from the channel (need to pass the `FasterWhisperConfig` through the same mechanism `transcription_api_key` uses)
  - Fix `if not self.transcription_api_key: return ""` → only skip when provider actually needs a key (groq/openai). Cleanest: check `self.transcription_provider not in _LOCAL_PROVIDERS` where `_LOCAL_PROVIDERS = {"faster_whisper"}`
- [x] **2026-04-15** — Add `transcription_faster_whisper: FasterWhisperConfig | None = None` attribute on `BaseChannel` (mirrors `transcription_api_key`)
- [x] **2026-04-15** — Edit `nanobot/channels/manager.py:_resolve_transcription_key`
  - Add `if provider == "faster_whisper": return ""` early return
- [x] **2026-04-15** — Edit `nanobot/channels/manager.py:_init_channels`
  - Inject `channel.transcription_faster_whisper = self.config.channels.faster_whisper` when provider is `faster_whisper`

### Phase 5 — Tests

- [x] **2026-04-15** — Create `tests/providers/` directory if it doesn't exist (it does — confirmed)
- [x] **2026-04-15** — Create `tests/providers/test_transcription.py`
  - Test: `FasterWhisperTranscriptionProvider.transcribe` returns stdout on success (mock `create_subprocess_exec`)
  - Test: returns `""` on non-zero exit (and logs stderr)
  - Test: returns `""` on missing file
  - Test: returns `""` on timeout
  - Test: honors env vars for model/device/compute_type
  - Test: falls back to bundled script path when `script_path` is empty
- [x] **2026-04-15** — Add an integration test extension to `tests/channels/test_base_channel.py` to verify:
  - Empty-key short-circuit no longer blocks `faster_whisper`
  - Existing groq/openai short-circuit still works
  - `faster_whisper` config is passed through to provider constructor

### Phase 6 — Lint, format, full test run

- [x] **2026-04-15** — `ruff check nanobot/` on changed files — all clean
- [x] **2026-04-15** — `ruff format` — no changes needed
- [x] **2026-04-15** — `pytest tests/providers/test_transcription.py -v` — 9/9 passed
- [x] **2026-04-15** — `pytest tests/channels/test_whatsapp_channel.py tests/channels/test_base_channel.py -v` — 22/22 passed
- [x] **2026-04-15** — `pytest` full suite — 1696 passed, 1 pre-existing failure (unrelated `test_edit_warns_if_file_modified_since_read`)

### Phase 7 — End-to-end smoke test (pending)

- [ ] **2026-04-15** — Edit local nanobot config: set `channels.transcription_provider = "faster_whisper"`
- [ ] **2026-04-15** — Start nanobot with Telegram channel enabled
- [ ] **2026-04-15** — Send a real voice note from Telegram
- [ ] **2026-04-15** — Verify logs show: `Transcribed voice: <first 50 chars>...`
- [ ] **2026-04-15** — Verify the LLM reply is coherent (confirms the transcript reached it)
- [ ] **2026-04-15** — Revert local config change if desired (the feature stays behind a flag)

### Phase 8 — Commit & push

- [ ] **2026-04-15** — `git add -A && git status` — review the diff one last time
- [ ] **2026-04-15** — Commit with a focused message (CONTRIBUTING.md favors focused patches)
- [ ] **2026-04-15** — `git push -u origin faster-whisper-stt` (pushes to **fork**, never upstream)

## Safety rails

- **Never push to `upstream`** — work only lives on `origin` (personal fork)
- **Read before edit** — every file touched is read in full first
- **One file at a time** — pause to confirm diffs before running multi-file edits
- **No dep additions to `pyproject.toml`** — whisper stays in the isolated venv
- **No destructive git ops** — no `reset --hard`, no `push --force`, no branch deletion
- **Tests before commit** — full `pytest` must pass
- **Lint before commit** — `ruff check` clean
- **No changes to `~/.openclaw`** — the openclaw setup stays untouched as a working reference

## Open questions for future phases (not blocking this work)

- Should the bundled `transcribe.py` also be usable as a skill (Option A from earlier discussion) so users can transcribe arbitrary audio files on demand? Low priority — can be a follow-up PR.
- Should we introduce `uv.lock` for the whole repo? Separate decision; out of scope here.
- GPU support (`device="cuda"`, `compute_type="float16"`) — works today via config override, but needs a docs note if we want to advertise it.

## Reference links

- Openclaw source: `/home/gergo/.openclaw/skills/voice-transcribe/`
- Openclaw venv (reference only, not touched): `/home/gergo/.openclaw/whisper-env`
- Upstream repo: https://github.com/HKUDS/nanobot
- Personal fork: https://github.com/agbocsardi/nanobot
- CONTRIBUTING.md: `./CONTRIBUTING.md` (style rules: simple, clear, decoupled, honest, durable)
