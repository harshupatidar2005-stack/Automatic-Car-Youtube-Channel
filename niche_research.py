"""
niche_research.py
------------------
Picks the best-performing niche RIGHT NOW, with zero human input.

Method (all free data sources):
  1. Google Trends (via pytrends) -> interest-over-time + momentum (rising vs flat vs falling)
  2. YouTube Data API search.list -> how many recent videos exist for the niche
     (competition proxy) and rough view velocity of top results (opportunity proxy)
  3. Score = (trend momentum) * (view opportunity) / (competition)

Re-run cadence is controlled by config.NICHE_REEVALUATE_DAYS. If the current
niche file is fresh, this script is a no-op (keeps channel focus/consistency,
which YouTube's algorithm rewards -- constant niche-hopping hurts a channel).

Robustness notes (each fixes a failure that actually occurred):
  * pytrends 4.9.2 passes `method_whitelist` to urllib3's Retry, which was
    removed in urllib3 2.x -- constructing TrendReq with retries raises
    TypeError. We patch the kwarg to `allowed_methods` at import time.
  * Google Trends aggressively rate-limits and frequently 429s from CI IPs.
    Trends is now strictly optional: if it's unavailable, every candidate
    scores neutrally on momentum and selection falls back to YouTube data
    alone, rather than the whole pipeline dying at step 1.
  * The scoring formula divided by a raw `totalResults` count that the API
    returns as a wildly inflated estimate, letting one candidate's noise
    dominate. Scores are now computed on log-damped, normalised components.
  * A stale-but-valid niche file is reused if fresh research fails, so a bad
    research day never blocks publishing.
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

import requests

# ---------------------------------------------------------------------------
# pytrends <-> urllib3 2.x compatibility shim (must run before TrendReq is used)
# ---------------------------------------------------------------------------
try:
    import urllib3.util.retry as _retry_mod

    if "allowed_methods" in getattr(_retry_mod.Retry.__init__, "__code__", ()).co_varnames:
        _OriginalRetry = _retry_mod.Retry

        class _CompatRetry(_OriginalRetry):
            """Accepts the removed `method_whitelist` kwarg pytrends still sends."""

            def __init__(self, *args, **kwargs):
                if "method_whitelist" in kwargs:
                    kwargs["allowed_methods"] = kwargs.pop("method_whitelist")
                super().__init__(*args, **kwargs)

        _retry_mod.Retry = _CompatRetry
except Exception:  # pragma: no cover - shim is best-effort
    pass

try:
    from pytrends.request import TrendReq
    # pytrends imports Retry directly into its own namespace, so patch there too.
    try:
        import pytrends.request as _pyt_req
        _pyt_req.Retry = _retry_mod.Retry
    except Exception:
        pass
except Exception as _exc:  # pragma: no cover
    print(f"[pytrends unavailable: {_exc}]")
    TrendReq = None


NEUTRAL_MOMENTUM = 1.0
TRENDS_ENABLED = os.environ.get("DISABLE_GOOGLE_TRENDS", "").lower() not in ("1", "true", "yes")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime:
    """Parse an ISO timestamp, tolerating both naive and tz-aware history."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _state_is_durable() -> bool:
    """Whether data/ written by this run will still exist on the next one.

    On GitHub Actions the workspace is thrown away after every job, so state
    only survives if the workflow can commit it back to the repo. That push
    needs a writable GITHUB_TOKEN; when it's read-only the commit step 403s
    and every run starts from a blank data/ directory.

    Outside CI (a normal local checkout) the directory is simply durable.
    """
    if os.environ.get("STATE_IS_DURABLE", "").lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("CI", "").lower() != "true" and not os.environ.get("GITHUB_ACTIONS"):
        return True
    # In Actions, the runner exposes the token's permission set.
    perms = os.environ.get("GITHUB_TOKEN_PERMISSIONS", "")
    if "contents=write" in perms.replace(" ", "").lower():
        return True
    # Committed state from a previous run proves the push path works.
    return os.path.exists(config.UPLOAD_LOG_FILE) or os.path.exists(config.CURRENT_NICHE_FILE)


