"""Tests for the daily Reel pipeline, using the fictional Cedar County campaign only."""
import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / "examples" / "fictional-campaign" / "social"

try:
    import numpy  # noqa: F401
    import PIL  # noqa: F401
    HAVE_MEDIA = shutil.which("ffmpeg") is not None
except ImportError:
    HAVE_MEDIA = False

from campaign_tool.social.facts import Fact, load_facts, validate_fact
from campaign_tool.social.script import check_script, llm_script, plan, template_script


def cfg():
    return json.loads((CAMPAIGN / "social.json").read_text())


class FactsTest(unittest.TestCase):
    def test_example_facts_load(self):
        self.assertEqual(len(load_facts(CAMPAIGN / "facts.json")), 2)

    def test_number_not_in_claim_is_rejected(self):
        fact = Fact(id="x", agency="A", claim="It keeps data for 30 days.", source_url="https://example.org",
                    source_label="s", frame="retention", numbers=["30", "90"], hooks=["Hook"],
                    beats=[{"type": "stat", "big": "30", "small": "days"}], why="Why", visual="v")
        self.assertTrue(any("90" in e for e in validate_fact(fact)))

    def test_hook_with_unlisted_number_is_rejected(self):
        fact = Fact(id="x", agency="A", claim="It keeps data for 30 days.", source_url="https://example.org",
                    source_label="s", frame="retention", numbers=["30"], hooks=["Kept 45 days"],
                    beats=[{"type": "stat", "big": "30", "small": "days"}], why="Why", visual="v")
        self.assertTrue(any("45" in e for e in validate_fact(fact)))


class ScriptTest(unittest.TestCase):
    def setUp(self):
        self.facts = load_facts(CAMPAIGN / "facts.json")
        self.day = dt.date(2026, 10, 1)

    def test_every_template_script_passes(self):
        for fact in self.facts:
            for fmt in ("stat-drop", "map-reveal", "records-receipt"):
                script = template_script(fact, fmt, cfg(), self.day)
                self.assertEqual(check_script(script, fact, cfg()), [], (fact.id, fmt))

    def test_added_number_and_banned_term_are_caught(self):
        fact = self.facts[0]
        script = template_script(fact, "stat-drop", cfg(), self.day)
        script["beats"].insert(1, {"type": "line", "text": "Kept for 9999 days, which is illegal"})
        problems = " ".join(check_script(script, fact, cfg()))
        self.assertIn("9999", problems)
        self.assertIn("illegal", problems)

    def test_plan_is_deterministic_and_avoids_yesterday(self):
        first = plan(self.facts, self.day, [])
        self.assertEqual(first, plan(self.facts, self.day, []))
        history = [{"date": "2026-09-30", "fact_id": first[0].id, "format": first[1]}]
        second = plan(self.facts, self.day, history)
        self.assertNotEqual(second[0].id, first[0].id)

    def test_llm_writer_is_optional(self):
        old = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            self.assertIsNone(llm_script(self.facts[0], "stat-drop", cfg(), self.day, ""))
        finally:
            if old:
                os.environ["ANTHROPIC_API_KEY"] = old


@unittest.skipUnless(HAVE_MEDIA, "needs numpy, Pillow and ffmpeg")
class RenderTest(unittest.TestCase):
    def test_short_reel_renders(self):
        from campaign_tool.social.render import Brand, Renderer, load_map
        from campaign_tool.social.veo import make_placeholder
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            make_placeholder(clip, seconds=2)
            c = cfg()
            renderer = Renderer(Brand(c["brand"]), load_map(CAMPAIGN / "boundary.geojson", CAMPAIGN / "points.geojson"))
            script = {"beats": [{"type": "map", "big": "24", "small": c["map"]["caption"]},
                                {"type": "cta", "text": c["cta"]["text"], "small": c["cta"]["small"]}]}
            out = Path(tmp) / "reel.mp4"
            stats = renderer.render(script, clip, out, ai_background=True)
            probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                    "stream=width,height", "-of", "csv=p=0", str(out)],
                                   capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(probe, "1080,1920")
            self.assertGreater(stats["frames"], 200)


