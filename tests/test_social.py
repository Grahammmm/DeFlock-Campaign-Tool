"""Tests for the daily Reel pipeline, using the fictional Cedar County campaign only."""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / "examples" / "fictional-campaign" / "social"

try:
    import numpy  # noqa: F401
    import PIL  # noqa: F401
    HAVE_MEDIA = shutil.which("ffmpeg") is not None
except ImportError:
    HAVE_MEDIA = False

from campaign_tool.social import script as script_module
from campaign_tool.social.facts import Fact, load_facts, quote_is_approved, validate_fact
from campaign_tool.social.posting import posting_notes
from campaign_tool.social.script import check_script, llm_script, plan, template_script
from campaign_tool.social.timeline import voice_summary


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
        from campaign_tool.social.timeline import timed_words
        words = timed_words("Every source is at example.org.", 0.1, 2.0)
        self.assertEqual([w for w, _ in words], ["Every", "source", "is", "at", "example.org."])
        self.assertAlmostEqual(words[0][1], 0.1)
        self.assertLess(words[-1][1], 2.1)
        self.assertEqual(sorted(t for _, t in words), [t for _, t in words])

    def test_scene_choice_uses_the_fact_frame(self):
        from campaign_tool.social.timeline import beat_scene
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


class PlanningWithoutMediaTest(unittest.TestCase):
    """Planning, scripting and checklist code must import without NumPy or Pillow installed."""

    def test_planning_modules_import_without_numpy_or_pillow(self):
        probe = (
            "import sys\n"
            "class Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in ('numpy', 'PIL'):\n"
            "            raise ImportError('blocked for test: ' + name)\n"
            "        return None\n"
            "sys.meta_path.insert(0, Block())\n"
            "import campaign_tool.social.facts, campaign_tool.social.script, campaign_tool.social.timeline\n"
            "import campaign_tool.social.posting, campaign_tool.social.shots\n"
            "for heavy in ('numpy', 'PIL'):\n"
            "    assert heavy not in sys.modules, heavy\n"
            "print('planning-ok')\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.py"
            path.write_text(probe)
            done = subprocess.run([sys.executable, str(path)], cwd=ROOT, capture_output=True, text=True,
                                  env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("planning-ok", done.stdout)


class VoiceFallbackTest(unittest.TestCase):
    """A test-voice fallback on any beat must reach the metadata and the posting checklist."""

    def notes(self, voices):
        fact = load_facts(CAMPAIGN / "facts.json")[0]
        script = template_script(fact, "stat-drop", cfg(), dt.date(2026, 10, 1))
        script.update(clip={"model": "none"}, render={"voice": voice_summary(voices), "voices": voices,
                                                      "scenes": []})
        return posting_notes(script, fact, False, cfg())

    def test_later_fallback_is_reported(self):
        self.assertEqual(voice_summary(["gemini", "gemini", "espeak", "gemini"]), "espeak")
        self.assertEqual(voice_summary(["elevenlabs", "silence", "espeak"]), "silence")
        self.assertEqual(voice_summary(["gemini", "gemini"]), "gemini")
        self.assertIsNone(voice_summary([]))

    def test_mixed_sequence_checklist_warns_and_labels(self):
        notes = self.notes(["gemini", "gemini", "silence", "gemini"])
        self.assertIn("Don't post this one", notes)
        self.assertIn("silence", notes)
        self.assertIn("beat 3 of 4", notes)
        self.assertIn("the narration is an AI voice", notes)

    def test_all_ai_voice_has_no_warning(self):
        notes = self.notes(["elevenlabs", "elevenlabs"])
        self.assertNotIn("Don't post", notes)
        self.assertIn("Add AI label", notes)

    def test_old_metadata_without_per_beat_list(self):
        fact = load_facts(CAMPAIGN / "facts.json")[0]
        script = template_script(fact, "stat-drop", cfg(), dt.date(2026, 10, 1))
        script.update(clip={"model": "none"}, render={"voice": "espeak", "scenes": []})
        self.assertIn("Don't post this one", posting_notes(script, fact, False, cfg()))


class QuoteBindingTest(unittest.TestCase):
    """Quote cards say "FROM THE RECORDS": their words must be an approved source quotation."""

    def setUp(self):
        self.fact = next(f for f in load_facts(CAMPAIGN / "facts.json") if f.id == "cc-audits")
        self.day = dt.date(2026, 10, 1)

    def script_with_quote(self, text):
        script = template_script(self.fact, "stat-drop", cfg(), self.day)
        for beat in script["beats"]:
            if beat["type"] == "quote":
                beat["text"] = text
        return script

    def test_approved_quote_passes_with_typographic_variants(self):
        self.assertEqual(check_script(self.script_with_quote("No completed audit reports produced."),
                                      self.fact, cfg()), [])
        self.assertTrue(quote_is_approved("\u201cNo completed  audit reports produced.\u201d", self.fact))

    def test_fabricated_nonnumeric_quote_is_rejected(self):
        problems = check_script(self.script_with_quote("The county admits nobody checks the cameras."),
                                self.fact, cfg())
        self.assertTrue(any("approved source quotation" in p for p in problems), problems)

    def test_excerpt_of_approved_quote_is_rejected(self):
        problems = check_script(self.script_with_quote("audit reports produced."), self.fact, cfg())
        self.assertTrue(any("approved source quotation" in p for p in problems), problems)

    def test_fact_quote_beat_must_be_listed(self):
        fact = Fact(id="x", agency="A", claim="The county produced no audits.", source_url="https://example.org",
                    source_label="s", frame="audits", numbers=[], hooks=["Hook"],
                    beats=[{"type": "quote", "text": "Nobody audits anything."}], why="Why", visual="v")
        self.assertTrue(any("approved source quotes" in e for e in validate_fact(fact)))
        fact.quotes = ["Nobody audits anything."]
        self.assertEqual(validate_fact(fact), [])

    def test_generated_quote_beat_falls_back_to_template(self):
        reply = {"hook": "The policy promises audits.",
                 "beats": [{"type": "quote", "text": "Audits were never a priority for us."}],
                 "why": "A safeguard nobody can see isn't one residents can trust."}
        body = json.dumps({"content": [{"text": json.dumps(reply)}]}).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        with mock.patch.object(script_module.urllib.request, "urlopen", return_value=response), \
                mock.patch("builtins.print"):
            self.assertIsNone(llm_script(self.fact, "stat-drop", cfg(), self.day, "", api_key="test-key"))
        reply["beats"] = [{"type": "quote", "text": "No completed audit reports produced."}]
        body = json.dumps({"content": [{"text": json.dumps(reply)}]}).encode()
        response.__enter__.return_value.read.return_value = body
        with mock.patch.object(script_module.urllib.request, "urlopen", return_value=response), \
                mock.patch("builtins.print"):
            accepted = llm_script(self.fact, "stat-drop", cfg(), self.day, "", api_key="test-key")
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted["writer"], "claude")


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

    def test_later_beat_fallback_reaches_render_metadata(self):
        from campaign_tool.social import reel as reel_module
        from campaign_tool.social.render import Brand
        c = cfg()
        fact = load_facts(CAMPAIGN / "facts.json")[0]
        script = {"beats": [{"type": "hook", "text": "A short hook"}, {"type": "line", "text": "A short line"},
                            {"type": "cta", "text": c["cta"]["text"], "small": c["cta"]["small"]}]}
        providers = iter(["gemini", "silence", "gemini"])
        real_narrate = reel_module.narrate

        def fake_narrate(text, out, voice_cfg, offline=False, log=print):
            seconds, _ = real_narrate(text, out, {}, offline=True, log=log)
            return seconds, next(providers)

        old = os.environ.pop("SOCIAL_BLENDER_PYTHON", None)
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(reel_module, "narrate", fake_narrate):
                stats = reel_module.Reel(Brand(c["brand"]), c, None, Path(tmp) / "shots", log=lambda *_: None).build(
                    script, fact, Path(tmp) / "reel.mp4", offline_voice=False)
        finally:
            if old:
                os.environ["SOCIAL_BLENDER_PYTHON"] = old
        self.assertEqual(stats["voices"], ["gemini", "silence", "gemini"])
        self.assertEqual(stats["voice"], "silence")


if __name__ == "__main__":
    unittest.main()
