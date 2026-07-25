"""
End-to-end and regression tests for the pipeline.

These run fully offline. Only the true network boundaries are stubbed
(Groq, Pexels/Pixabay, edge-tts, YouTube); everything else -- ffmpeg
rendering, Pillow caption/thumbnail drawing, queue and scheduling logic --
executes for real, so the test actually proves the pipeline produces a
playable MP4 and a valid thumbnail.

Run:  python -m pytest tests/ -v      (or: python tests/test_pipeline.py)
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import media  # noqa: E402


def _make_stock_clip(path: str, seconds: float = 2.0, fps: int = 25, size: str = "640x360"):
    """A stand-in for downloaded stock footage, at a deliberately odd fps."""
    media.run_ffmpeg(["-f", "lavfi", "-i", f"testsrc=s={size}:d={seconds}:r={fps}",
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", path],
                     description="test stock clip")
    return path


def _make_audio(path: str, seconds: float = 1.5):
    media.run_ffmpeg(["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                      "-c:a", "libmp3lame", path], description="test audio")
    return path


class TestMediaHelpers(unittest.TestCase):
    def test_ffmpeg_binary_resolves(self):
        self.assertTrue(os.path.exists(media.ffmpeg_bin()) or media.ffmpeg_bin())

    def test_font_is_discoverable(self):
        self.assertTrue(os.path.isfile(media.font_path()))

    def test_probe_duration_without_ffprobe(self):
        """Regression: pipeline crashed with FileNotFoundError when ffprobe was absent."""
        with tempfile.TemporaryDirectory() as tmp:
            audio = _make_audio(os.path.join(tmp, "a.mp3"), 2.0)
            with mock.patch.object(media, "ffprobe_bin", lambda: None):
                media.probe_duration.__wrapped__ if False else None
                duration = media.probe_duration(audio)
            self.assertAlmostEqual(duration, 2.0, delta=0.3)

    def test_caption_survives_hostile_text(self):
        """Regression: drawtext escaping mangled/failed on these characters."""
        hostile = ["It's 90% certain: \"stop\"", "A\\B:C'D", "x" * 400, "emoji free — dash"]
        with tempfile.TemporaryDirectory() as tmp:
            for i, text in enumerate(hostile):
                out = media.render_caption_png(text, (1080, 1920), os.path.join(tmp, f"c{i}.png"))
                self.assertTrue(out and os.path.getsize(out) > 0, f"failed on: {text[:30]}")

    def test_caption_empty_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(media.render_caption_png("   ", (640, 360),
                                                       os.path.join(tmp, "c.png")))


class TestScriptWriter(unittest.TestCase):
    def setUp(self):
        import script_writer
        self.sw = script_writer

    def test_parses_json_wrapped_in_prose(self):
        """Regression: LLM chatter around the JSON raised JSONDecodeError."""
        raw = 'Sure! Here is your script:\n{"title": "A", "scenes": []}\nHope it helps!'
        self.assertEqual(self.sw._parse_json_response(raw)["title"], "A")

    def test_parses_fenced_and_trailing_commas(self):
        cases = [
            '```json\n{"title":"A","scenes":[]}\n```',
            '```\n{"title":"A","scenes":[]}\n```',
            '{"title":"A","scenes":[],}',
        ]
        for raw in cases:
            self.assertEqual(self.sw._parse_json_response(raw)["title"], "A", raw)

    def test_nested_braces_preserved(self):
        raw = 'text {"title":"A","scenes":[{"narration":"x","visual_keyword":"y"}]} more'
        self.assertEqual(len(self.sw._parse_json_response(raw)["scenes"]), 1)

    def test_validation_rejects_missing_scenes(self):
        """Regression: a missing 'scenes' key used to KeyError mid-render."""
        with self.assertRaises(self.sw.ScriptGenerationError):
            self.sw._validate_and_normalise({"title": "A"}, "short", "n")

    def test_narration_strips_stage_directions(self):
        data = {"title": "T", "scenes": [
            {"narration": "[dramatic music] Narrator: The **truth** is out.",
             "visual_keyword": "night sky"}]}
        item = self.sw._validate_and_normalise(data, "short", "space")
        self.assertEqual(item["scenes"][0]["narration"], "The truth is out.")

    def test_short_is_trimmed_to_max_length(self):
        """Regression: over-length 'Shorts' get published as normal videos."""
        long_scene = {"narration": " ".join(["word"] * 60), "visual_keyword": "k"}
        data = {"title": "T", "scenes": [dict(long_scene) for _ in range(10)]}
        item = self.sw._validate_and_normalise(data, "short", "n")
        words = sum(len(s["narration"].split()) for s in item["scenes"])
        self.assertLessEqual(words / self.sw.WORDS_PER_SECOND, config.SHORT_MAX_SECONDS + 1)

    def test_missing_api_key_is_actionable(self):
        with mock.patch.object(config, "GROQ_API_KEY", ""):
            with self.assertRaises(self.sw.ScriptGenerationError) as ctx:
                self.sw._call_llm("hi")
            self.assertIn("GROQ_API_KEY", str(ctx.exception))


class TestUploaderLogic(unittest.TestCase):
    def setUp(self):
        import youtube_uploader
        self.up = youtube_uploader

    def test_tags_respect_character_budget(self):
        """Regression: tags[:500] sliced the list, not the 500-char API limit."""
        packed = self.up._pack_tags([f"a long tag number {i}" for i in range(200)])
        cost = sum(len(t) + (2 if " " in t else 0) + 1 for t in packed)
        self.assertLessEqual(cost, self.up.MAX_TAGS_CHARS)
        self.assertGreater(len(packed), 0)

    def test_slots_never_collide(self):
        """Regression: two runs in one window scheduled the same publish minute."""
        now = datetime(2026, 7, 25, 6, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            log_file = os.path.join(tmp, "upload_log.json")
            with mock.patch.object(config, "UPLOAD_LOG_FILE", log_file), \
                 mock.patch.object(config, "DATA_DIR", tmp):
                seen = set()
                for _ in range(6):
                    slot = self.up.next_available_slot(True, now=now)
                    self.assertNotIn(slot, seen)
                    self.assertGreater(slot, now)
                    seen.add(slot)
                    self.up._log_upload({"id": str(slot), "title": "t",
                                         "format": "short", "niche": "n"}, "vid", slot)

    def test_slots_distinct_without_any_shared_state(self):
        """Regression: when data/ can't be persisted (read-only GITHUB_TOKEN),
        every run saw an empty upload log and the 06:00 and 12:00 crons both
        scheduled at 13:00 -- two videos published at the same minute daily."""
        with mock.patch.object(self.up, "_scheduled_times", lambda: set()):
            slots = []
            for hour in self.up.RUN_TRIGGER_HOURS:
                now = datetime(2026, 7, 25, hour, 0, tzinfo=timezone.utc)
                slot = self.up.next_available_slot(True, now=now)
                self.assertGreater(slot, now, "publishAt must be in the future")
                slots.append(slot)
            self.assertEqual(len(set(slots)), len(slots),
                             f"cron runs collided with no shared state: {slots}")

    def test_publish_at_is_utc_rfc3339(self):
        naive = datetime(2026, 7, 25, 18, 0)
        body = self.up._build_body({"title": "T", "description": "d", "tags": ["a"],
                                    "format": "short", "niche": "space"}, naive)
        self.assertRegex(body["status"]["publishAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_shorts_get_hashtag(self):
        body = self.up._build_body({"title": "T", "description": "d", "tags": [],
                                    "format": "short", "niche": "n"}, datetime.now(timezone.utc))
        self.assertIn("#Shorts", body["snippet"]["description"])

    def test_missing_credentials_is_actionable(self):
        with mock.patch.object(config, "YT_CLIENT_ID", ""), \
             mock.patch.object(config, "YT_REFRESH_TOKEN", ""), \
             mock.patch.object(config, "YT_CLIENT_SECRET", ""):
            with self.assertRaises(self.up.UploadError) as ctx:
                self.up._get_client()
            self.assertIn("YOUTUBE_CLIENT_ID", str(ctx.exception))


class TestNicheResearch(unittest.TestCase):
    def test_pytrends_urllib3_shim(self):
        """Regression: pytrends passes method_whitelist, removed in urllib3 2.x."""
        import niche_research  # noqa: F401  (applies the shim on import)
        import pytrends.request as pr
        retry = pr.Retry(total=2, backoff_factor=0.1,
                         method_whitelist=frozenset(["GET", "POST"]))
        self.assertEqual(retry.allowed_methods, frozenset(["GET", "POST"]))

    def test_niche_stable_when_state_not_durable(self):
        """Regression: with no persisted niche file, every run re-ran full
        research (~1500 YouTube quota units each) and the niche could change
        between runs, destroying the channel focus the algorithm rewards."""
        import niche_research as nr
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"CI": "true", "GITHUB_ACTIONS": "true"},
                             clear=False), \
             mock.patch.object(config, "DATA_DIR", tmp), \
             mock.patch.object(config, "CURRENT_NICHE_FILE", os.path.join(tmp, "n.json")), \
             mock.patch.object(config, "UPLOAD_LOG_FILE", os.path.join(tmp, "u.json")), \
             mock.patch.object(nr, "score_niche",
                               side_effect=AssertionError("must not call the API")):
            os.environ.pop("CHANNEL_NICHE", None)
            os.environ.pop("STATE_IS_DURABLE", None)
            os.environ.pop("GITHUB_TOKEN_PERMISSIONS", None)
            self.assertFalse(nr._state_is_durable())
            picks = [nr.choose_best_niche()["niche"] for _ in range(3)]
        self.assertEqual(len(set(picks)), 1, f"niche drifted between runs: {picks}")
        self.assertIn(picks[0], config.CANDIDATE_NICHES)

    def test_pinned_niche_overrides_research(self):
        import niche_research as nr
        with mock.patch.dict(os.environ, {"CHANNEL_NICHE": "space and astronomy facts"}), \
             mock.patch.object(nr, "score_niche",
                               side_effect=AssertionError("must not call the API")):
            result = nr.choose_best_niche()
        self.assertEqual(result["niche"], "space and astronomy facts")
        self.assertTrue(result["pinned"])

    def test_state_durable_outside_ci(self):
        import niche_research as nr
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(nr._state_is_durable())

    def test_momentum_neutral_when_trends_unavailable(self):
        import niche_research as nr
        with mock.patch.object(nr, "TrendReq", None):
            self.assertEqual(nr._trend_momentum("anything"), nr.NEUTRAL_MOMENTUM)

    def test_scoring_is_finite_and_ranks(self):
        import niche_research as nr
        with mock.patch.object(nr, "_trend_momentum", lambda k: 1.5), \
             mock.patch.object(nr, "_youtube_competition_and_views", lambda k: (1000, 50000)):
            score = nr.score_niche("psychology facts")
        self.assertGreater(score["opportunity_score"], 0)
        self.assertTrue(float("inf") != score["opportunity_score"])

    def test_stale_and_corrupt_niche_files(self):
        import niche_research as nr
        self.assertFalse(nr._is_fresh(None))
        self.assertFalse(nr._is_fresh({"niche": "x"}))
        self.assertFalse(nr._is_fresh({"niche": "x", "chosen_at": "not-a-date"}))
        fresh = {"niche": "x", "chosen_at": datetime.now(timezone.utc).isoformat()}
        self.assertTrue(nr._is_fresh(fresh))
        # Naive timestamps from older runs must not raise.
        legacy = {"niche": "x", "chosen_at": datetime.utcnow().isoformat()}
        self.assertTrue(nr._is_fresh(legacy))
        old = {"niche": "x",
               "chosen_at": (datetime.now(timezone.utc) - timedelta(days=99)).isoformat()}
        self.assertFalse(nr._is_fresh(old))


class TestVideoAssembly(unittest.TestCase):
    """The real proof: render an actual MP4 with ffmpeg, no network."""

    def test_assembles_playable_video_from_mixed_fps_sources(self):
        import video_assembler as va
        import thumbnail_gen as tg

        with tempfile.TemporaryDirectory() as tmp:
            durations = [1.5, 1.0, 2.0]
            scenes = []
            for i, dur in enumerate(durations):
                audio = _make_audio(os.path.join(tmp, f"a{i}.mp3"), dur)
                scenes.append({
                    "narration": f"Scene {i}: it's 90% certain \"this\" works.",
                    "visual_keyword": "test",
                    "audio_path": audio,
                    "duration_seconds": media.probe_duration(audio),
                })

            item = {"id": "test-item", "format": "short", "niche": "science facts",
                    "title": "An Extremely Long Test Title That Must Wrap Correctly Onto "
                             "Several Lines Without Overflowing", "scenes": scenes}

            # Stock clips at 25/30/60 fps -- the mix that broke concat -c copy.
            fps_cycle = [25, 30, 60]
            calls = {"n": 0}

            def fake_fetch(keyword, orientation, out_path):
                fps = fps_cycle[calls["n"] % len(fps_cycle)]
                calls["n"] += 1
                _make_stock_clip(out_path, seconds=1.0, fps=fps)  # shorter than audio: must loop
                return True

            with mock.patch.object(va, "_fetch_clip_for_scene", fake_fetch):
                out = va.assemble_video(item, os.path.join(tmp, "work"))

            self.assertTrue(os.path.exists(out))
            self.assertGreater(os.path.getsize(out), 10000)

            expected = sum(s["duration_seconds"] for s in scenes)
            actual = media.probe_duration(out)
            self.assertAlmostEqual(actual, expected, delta=0.75,
                                   msg="concat drifted -- fps normalisation regressed")

            # Must decode cleanly all the way through.
            proc = subprocess.run([media.ffmpeg_bin(), "-hide_banner", "-v", "error",
                                   "-i", out, "-f", "null", "-"],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr[:500])

            # Thumbnail from that real video.
            thumb = tg.generate_thumbnail(out, item["title"],
                                          os.path.join(tmp, "thumb.jpg"), is_short=True)
            self.assertTrue(os.path.exists(thumb))
            self.assertLessEqual(os.path.getsize(thumb), tg.MAX_THUMBNAIL_BYTES)
            from PIL import Image
            self.assertEqual(Image.open(thumb).size, tuple(config.SHORT_RESOLUTION))

    def test_falls_back_when_no_stock_footage(self):
        import video_assembler as va
        with tempfile.TemporaryDirectory() as tmp:
            audio = _make_audio(os.path.join(tmp, "a.mp3"), 1.0)
            item = {"id": "fb", "format": "longform", "niche": "n", "title": "T",
                    "scenes": [{"narration": "No footage exists for this.",
                                "visual_keyword": "zzz",
                                "audio_path": audio,
                                "duration_seconds": media.probe_duration(audio)}]}
            with mock.patch.object(va, "_fetch_clip_for_scene", lambda *a, **k: False):
                out = va.assemble_video(item, os.path.join(tmp, "work"))
            self.assertTrue(os.path.exists(out) and os.path.getsize(out) > 5000)

    def test_short_video_thumbnail_extraction(self):
        """Regression: fixed t=2.0s extraction failed on clips under 2 seconds."""
        import thumbnail_gen as tg
        with tempfile.TemporaryDirectory() as tmp:
            clip = _make_stock_clip(os.path.join(tmp, "tiny.mp4"), seconds=0.8, fps=30)
            thumb = tg.generate_thumbnail(clip, "Tiny", os.path.join(tmp, "t.jpg"),
                                          is_short=False)
            self.assertTrue(os.path.exists(thumb))


class TestTTS(unittest.TestCase):
    def test_silent_fallback_when_tts_unavailable(self):
        """Regression: an edge-tts outage aborted the whole run."""
        import tts_voiceover as tts
        with tempfile.TemporaryDirectory() as tmp:
            item = {"id": "x", "scenes": [
                {"narration": "This is a sentence with about eight words here."},
                {"narration": "   "},  # must be skipped, not rendered as 0 bytes
            ]}
            with mock.patch.object(tts, "_synthesize_with_retry", lambda *a, **k: False):
                out = tts.generate_voiceover(item, tmp)
            self.assertEqual(len(out["scenes"]), 1)
            self.assertGreater(out["scenes"][0]["duration_seconds"], 0)
            self.assertTrue(os.path.exists(out["scenes"][0]["audio_path"]))

    def test_raises_when_nothing_narratable(self):
        import tts_voiceover as tts
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                tts.generate_voiceover({"id": "x", "scenes": [{"narration": ""}]}, tmp)


class TestOrchestrator(unittest.TestCase):
    def test_queue_survives_production_failure(self):
        """Regression: the item was popped before production, so a crash lost it."""
        import orchestrator
        with tempfile.TemporaryDirectory() as tmp:
            queue_file = os.path.join(tmp, "queue.json")
            item = {"id": "keep-me", "format": "short", "niche": "n", "title": "T",
                    "scenes": [{"narration": "hi", "visual_keyword": "k"}]}
            with open(queue_file, "w") as f:
                json.dump([item, dict(item, id="second")], f)

            with mock.patch.object(config, "CONTENT_QUEUE_FILE", queue_file), \
                 mock.patch.object(config, "DATA_DIR", tmp), \
                 mock.patch.object(orchestrator, "FAILED_DIR", os.path.join(tmp, "failed")), \
                 mock.patch.object(orchestrator, "choose_best_niche",
                                   lambda force=False: {"niche": "n"}), \
                 mock.patch.object(orchestrator, "generate_voiceover",
                                   side_effect=RuntimeError("boom")):
                rc = orchestrator.run_once()

            self.assertEqual(rc, 1, "a failed run must exit non-zero")
            with open(queue_file) as f:
                remaining = json.load(f)
            self.assertEqual([q["id"] for q in remaining], ["second"])
            self.assertTrue(os.path.exists(os.path.join(tmp, "failed", "keep-me.json")))

    def test_dry_run_produces_video_without_uploading(self):
        """Full pipeline end-to-end, upload boundary stubbed."""
        import orchestrator
        import tts_voiceover as tts
        import video_assembler as va

        with tempfile.TemporaryDirectory() as tmp:
            queue_file = os.path.join(tmp, "queue.json")
            item = {"id": "e2e", "format": "short", "niche": "space facts",
                    "title": "The Moon Is Drifting Away", "tags": ["space"],
                    "description": "d",
                    "scenes": [{"narration": "The moon drifts away each year.",
                                "visual_keyword": "moon"},
                               {"narration": "It's 3.8 centimetres: every year.",
                                "visual_keyword": "night sky"}]}
            with open(queue_file, "w") as f:
                json.dump([item], f)

            uploaded = {"called": False}

            def fake_upload(*a, **k):
                uploaded["called"] = True
                return "vid"

            def fake_tts(content_item, work_dir):
                os.makedirs(work_dir, exist_ok=True)
                for i, sc in enumerate(content_item["scenes"]):
                    p = _make_audio(os.path.join(work_dir, f"s{i}.mp3"), 1.2)
                    sc["audio_path"] = p
                    sc["duration_seconds"] = media.probe_duration(p)
                return content_item

            with mock.patch.object(config, "CONTENT_QUEUE_FILE", queue_file), \
                 mock.patch.object(config, "DATA_DIR", tmp), \
                 mock.patch.object(orchestrator, "choose_best_niche",
                                   lambda force=False: {"niche": "space facts"}), \
                 mock.patch.object(orchestrator, "generate_voiceover", fake_tts), \
                 mock.patch.object(orchestrator, "upload_video", fake_upload), \
                 mock.patch.object(va, "_fetch_clip_for_scene",
                                   lambda k, o, p: bool(_make_stock_clip(p, 1.0, 30))):
                rc = orchestrator.run_once(dry_run=True)

            self.assertEqual(rc, 0)
            self.assertFalse(uploaded["called"], "dry run must not upload")
            produced = os.path.join(tmp, "dry_run", "e2e", "video", "e2e.mp4")
            self.assertTrue(os.path.exists(produced), "dry run kept no video")
            self.assertGreater(os.path.getsize(produced), 10000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
