"""RF1240-14: flock_page check --json prints one flock-page-check-v1 document from the same check; text and exit unchanged."""
import json, unittest

import tests.flock.test_page_check as base  # module import only: the base TestCase is not collected again here

KEYS, ROOT, run = base.KEYS, base.ROOT, base.run
DOC_KEYS = ["counts", "differing_keys", "errors", "review_required", "schema", "state", "superseded"]
COUNTS = dict(cards=3, admissions=2, sources=3, records_messages=2, dropped_outreach=1)
ZERO = dict.fromkeys(COUNTS, 0)


def text_of(doc):  # the text form a document stands for: the error lines, then the summary line
    return doc["errors"] + [" ".join("%s=%d" % (k, doc["counts"][k]) for k in COUNTS)
                            + " errors=%d superseded=%d state=%s" % (len(doc["errors"]), doc["superseded"], doc["state"])]


class FlockPageCheckJsonTest(unittest.TestCase):
    setUp, save, flags, build, snapshot = (base.FlockPageCheckTest.setUp, base.FlockPageCheckTest.save, base.FlockPageCheckTest.flags,
                                           base.FlockPageCheckTest.build, base.FlockPageCheckTest.snapshot)

    def both(self, story=None):  # -> (exit, text lines, document); text and --json agree, and neither touches the disk
        story = story or self.story
        before = self.snapshot()
        code, out = run("check", *self.flags(()), "--story", story)
        jcode, raw = run("check", *self.flags(()), "--story", story, "--json")
        self.assertEqual(self.snapshot(), before, "check wrote, touched or created something")
        doc = json.loads(raw)
        self.assertEqual(sorted(doc), DOC_KEYS)
        self.assertEqual((doc["schema"], doc["review_required"]), ("flock-page-check-v1", True))
        self.assertEqual(raw, json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False) + "\n")
        self.assertEqual(jcode, code)
        self.assertEqual(out.splitlines(), text_of(doc))
        if doc["state"] != "stale":
            self.assertEqual(doc["differing_keys"], [])
        for s in base.SENTINELS + (str(self.tmp),):  # never card, source or thread text, never a path
            self.assertNotIn(s, raw)
            self.assertNotIn(s, out)
        return code, out.splitlines(), doc

    def test_build_then_check_is_current(self):  # 1
        self.assertEqual(self.build(), 0)
        code, lines, doc = self.both()
        self.assertEqual((code, lines), (0, [base.tail("current")]))
        self.assertEqual((doc["state"], doc["differing_keys"], doc["errors"], doc["counts"], doc["superseded"]),
                         ("current", [], [], COUNTS, 0))

    def test_edited_headline_and_milestones_are_stale_in_keys_order(self):  # 2
        self.assertEqual(self.build(), 0)
        self.data["cards"]["milestones"][0]["label"] = "Council votes on the camera program"  # listed first, KEYS order wins
        self.data["cards"]["meta"]["headline"] = "Sample City cameras: a changed headline"
        self.save()
        code, lines, doc = self.both()
        msg = "story 0: differs from a fresh build (keys: headline, milestones)"
        self.assertEqual((code, lines), (1, [msg, base.tail("stale", 1)]))
        self.assertEqual((doc["state"], doc["differing_keys"], doc["errors"], doc["counts"]),
                         ("stale", ["headline", "milestones"], [msg], COUNTS))

    def test_same_values_or_wrong_top_level_lists_the_differing_keys(self):  # 2 (bypass by formatting / shape)
        self.assertEqual(self.build(), 0)
        story = json.loads(self.story.read_text(encoding="utf-8"))
        self.story.write_text(json.dumps(story), encoding="utf-8")
        code, lines, doc = self.both()
        self.assertEqual((code, doc["state"], doc["differing_keys"]), (1, "stale", []))
        self.assertEqual(doc["errors"], ["story 0: differs from a fresh build (keys: none)"])
        self.story.write_text(json.dumps([story]), encoding="utf-8")
        self.assertEqual(self.both()[2]["differing_keys"], KEYS)

    def test_missing_link_and_unreadable_story(self):  # 3
        code, lines, doc = self.both()
        self.assertEqual((code, doc["state"], doc["errors"], doc["counts"]), (1, "missing", [], COUNTS))
        self.assertEqual(list(self.outdir.iterdir()), [])
        real = self.tmp / "real-story.json"
        self.assertEqual(self.build(out=real), 0)
        for target in (real, self.tmp / "nowhere.json"):  # a link, live or dangling, is never followed
            with self.subTest(target=target.name):
                self.story.symlink_to(target)
                code, lines, doc = self.both()
                self.assertEqual((code, doc["state"], doc["errors"], doc["counts"]), (1, "link", [], ZERO))
                self.story.unlink()
        self.assertEqual(self.build(), 0)
        for bad in (b'{"title": "caf\xe9"}', b"{not json", b""):
            with self.subTest(bad=bad[:12]):
                self.story.write_bytes(bad)
                code, lines, doc = self.both()
                self.assertEqual((code, doc["state"], doc["differing_keys"], doc["errors"]),
                                 (1, "stale", [], ["story 0: unreadable JSON"]))
        self.story.unlink()
        self.story.mkdir()
        self.assertEqual(self.both()[2]["errors"], ["io 0: IsADirectoryError"])
        self.assertEqual(self.both()[2]["state"], "invalid")

    def test_story_inside_the_repository_is_refused(self):  # 4
        story = ROOT / "tests" / "fixtures" / "sample-story.json"
        code, lines, doc = self.both(story)
        self.assertEqual((code, doc["state"], doc["errors"], doc["counts"], doc["differing_keys"]),
                         (1, "invalid", ["out 0: refused inside the repository"], ZERO, []))
        self.assertFalse(story.exists())

    def test_bad_cards_input_prints_the_text_error_line(self):  # 5
        self.assertEqual(self.build(), 0)
        for raw, want in ((b"[]", ["cards 0: not an object"]), (b"{not json", ["cards 0: not JSON"]),
                          (b'{"schema": 2, "slug": "x"}', ["cards 0: schema must be 1 with a lowercase slug"])):
            with self.subTest(raw=raw):
                (self.fix / "cards.json").write_bytes(raw)
                code, lines, doc = self.both()
                self.assertEqual((code, doc["state"], doc["errors"], doc["counts"]), (1, "invalid", want, ZERO))
                self.assertEqual(lines[:-1], want)

    def test_superseded_counts_regular_files_only(self):  # 6
        self.assertEqual(self.build(), 0)
        for name in ("sample-story.json.superseded-20260101T000000000000Z", "sample-story.json.superseded-20260102T000000000000Z"):
            (self.outdir / name).write_text("{}", encoding="utf-8")
        (self.outdir / "sample-story.json.superseded-fake").symlink_to(self.story)
        (self.outdir / "other-story.json.superseded-1").write_text("{}", encoding="utf-8")
        code, lines, doc = self.both()
        self.assertEqual((code, doc["state"], doc["superseded"]), (0, "current", 2))
        self.assertEqual(lines, [base.tail("current", superseded=2)])


if __name__ == "__main__":
    unittest.main()
