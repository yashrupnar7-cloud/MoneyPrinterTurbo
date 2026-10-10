# Modern Facts Shorts

Automated YouTube Shorts pipeline for the **Modern Facts** channel
(AI / future-technology facts). It connects the existing MoneyPrinterTurbo
services end to end:

1. **Topic + script** — Gemini via the repository's built-in LLM adapter
   (`llm_provider = "gemini"`), or pass `--script` to skip the LLM.
2. **Voiceover** — Microsoft Edge TTS (free, no API key), rate `1.1`.
3. **Visuals** — abstract 9:16 background clips generated locally with
   FFmpeg (`scripts/generate_shorts_backgrounds.py`) and fed to the stock
   pipeline through `--video-source local`. No stock-footage API key needed.
4. **Subtitles** — burnt-in captions from the Edge subtitle stage (SRT kept
   alongside the MP4).
5. **Output** — `storage/modern_facts/modern-facts-<timestamp>-<slug>.mp4`
   plus `subtitle.srt`, `script.json`, `*-meta.json`, and
   `upload-status.json`.
6. **YouTube publishing** — after generation succeeds, the workflow's
   dedicated upload step publishes the finished video to YouTube through
   the existing `app/services/upload_post.py` Upload-Post integration
   (`POST https://api.upload-post.com/api/upload`). Visibility comes from
   the `youtube_privacy` input and defaults to **private**; the run only
   succeeds when the upload API confirms success.

## GitHub Actions

Workflow: `.github/workflows/shorts.yml`. It runs on a **schedule** (twice a
day, `0 9,21 * * *` UTC) and on **workflow_dispatch** (manual), and uploads
the finished files as the artifact `modern-facts-short-<run number>`.

Scheduled runs use the defaults: a Gemini-chosen topic, the default voice,
and `private` visibility. Manual runs can override these per run.

Steps:

1. Repository **Settings → Secrets and variables → Actions** (all three
   are required — the workflow fails fast when one is missing):
   - `GEMINI_API_KEY` — Gemini API key used for topic + script.
   - `UPLOAD_POST_API_KEY` — Upload-Post API key.
   - `UPLOAD_POST_USERNAME` — your Upload-Post username (identifies the
     already-authorized YouTube channel).
2. **Actions → Modern Facts Shorts → Run workflow**, optionally supplying:
   - `topic` — fixed topic; empty means Gemini picks one.
   - `voice_name` — any Edge TTS voice, default
     `en-US-AndrewMultilingualNeural-Male`.
   - `youtube_privacy` — `private` (default), `unlisted`, or `public`.

Scheduled runs cannot supply these inputs, so they use the defaults above.
To change the cadence, edit the `cron` entry under `on.schedule` in
`.github/workflows/shorts.yml`.

Job order and failure semantics:

1. `Verify required secrets are configured` — fails immediately with an
   `::error::` message when any of the three secrets is missing, so a run
   that cannot upload never wastes a generation.
2. `Run Shorts generator` — generation only (`--no-upload`); the pipeline
   itself never publishes in CI.
3. `Upload the Short artifact` — stores `storage/modern_facts/` (MP4, SRT,
   metadata) as the run artifact before any publish attempt.
4. `Upload video to YouTube via Upload-Post` — runs only after generation
   and the artifact succeeded. It uploads the newest generated MP4 through
   the existing Upload-Post integration with `--privacy` from the
   `youtube_privacy` input, and **fails the whole run** with a clear
   `::error::YouTube upload failed: <reason>` message if the upload API
   reports an error. The workflow succeeds only when the upload succeeds.

## Authorizing YouTube (one-time, done on Upload-Post's website)

This repository never receives Google OAuth client secrets, refresh tokens,
or access tokens. The YouTube OAuth 2.0 consent happens once on the
Upload-Post platform, which stores the Google-issued tokens server-side and
publishes through them using only an Upload-Post API key that you keep as a
GitHub secret:

1. Create an account at <https://upload-post.com/>.
2. In the Upload-Post dashboard, connect/authorize your YouTube channel
   (their OAuth consent screen; review the requested YouTube scopes).
3. Create an **API key** in the Upload-Post dashboard.
4. Add two GitHub Actions secrets:
   - `UPLOAD_POST_API_KEY` = the API key from step 3
   - `UPLOAD_POST_USERNAME` = your Upload-Post username
5. Run the workflow. The first test upload should stay at
   `youtube_privacy: private`; confirm the video appears as private in
   YouTube Studio, then switch to `unlisted`/`public`.

Locally the same two values go into environment variables (never into
source code):

```powershell
$env:UPLOAD_POST_API_KEY = "..."   # do not commit or paste into files
$env:UPLOAD_POST_USERNAME = "..."
```

Both are written only to `config.toml` (gitignored) and never appear in
logs, command lines, summaries, or the artifact.

## Local usage

```bash
# everything automated (topic + script from Gemini, private YouTube upload)
uv sync --frozen --python 3.11
set GEMINI_API_KEY=your-key
set UPLOAD_POST_API_KEY=your-upload-post-key
set UPLOAD_POST_USERNAME=your-upload-post-username
uv run --no-sync python -X utf8 scripts/run_modern_facts_shorts.py

# fixed topic, Gemini script
uv run --no-sync python -X utf8 scripts/run_modern_facts_shorts.py --topic "AI agents now run data centers"

# fully offline script, no publishing even if secrets are set
uv run --no-sync python -X utf8 scripts/run_modern_facts_shorts.py --script "Hook. Body. Ending." --no-upload

# reusing your own footage instead of generated backgrounds
uv run --no-sync python -X utf8 scripts/run_modern_facts_shorts.py --materials "D:\clips\a.mp4,D:\clips\b.mp4"
```