class ReelV2PlanningTest(unittest.TestCase):
    """Narration, pronunciation and scene choice for the v2 (narrated, 3D) Reel."""

    def test_spoken_line_numbers_are_checked(self):
        fact = load_facts(CAMPAIGN / "facts.json")[0]
        script = template_script(fact, "stat-drop", cfg(), dt.date(2026, 10, 1))
        script["beats"][1]["say"] = "They keep it for 4321 days."
        self.assertIn("4321", " ".join(check_script(script, fact, cfg())))

    def test_timed_words_cover_the_spoken_duration(self):
        from campaign_tool.social.reel import timed_words
        words = timed_words("Every source is at example.org.", 0.1, 2.0)
        self.assertEqual([w for w, _ in words], ["Every", "source", "is", "at", "example.org."])
        self.assertAlmostEqual(words[0][1], 0.1)
        self.assertLess(words[-1][1], 2.1)
        self.assertEqual(sorted(t for _, t in words), [t for _, t in words])

    def test_scene_choice_uses_the_fact_frame(self):
        from campaign_tool.social.reel import beat_scene
        fact = load_facts(CAMPAIGN / "facts.json")[0]
        self.assertIn(beat_scene({"type": "hook", "text": "x"}, fact, 0, 3), ("camera_hero", "plate_scan"))
        self.assertEqual(beat_scene({"type": "map", "big": "24", "small": "x"}, fact, 1, 3), "county_pins")
        fact.frame = "retention"
        self.assertEqual(beat_scene({"type": "stat", "big": "365", "small": "days kept"}, fact, 1, 3), "retention_blocks")
        self.assertNotEqual(beat_scene({"type": "stat", "big": "260×", "small": "longer than"}, fact, 1, 3),
                            "retention_blocks")
        self.assertEqual(beat_scene({"type": "stat", "big": "9", "small": "x", "scene": "plate_scan"}, fact, 1, 3),
                         "plate_scan")

    def test_shot_key_is_stable_and_data_dependent(self):
        from campaign_tool.social.shots import shot_key, shot_params
        self.assertEqual(shot_key("retention_blocks", shot_params("retention_blocks", number=90)),
                         shot_key("retention_blocks", shot_params("retention_blocks", number=90)))
        self.assertNotEqual(shot_key("retention_blocks", shot_params("retention_blocks", number=90)),
                            shot_key("retention_blocks", shot_params("retention_blocks", number=30)))

    def test_missing_blender_skips_shot(self):
        from campaign_tool.social.shots import ensure_shot
        old = os.environ.pop("SOCIAL_BLENDER_PYTHON", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                self.assertIsNone(ensure_shot("camera_hero", {"seconds": 4.0}, tmp, log=lambda *_: None))
        finally:
            if old:
                os.environ["SOCIAL_BLENDER_PYTHON"] = old


@unittest.skipUnless(HAVE_MEDIA, "needs numpy, Pillow and ffmpeg")
class ReelV2RenderTest(unittest.TestCase):
    def test_narrated_reel_renders_without_blender_or_voice(self):
        from campaign_tool.social.reel import Reel
        from campaign_tool.social.render import Brand
        from campaign_tool.social import narration
        c = cfg()
        fact = load_facts(CAMPAIGN / "facts.json")[0]
        script = {"beats": [{"type": "hook", "text": "A short hook"},
                            {"type": "cta", "text": c["cta"]["text"], "small": c["cta"]["small"]}]}
        old = os.environ.pop("SOCIAL_BLENDER_PYTHON", None)
        real_which = narration.shutil.which
        narration.shutil.which = lambda name: None if name == "espeak-ng" else real_which(name)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "reel.mp4"
                stats = Reel(Brand(c["brand"]), c, None, Path(tmp) / "shots", log=lambda *_: None).build(
                    script, fact, out, offline_voice=True)
                self.assertEqual(stats["voice"], "silence")
                probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                                        "-of", "csv=p=0", str(out)], capture_output=True, text=True, check=True).stdout
                self.assertIn("video,1080,1920", probe)
                self.assertIn("audio", probe)
        finally:
            narration.shutil.which = real_which
            if old:
                os.environ["SOCIAL_BLENDER_PYTHON"] = old


if __name__ == "__main__":
    unittest.main()
