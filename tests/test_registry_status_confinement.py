"""RF0300-5: `campaigns status` confines each campaign before its campaign.json is read.

Synthetic campaigns in a temporary folder only. Run: python3 -B -m unittest tests.test_registry_status_confinement -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import registry
from campaign_tool.cli import main

TAIL = " | private; review-required"
WATCH, SEEN, NOW = [], [], {"campaign": None}  # a path under WATCH reached past the guard while another campaign is read
registry.confine("", ())  # the guard first: the recorder below only sees what the guard let through


def _record(event, args):
    if WATCH and NOW["campaign"] and event in registry._PATH_EVENTS:
        for arg in args[:registry._PATH_EVENTS[event]]:
            if isinstance(arg, (str, bytes, os.PathLike)):
                target = Path(os.path.realpath(os.path.abspath(os.fsdecode(arg))))
                SEEN.extend((event, str(target)) for w in WATCH
                            if registry._within(target, w) and not registry._within(target, NOW["campaign"]))


sys.addaudithook(_record)

HEALTHY = "requests=0 open=0 waiting-send=0 waiting-publish=0 law=draft"


class StatusConfinement(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-status-confine-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        patch = mock.patch.dict(os.environ, {registry.ENV: str(self.base / "config" / "campaigns.json")})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.release)
        self.addCleanup(WATCH.clear)
        SEEN.clear()
        NOW["campaign"] = None

    def campaign(self, name, folder=None):
        root = self.base / (folder or name)
        root.mkdir()
        (root / "campaign.json").write_text(json.dumps(
            {"schema_version": 1, "name": name, "county": "Synthetic", "state": "CA"}), encoding="utf-8")
        registry.add(name, root)
        return root

    def link(self, attacker, target, relative=False):
        (attacker / "campaign.json").unlink()
        (attacker / "campaign.json").symlink_to(os.path.relpath(target / "campaign.json", attacker) if relative
                                                else target / "campaign.json")

    def status(self, **patches):
        """(lines, current) with the real guard, tagging which campaign is being read."""
        real_root = registry._root

        def root_of(path):
            NOW["campaign"] = Path(os.path.realpath(path))
            return real_root(path)
        WATCH[:] = [Path(e["directory"]) for e in json.loads(Path(os.environ[registry.ENV]).read_text())]
        with mock.patch.object(registry, "_root", side_effect=root_of):
            result = registry.status_lines()
        WATCH.clear()
        self.assertEqual(registry._denied, (), "the guard outlived the status read")
        return result

    def test_status_config_symlink_never_opens_other_campaign(self):
        alpha, beta = self.campaign("alpha"), self.campaign("beta")
        self.link(alpha, beta)
        lines, current = self.status()
        self.assertEqual(SEEN, [], "alpha's turn opened beta past the guard")
        self.assertFalse(current)
        self.assertEqual(lines, [f"campaign | alpha | NOT CURRENT: ValueError{TAIL}",
                                 f"campaign | beta | {HEALTHY}{TAIL}"])

    def test_cli_exits_3_and_keeps_neighbour(self):
        alpha, beta = self.campaign("alpha"), self.campaign("beta")
        self.link(alpha, beta, relative=True)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "argv", ["deflock", "campaigns", "status"]):
            with self.assertRaises(SystemExit) as raised:
                main()
        code = raised.exception.code
        self.assertEqual(code, 3)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(out.getvalue().splitlines(), [f"campaign | alpha | NOT CURRENT: ValueError{TAIL}",
                                                       f"campaign | beta | {HEALTHY}{TAIL}"])
        self.assertEqual(registry._denied, ())

    def test_target_sorted_first_and_third_campaign_still_read(self):
        a, b, c = self.campaign("a-target", "a target's"), self.campaign("b-attacker"), self.campaign("c-other")
        self.link(b, a)
        lines, current = self.status()
        self.assertEqual(SEEN, [])
        self.assertFalse(current)
        self.assertEqual(lines, [f"campaign | a-target | {HEALTHY}{TAIL}",
                                 f"campaign | b-attacker | NOT CURRENT: ValueError{TAIL}",
                                 f"campaign | c-other | {HEALTHY}{TAIL}"])

    def test_refused_config_never_reaches_state(self):
        alpha, beta = self.campaign("alpha"), self.campaign("beta")
        self.link(alpha, beta)
        seen = []
        real_state = registry._state
        with mock.patch.object(registry, "_state", side_effect=lambda root: seen.append(root) or real_state(root)):
            self.status()
        self.assertEqual(seen, [beta])

    def test_pre_and_post_guards_use_the_same_snapshot(self):
        alpha, beta = self.campaign("alpha"), self.campaign("beta")
        calls, real = [], registry.confine
        with mock.patch.object(registry, "confine",
                               side_effect=lambda root, reg: calls.append((str(root), list(reg))) or real(root, reg)):
            registry.status_lines()
        registered = [str(alpha), str(beta)]
        self.assertEqual(calls, [(str(alpha), registered), (str(alpha), registered), ("", []),
                                 (str(beta), registered), (str(beta), registered), ("", [])])

    def test_failures_release_and_neighbours_survive(self):
        a, b, c = self.campaign("a"), self.campaign("b"), self.campaign("c")
        (b / "campaign.json").write_text("{not json", encoding="utf-8")
        lines, current = self.status()
        self.assertEqual([line.split(" | ")[2] for line in lines],
                         [HEALTHY, "NOT CURRENT: ValueError", HEALTHY])
        self.assertFalse(current)
        real_state = registry._state

        def boom(root):
            if root == a:
                raise RuntimeError("synthetic-canary-text")
            return real_state(root)
        with mock.patch.object(registry, "_state", side_effect=boom):
            lines, _ = registry.status_lines()
        self.assertEqual([line.split(" | ")[2] for line in lines],
                         ["NOT CURRENT: RuntimeError", "NOT CURRENT: ValueError", HEALTHY])
        self.assertNotIn("canary", "\n".join(lines))
        self.assertEqual(registry._denied, ())

    def test_ordinary_output_and_empty_register(self):
        self.assertEqual(registry.status_lines(), (["campaign | (none registered)" + TAIL], True))
        self.campaign("beta")
        self.campaign("Alpha")
        self.assertEqual(registry.status_lines(), ([f"campaign | Alpha | {HEALTHY}{TAIL}",
                                                    f"campaign | beta | {HEALTHY}{TAIL}"], True))


if __name__ == "__main__":
    unittest.main()
