# YouTube Automation Pipeline (Shorts + Long-form, $0/month)

Fully automated: niche selection, scripting, voiceover, video assembly,
thumbnails, and scheduled uploads for both Shorts and long-form — running
on a free schedule via GitHub Actions. Once set up, it makes every content
decision itself, forever (re-checking the market monthly).

## What this does every run (no input from you)

1. **Picks the niche** — scores a candidate list against live Google
   Trends momentum and recent YouTube competition/views, and locks in the
   winner for 30 days at a time (channel focus matters for the algorithm,
   so it doesn't hop niches daily).
2. **Writes scripts** — one long-form (~8 min) + a few derived Shorts,
   picking the exact topic/angle itself within the niche.
3. **Generates voiceover** — free neural TTS (edge-tts).
4. **Assembles video** — pulls matching free stock footage (Pexels/Pixabay),
   times it to the narration, burns in captions.
5. **Makes a thumbnail** — bold text over a video frame.
6. **Uploads to YouTube** — schedules Shorts multiple times/day and
   long-form a few times/week, sets tags/description/#Shorts automatically.

## The one thing that can't be automated: initial account access

YouTube requires a human to click "Allow" on Google's OAuth consent screen
at least once — no code can do this for you, by design (it's how Google
prevents silent account takeover). Everything else after that is hands-off.

## Setup (about 20 minutes, one time only)

### 1. Free accounts/keys you need
| Service | Why | Cost |
|---|---|---|
| [Groq Console](https://console.groq.com) | Script generation (Llama models) | Free tier |
| [Pexels API](https://www.pexels.com/api/) | Stock video footage | Free |
| [Pixabay API](https://pixabay.com/api/docs/) | Stock video fallback | Free |
| [Google Cloud Console](https://console.cloud.google.com) | YouTube Data API v3 + OAuth | Free |
| GitHub | Hosting the code + free scheduler (Actions) | Free |

### 2. Enable the YouTube Data API + get OAuth credentials
1. Create a project in Google Cloud Console.
2. Enable "YouTube Data API v3".
3. Create OAuth credentials, type **Desktop app**. Download as `client_secret.json`.
4. Also create an **API key** (separate from OAuth) for search/competition
   lookups — set as `YOUTUBE_DATA_API_KEY`.

### 3. Get your one-time refresh token
```bash
pip install google-auth-oauthlib
python get_refresh_token.py
```
A browser opens once — log in with the Google account that owns your
channel, approve, and copy the three printed values.

### 4. Add secrets to your GitHub repo
Repo → Settings → Secrets and variables → Actions → New repository secret:
`GROQ_API_KEY`, `PEXELS_API_KEY`, `PIXABAY_API_KEY`, `YOUTUBE_CLIENT_ID`,
`YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`, `YOUTUBE_DATA_API_KEY`.

### 5. Push this repo to GitHub and enable Actions
That's it — the workflow in `.github/workflows/automation.yml` runs 3x/day
automatically from then on, for free, using GitHub's own compute.

## Honest limitations, please read

- **YouTube quota**: uploads cost 1,600 of your 10,000 daily API quota
  units — so realistically ~6 uploads/day max on the free tier. The
  default cadence (3 Shorts/day + long-form 3x/week) is well within that.
- **AI-content policy**: YouTube demonetizes/suspends channels seen as
  "mass-produced, repetitive, reused" (their spam policy, tightened
  2024–2025). This pipeline generates a genuinely new script/topic each
  time rather than templating the same video — but you should
  occasionally spot-check output quality, especially early on.
- **First few weeks are the real test**: I've built the pipeline as sound
  engineering, but no one can guarantee a niche's real-world performance
  in advance — that's true for human creators too. Watch the first
  month's analytics; if the niche pick underperforms, force a
  re-evaluation early with `python niche_research.py --force`.
- **Groq's free tier changes over time** — check
  console.groq.com/docs/models if `GROQ_MODEL` in `config.py` ever 404s.
- **Voice/visual sameness**: to keep this at $0, all videos use one TTS
  voice and stock footage rather than custom animation. This is a
  reasonable, common approach for faceless channels, but it's a
  deliberate trade-off, not a limitation you need to fix.

## Repo layout
```
config.py               # cadence, resolutions, candidate niches
media.py                # ffmpeg/font discovery, caption rendering, duration probing
niche_research.py       # picks the niche
script_writer.py        # writes scripts via Groq
tts_voiceover.py        # free TTS
video_assembler.py      # stock footage + ffmpeg assembly
thumbnail_gen.py        # thumbnail generation
youtube_uploader.py     # scheduled upload
get_refresh_token.py    # one-time OAuth helper (run locally)
orchestrator.py         # ties it all together, called by GitHub Actions
tests/test_pipeline.py  # offline end-to-end + regression tests
.github/workflows/      # the free scheduler
data/                   # niche/queue/upload state (auto-committed)
```

## Running it yourself

```bash
pip install -r requirements.txt

# Render a real video end-to-end WITHOUT publishing anything.
# Artifacts land in data/dry_run/<id>/ so you can watch them first.
python orchestrator.py --dry-run

# The real thing (needs the YouTube secrets set)
python orchestrator.py

# Re-pick the niche immediately instead of waiting for the 30-day cycle
python orchestrator.py --force-niche

# Offline test suite -- renders actual MP4s with ffmpeg, hits no network
python -m pytest tests/ -q
```

`ffmpeg` is used for all rendering. If it isn't on your PATH the pipeline
falls back to the `imageio-ffmpeg` wheel automatically; you can also point
`FFMPEG_BINARY` / `CAPTION_FONT` at specific paths.

Useful environment overrides: `TTS_VOICE` (any `edge-tts --list-voices`
name), `DISABLE_GOOGLE_TRENDS=1` (skip Trends when it rate-limits CI), and
`CHANNEL_NICHE="psychology facts"` to pin the niche and skip research
entirely (saves ~1500 YouTube quota units per run).

## If Actions can't save state (the `data/` 403)

The workflow commits `data/*.json` back to the repo so runs remember the
niche, queue and schedule. That push needs a writable token:

**Settings → Actions → General → Workflow permissions → "Read and write
permissions" → Save**

Without it the commit step 403s and every run starts from a blank `data/`.
**The pipeline still works correctly in that state** — it just can't learn
from previous runs:

- Publish slots are assigned **deterministically from each run's own cron
  hour**, so the 06:00/12:00/19:00 runs get 13:00/17:00/21:00 with no shared
  state. (Previously they'd all see an empty log and pick the same slot,
  publishing several videos at the same minute.)
- The niche falls back to a **stable calendar-derived pick** instead of
  re-running research every time, which would burn ~4500 quota units/day and
  let the niche drift between runs. Set `CHANNEL_NICHE` to pin it explicitly.
- The state-commit failure is a **warning, not a job failure** — the video
  was already produced and uploaded.

Turning the setting on is still worth it: it enables real niche research,
cross-run dedup, and the upload history.

## How it behaves when things break

Free APIs fail constantly, so the pipeline degrades instead of dying:

| Failure | Behaviour |
|---|---|
| Google Trends unavailable/rate-limited | Niches score on YouTube data alone |
| YouTube quota exhausted for search | Neutral competition scores, run continues |
| All niche scoring fails | Keeps the previously chosen niche |
| Groq returns prose-wrapped or malformed JSON | Extracted/repaired, then retried |
| Groq rate-limits or 5xx | Retried with backoff; partial queue is saved |
| No stock footage for a keyword | Falls back to a generated gradient background |
| A stock clip is corrupt | That scene re-renders on the fallback background |
| edge-tts outage | Timed silent track keeps the run alive |
| Custom thumbnails not enabled on channel | Video still uploads |
| Production crashes mid-render | Item is quarantined to `data/failed/`, run exits non-zero |

Videos are only removed from the queue after a successful upload, so a
crash can never silently lose a generated script.