def _load_current_niche():
    if os.path.exists(config.CURRENT_NICHE_FILE):
        try:
            with open(config.CURRENT_NICHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[current_niche.json unreadable ({exc}), will re-research]")
    return None


def _is_fresh(niche_data) -> bool:
    if not niche_data or "chosen_at" not in niche_data or "niche" not in niche_data:
        return False
    try:
        chosen_at = _parse_iso(niche_data["chosen_at"])
    except (ValueError, TypeError):
        return False
    return _utcnow() - chosen_at < timedelta(days=config.NICHE_REEVALUATE_DAYS)


def _trend_momentum(keyword: str) -> float:
    """Returns a momentum score: >1 means rising interest, <1 means declining."""
    if TrendReq is None or not TRENDS_ENABLED:
        return NEUTRAL_MOMENTUM
    try:
        pytrends = TrendReq(hl="en-US", tz=0, timeout=(10, 25))
        pytrends.build_payload([keyword], timeframe="today 3-m")
        df = pytrends.interest_over_time()
        if df is None or df.empty or keyword not in df:
            return NEUTRAL_MOMENTUM
        series = df[keyword].astype(float)
        if len(series) < 4:
            return NEUTRAL_MOMENTUM
        midpoint = len(series) // 2
        first_half = series.iloc[:midpoint].mean()
        second_half = series.iloc[midpoint:].mean()
        if not first_half or first_half <= 0:
            return NEUTRAL_MOMENTUM
        # Clamp: a 10x swing is noise, not signal, and would dominate scoring.
        return round(min(max(second_half / first_half, 0.2), 3.0), 3)
    except Exception as exc:
        print(f"  [trend lookup unavailable for '{keyword}': {type(exc).__name__}]")
        return NEUTRAL_MOMENTUM


def _youtube_competition_and_views(keyword: str):
    """Returns (recent_video_count, avg_views_of_recent_uploads)."""
    api_key = os.environ.get("YOUTUBE_DATA_API_KEY", "")
    if not api_key:
        # search.list needs an API key (not just OAuth) and has its own quota
        # cost, so without one every candidate scores identically on this axis.
        return 500, 10000

    try:
        search_resp = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "id", "q": keyword, "type": "video", "order": "viewCount",
                "publishedAfter": (_utcnow() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "maxResults": 25, "relevanceLanguage": "en", "key": api_key,
            }, timeout=20,
        )
        if search_resp.status_code == 403:
            print("  [YouTube API quota exhausted or key invalid -- neutral scoring]")
            return 500, 10000
        search_resp.raise_for_status()
        data = search_resp.json()
    except Exception as exc:
        print(f"  [YouTube search failed: {type(exc).__name__}]")
        return 500, 10000

    video_ids = [item["id"]["videoId"] for item in data.get("items", [])
                 if isinstance(item.get("id"), dict) and item["id"].get("videoId")]
    recent_count = data.get("pageInfo", {}).get("totalResults", len(video_ids)) or len(video_ids)

    if not video_ids:
        return max(recent_count, 1), 0

    try:
        stats_resp = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "statistics", "id": ",".join(video_ids[:25]), "key": api_key},
            timeout=20,
        )
        stats_resp.raise_for_status()
        stats = stats_resp.json()
    except Exception as exc:
        print(f"  [YouTube stats failed: {type(exc).__name__}]")
        return max(recent_count, 1), 0

    views = []
    for item in stats.get("items", []):
        try:
            views.append(int(item.get("statistics", {}).get("viewCount", 0)))
        except (TypeError, ValueError):
            continue
    # Median resists the single viral outlier that would otherwise decide the niche.
    if views:
        views.sort()
        mid = len(views) // 2
        typical = views[mid] if len(views) % 2 else (views[mid - 1] + views[mid]) / 2
    else:
        typical = 0
    return max(recent_count, 1), typical


