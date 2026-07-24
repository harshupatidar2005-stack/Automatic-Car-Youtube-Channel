"""
niche_research.py
------------------
Picks the best-performing niche RIGHT NOW, with zero human input.

Method (all free data sources):
  1. Google Trends (via pytrends) -> interest-over-time + momentum (rising vs flat vs falling)
  2. YouTube Data API search.list -> how many recent videos exist for the niche
     (competition proxy) and rough view velocity of top results (opportunity proxy)
  3. Score = (trend momentum) * (avg views of recent top videos) / (competition count)

Re-run cadence is controlled by config.NICHE_REEVALUATE_DAYS. If the current
niche file is fresh, this script is a no-op (keeps channel focus/consistency,
which YouTube's algorithm rewards -- constant niche-hopping hurts a channel).
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.append(os.path.abspath(__file__))
import config

try:
    from pytrends.request import TrendReq
except ImportError:
    TrendReq = None

import requests


def _load_current_niche():
    if os.path.exists(config.CURRENT_NICHE_FILE):
        with open(config.CURRENT_NICHE_FILE) as f:
            return json.load(f)
    return None


def _is_fresh(niche_data):
    if not niche_data:
        return False
    chosen_at = datetime.fromisoformat(niche_data["chosen_at"])
    return datetime.utcnow() - chosen_at < timedelta(days=config.NICHE_REEVALUATE_DAYS)


def _trend_momentum(keyword: str) -> float:
    """Returns a 0-2 score: >1 means rising interest, <1 means declining."""
    if TrendReq is None:
        return 1.0  # neutral fallback if pytrends isn't installed
    try:
        pytrends = TrendReq(hl="en-US", tz=360)
        pytrends.build_payload([keyword], timeframe="today 3-m")
        df = pytrends.interest_over_time()
        if df.empty or keyword not in df:
            return 0.5
        series = df[keyword]
        first_half_avg = series.iloc[: len(series) // 2].mean()
        second_half_avg = series.iloc[len(series) // 2:].mean()
        if first_half_avg == 0:
            return 1.0
        return round(second_half_avg / first_half_avg, 3)
    except Exception as e:
        print(f"  [trend lookup failed for '{keyword}': {e}]")
        return 0.5


def _youtube_competition_and_views(keyword: str):
    """Returns (recent_video_count, avg_views_of_top_10) using YouTube Data API."""
    api_key = os.environ.get("YOUTUBE_DATA_API_KEY", "")
    if not api_key:
        # Falls back to neutral numbers if no separate API key is set for search
        # (search.list needs an API key, not just OAuth, and has its own quota cost)
        return 500, 10000

    search_url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "id",
        "q": keyword,
        "type": "video",
        "order": "date",
        "publishedAfter": (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "maxResults": 25,
        "key": api_key,
    }
    resp = requests.get(search_url, params=params, timeout=15).json()
    video_ids = [item["id"]["videoId"] for item in resp.get("items", [])]
    recent_count = resp.get("pageInfo", {}).get("totalResults", len(video_ids))

    if not video_ids:
        return recent_count, 0

    stats_url = "https://www.googleapis.com/youtube/v3/videos"
    stats_params = {"part": "statistics", "id": ",".join(video_ids[:25]), "key": api_key}
    stats_resp = requests.get(stats_url, params=stats_params, timeout=15).json()
    views = [int(item["statistics"].get("viewCount", 0)) for item in stats_resp.get("items", [])]
    avg_views = sum(views) / len(views) if views else 0
    return recent_count, avg_views


def score_niche(keyword: str) -> dict:
    momentum = _trend_momentum(keyword)
    competition, avg_views = _youtube_competition_and_views(keyword)
    competition = max(competition, 1)
    opportunity = (momentum * avg_views) / competition
    return {
        "niche": keyword,
        "momentum": momentum,
        "recent_competition": competition,
        "avg_recent_views": avg_views,
        "opportunity_score": round(opportunity, 2),
    }


def choose_best_niche(force=False) -> dict:
    current = _load_current_niche()
    if not force and _is_fresh(current):
        print(f"Current niche '{current['niche']}' is still fresh "
              f"(chosen {current['chosen_at']}). Skipping re-research.")
        return current

    print("Scoring candidate niches against live trend + competition data...")
    results = []
    for niche in config.CANDIDATE_NICHES:
        print(f" - scoring: {niche}")
        results.append(score_niche(niche))
        time.sleep(1)  # be polite to free APIs / avoid rate limits

    results.sort(key=lambda r: r["opportunity_score"], reverse=True)
    winner = results[0]
    winner["chosen_at"] = datetime.utcnow().isoformat()
    winner["all_scores"] = results

    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.CURRENT_NICHE_FILE, "w") as f:
        json.dump(winner, f, indent=2)

    print(f"\nSelected niche: {winner['niche']} (score={winner['opportunity_score']})")
    return winner


if __name__ == "__main__":
    force = "--force" in sys.argv
    choose_best_niche(force=force)
