"""RF1801-5: reference_problems builds the current-decision index at most once per call, and only for citations."""
import unittest
from unittest import mock

from campaign_tool import meeting_elsewhere as me


def entry(i, state="adopted", url="https://example.org/a"):
    return {"place": f"Town {i}", "body": "Council", "decided_on": "2026-01-01", "kind": "ban",
            "state": state, "as_stated": f"as stated {i}", "source_url": url}


def run(meetings, elsewhere, skip=None):
    calls = []
    real = me.identity

    def spy(e):
        calls.append(1)
        return real(e)

    with mock.patch.object(me, "identity", spy):
        out = me.reference_problems(meetings, elsewhere, skip)
    return out, len(calls)


class IndexBounds(unittest.TestCase):
    def test_index_built_once_for_many_citing_meetings(self):
        current = [entry(i) for i in range(100)]
        meetings = [(f"m{k}.json", {"id": f"m{k}", "cited_elsewhere": [dict(current[k % 100])]}) for k in range(1000)]
        out, n = run(meetings, current)
        self.assertEqual(out, [])
        self.assertEqual(n, 100 + 1000)

    def test_uncited_and_all_skipped_meetings_do_no_source_work(self):
        current = [entry(i) for i in range(100)]
        uncited = [(f"u{k}.json", {"id": f"u{k}"}) for k in range(50)] + [("e.json", {"id": "e", "cited_elsewhere": []})]
        self.assertEqual(run(uncited, current), ([], 0))
        skipped = [("s.json", {"id": "s", "cited_elsewhere": [entry(500)]})]
        self.assertEqual(run(skipped, current, "s"), ([], 0))
        mixed = uncited + [("c.json", {"id": "c", "cited_elsewhere": [dict(current[3])]})] + uncited
        self.assertEqual(run(mixed, current), ([], 101))

    def test_none_and_empty_sources_keep_literal_errors_and_build_once(self):
        snaps = [entry(1), entry(2)]
        meetings = [("m.json", {"id": "m", "cited_elsewhere": snaps}),
                    ("n.json", {"id": "n", "cited_elsewhere": [entry(3)]})]
        out, n = run(meetings, None)
        self.assertEqual(n, 3)
        self.assertEqual(out[0], "m.json.cited_elsewhere[0]: content/elsewhere.json not found; no entry for "
                                 "Town 1, Council, 2026-01-01 (ban); restage with meetings-stage --replace")
        self.assertEqual(len(out), 3)
        out, n = run(meetings, [])
        self.assertEqual(n, 3)
        self.assertEqual(out[1], "m.json.cited_elsewhere[1]: content/elsewhere.json has no entry for "
                                 "Town 2, Council, 2026-01-01 (ban); restage with meetings-stage --replace")

    def test_changed_facts_and_order_and_replace_skip_preserved(self):
        current = [entry(2, state="repealed", url="https://example.org/b"), entry(1)]
        snap1 = entry(1)
        snap2 = entry(2)
        meetings = [("a.json", {"id": "a", "cited_elsewhere": [snap1, snap2]}),
                    ("b.json", {"id": "b", "cited_elsewhere": [entry(9)]})]
        out, _ = run(meetings, current)
        self.assertEqual(out, [
            "a.json.cited_elsewhere[1]: Town 2, Council, 2026-01-01 (ban) changed in content/elsewhere.json "
            "(state, source_url); restage with meetings-stage --replace",
            "b.json.cited_elsewhere[0]: content/elsewhere.json has no entry for Town 9, Council, 2026-01-01 (ban); "
            "restage with meetings-stage --replace"])
        out, _ = run(meetings, current, "a")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("b.json"))
        self.assertEqual(snap2, entry(2))  # inputs untouched

    def test_one_shot_iterator_and_fresh_between_calls(self):
        current = [entry(1)]
        gen = iter([("a.json", {"id": "a", "cited_elsewhere": [entry(1)]}),
                    ("b.json", {"id": "b", "cited_elsewhere": [entry(1)]})])
        self.assertEqual(run(gen, current), ([], 3))
        meetings = [("a.json", {"id": "a", "cited_elsewhere": [entry(1)]})]
        self.assertEqual(run(meetings, current)[0], [])
        self.assertEqual(len(run(meetings, [entry(2)])[0]), 1)  # no cache from the earlier call
        self.assertEqual(run(meetings, current)[0], [])


if __name__ == "__main__":
    unittest.main()