def score_niche(keyword: str) -> dict:
    momentum = _trend_momentum(keyword)
    competition, typical_views = _youtube_competition_and_views(keyword)

    # Log-damp both YouTube axes: raw counts span several orders of magnitude
    # and the API's totalResults is an estimate, so linear division let noise
    # dominate the ranking.
    view_signal = math.log10(typical_views + 10)
    competition_signal = math.log10(competition + 10)
    opportunity = momentum * view_signal / max(competition_signal, 0.5)

    return {
        "niche": keyword,
        "momentum": momentum,
        "recent_competition": competition,
        "typical_recent_views": typical_views,
        "opportunity_score": round(opportunity, 4),
    }


def _pinned_niche() -> str:
    """An explicitly pinned niche, bypassing research entirely.

    Set CHANNEL_NICHE (env or Actions secret) to lock the channel's focus.
    This also makes the pipeline fully deterministic when data/ can't be
    persisted between runs -- see _fallback_niche_for_period().
    """
    return " ".join(os.environ.get("CHANNEL_NICHE", "").split())


def _fallback_niche_for_period() -> str:
    """Deterministic niche for the current re-evaluation window.

    Persisting the chosen niche requires the Actions job to push data/ back to
    the repo. If that push is blocked (read-only GITHUB_TOKEN), every run sees
    no niche file and would otherwise re-run full research -- ~1500 YouTube
    quota units per run, and a niche that can CHANGE between runs, which
    destroys the channel focus the algorithm rewards.

    Deriving the pick from the calendar keeps it stable across runs with no
    state at all, and still rotates on the configured cadence.
    """
    period = int(_utcnow().timestamp() // (config.NICHE_REEVALUATE_DAYS * 86400))
    return config.CANDIDATE_NICHES[period % len(config.CANDIDATE_NICHES)]


def choose_best_niche(force: bool = False) -> dict:
    pinned = _pinned_niche()
    if pinned and not force:
        print(f"Using pinned niche from CHANNEL_NICHE: '{pinned}'")
        return {"niche": pinned, "chosen_at": _utcnow().isoformat(), "pinned": True}

    current = _load_current_niche()
    if not force and _is_fresh(current):
        print(f"Current niche '{current['niche']}' is still fresh "
              f"(chosen {current['chosen_at']}). Skipping re-research.")
        return current

    # Without a persisted niche file, full research would run on EVERY run.
    # Only pay that cost when the state directory is actually writable and
    # durable; otherwise fall back to a stable, calendar-derived pick.
    if current is None and not force and not _state_is_durable():
        fallback = _fallback_niche_for_period()
        print(f"No persisted niche and data/ is not durable -- using stable "
              f"calendar-derived niche '{fallback}'. "
              f"Set CHANNEL_NICHE to pin one explicitly, or enable "
              f"'Read and write permissions' for Actions so the pick persists.")
        return {"niche": fallback, "chosen_at": _utcnow().isoformat(),
                "derived": True}

    print("Scoring candidate niches against live trend + competition data...")
    results = []
    for niche in config.CANDIDATE_NICHES:
        print(f" - scoring: {niche}")
        try:
            results.append(score_niche(niche))
        except Exception as exc:
            print(f"   [scoring failed for '{niche}': {type(exc).__name__}: {exc}]")
        time.sleep(1)  # be polite to free APIs / avoid rate limits

    if not results:
        if current and current.get("niche"):
            print("All niche scoring failed -- keeping the previously chosen niche.")
            return current
        fallback = config.CANDIDATE_NICHES[0]
        print(f"All niche scoring failed and no prior niche exists -- defaulting to '{fallback}'.")
        return {"niche": fallback, "chosen_at": _utcnow().isoformat(),
                "opportunity_score": 0, "fallback": True}

    results.sort(key=lambda r: r["opportunity_score"], reverse=True)
    winner = dict(results[0])
    winner["chosen_at"] = _utcnow().isoformat()
    winner["all_scores"] = results

    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp_path = config.CURRENT_NICHE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(winner, f, indent=2)
    os.replace(tmp_path, config.CURRENT_NICHE_FILE)  # atomic: never a half-written file

    print(f"\nSelected niche: {winner['niche']} (score={winner['opportunity_score']})")
    return winner


if __name__ == "__main__":
    choose_best_niche(force="--force" in sys.argv)