Useful flags: `--voice-name`, `--voice-rate`, `--paragraph-number`,
`--background-count`, `--background-duration`, `--output-dir`,
`--stop-at` (`script` to preview the narration only), `--no-upload`,
`--youtube-privacy`, `--upload-platforms`, `--cross-post-timeout`,
`--cli-arg` to pass anything else straight to `cli.py`.

Background clips only:

```bash
uv run --no-sync python scripts/generate_shorts_backgrounds.py --count 8 --duration 12
```

## How publishing works (and its failure semantics)

**In GitHub Actions** (`.github/workflows/shorts.yml`):

- The generator runs with `--no-upload`, so generation and publishing are
  fully separated steps.
- The dedicated upload step runs `scripts/upload_short_youtube.py`, which
  picks the newest MP4 in `storage/modern_facts/`, reads the title from
  its sibling `*-meta.json`, and calls the existing
  `app/services/upload_post.py` (`cross_post_video`, platform
  `youtube`) with credentials from the two environment secrets. No API
  details are reimplemented or invented.
- The YouTube visibility is passed explicitly as `privacyStatus` from the
  `youtube_privacy` workflow input (default `private`), so the upload can
  never silently fall back to the config file's visibility.
- Exit code `0` means Upload-Post confirmed the publish succeeded; any
  API error exits `1` and emits `::error::YouTube upload failed: <reason>`,
  failing the run. Credentials are never printed.

**Locally** (optional, same code base):

- When `UPLOAD_POST_API_KEY` and `UPLOAD_POST_USERNAME` are set in the
  environment, `scripts/run_modern_facts_shorts.py` lets the stock
  `task.py` cross-post path publish after `generate_final_videos`
  succeeds (`upload_post_enabled` + `upload_post_auto_upload` are written
  to `config.toml` from the environment).
- `cli.py` waits for that background upload (`--cross-post-timeout`,
  default 3600 s) and prints the final `cross_post_state` /
  `cross_post_error` in its result JSON.
- The orchestrator writes the outcome to
  `storage/modern_facts/upload-status.json` with `status` = `uploaded`,
  `failed`, `disabled` (secrets not configured), or `skipped` (generation
  stopped before the video stage).
- A local upload failure is **reported, never fatal to generation**: the
  MP4, SRT, and metadata are already copied and the generator exits `0`;
  check `upload-status.json` or use `--verify-upload-status` to fail on it.

## Secrets and configuration

| Purpose | Environment / GitHub Secret | Required |
| --- | --- | --- |
| Topic + script + post metadata (Gemini) | `GEMINI_API_KEY` | yes |
| YouTube publishing (Upload-Post API key) | `UPLOAD_POST_API_KEY` | yes |
| YouTube publishing (Upload-Post username) | `UPLOAD_POST_USERNAME` | yes |

All three are required for the workflow: it fails fast when one is
missing. Locally, the two Upload-Post values are only needed when you want
the generator to publish (otherwise pass `--no-upload`).

- Nothing else is paid or keyed: Edge TTS, the bundled
  `resource/songs/*.mp3` background music, and the local FFmpeg backgrounds
  are all free.
- `scripts/run_modern_facts_shorts.py` creates `config.toml` from
  `config.example.toml` before anything in `app/` is imported, then sets
  `llm_provider = "gemini"`, `gemini_api_key`, and — when the Upload-Post
  secrets are present — `upload_post_enabled`, `upload_post_api_key`,
  `upload_post_username`, `upload_post_platforms = ["youtube"]`,
  `upload_post_auto_upload = true`, and
  `upload_post_youtube_privacy_status` (default `private`). All other
  settings stay at their shipped defaults.

## Testing one private YouTube upload

Workflow (recommended first test):

1. Add the three secrets (`GEMINI_API_KEY`, `UPLOAD_POST_API_KEY`,
   `UPLOAD_POST_USERNAME`).
2. **Actions → Modern Facts Shorts → Run workflow** with
   `youtube_privacy = private`.
3. Watch the `Upload video to YouTube via Upload-Post` step: it prints
   `YouTube upload succeeded: <file>` or fails the run with
   `::error::YouTube upload failed: <reason>`.
4. Confirm the video in YouTube Studio (visibility: private).

Local equivalent (same integration, environment secrets):

```powershell
$env:GEMINI_API_KEY = "..."
$env:UPLOAD_POST_API_KEY = "..."
$env:UPLOAD_POST_USERNAME = "..."
uv run --no-sync python -X utf8 scripts/run_modern_facts_shorts.py --topic "AI agents now run data centers"
uv run --no-sync python -X utf8 scripts/upload_short_youtube.py --privacy private
```

The verify command exits non-zero only when a configured upload failed, so
it can be used as a standalone check anywhere.

## Tests

```bash
uv run --no-sync python -X utf8 -m pytest -q test/services/test_modern_facts_shorts.py test/services/test_upload_short_youtube.py
uv run --no-sync ruff check scripts test/services/test_modern_facts_shorts.py test/services/test_upload_short_youtube.py
```
