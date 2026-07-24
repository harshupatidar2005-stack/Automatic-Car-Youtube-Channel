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
python src/get_refresh_token.py
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
  re-evaluation early with `python src/niche_research.py --force`.
- **Groq's free tier changes over time** — check
  console.groq.com/docs/models if `GROQ_MODEL` in `config.py` ever 404s.
- **Voice/visual sameness**: to keep this at $0, all videos use one TTS
  voice and stock footage rather than custom animation. This is a
  reasonable, common approach for faceless channels, but it's a
  deliberate trade-off, not a limitation you need to fix.

## Repo layout
```
config.py                  # cadence, resolutions, candidate niches
src/niche_research.py      # picks the niche
src/script_writer.py       # writes scripts via Groq
src/tts_voiceover.py       # free TTS
src/video_assembler.py     # stock footage + ffmpeg assembly
src/thumbnail_gen.py       # thumbnail generation
src/youtube_uploader.py    # scheduled upload
src/get_refresh_token.py   # one-time OAuth helper (run locally)
src/orchestrator.py        # ties it all together, called by GitHub Actions
.github/workflows/         # the free scheduler
data/                      # niche/queue/upload state (auto-committed)
```
