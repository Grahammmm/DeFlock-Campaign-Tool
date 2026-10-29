"""File-launched, offline-only test/CLI child. No inline interpreter children."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from tests.fixtures.fa1302.boundary import Boundary, cli_adapter

COMPATIBILITY = [
    "tests.records.test_mail_adoption_bindings.CanonicalBindingDriftTests." + name
    for name in (
        "test_occurrence_moved_to_other_identity_is_refused",
        "test_changed_acquisition_method_is_refused",
        "test_missing_mail_occurrence_is_refused",
        "test_unchanged_bindings_stay_complete_and_byte_identical")]


def main():
    if len(sys.argv) < 3:
        return 2
    mode, output = sys.argv[1], Path(sys.argv[2])
    boundary = Boundary(output)
    with boundary.enter(audit=True):
        if mode == "cli":
            from campaign_tool.records import mail_adoption
            sys.argv = ["adopt-mail", *sys.argv[3:]]
            return mail_adoption.main()
        if mode == "probe":
            from campaign_tool.records import mail_adoption
            result = {"module": mail_adoption.__file__, "repo": str(REPO),
                      "temp": __import__("tempfile").gettempdir(),
                      "bytecode_disabled": sys.dont_write_bytecode, "events": boundary.events}
            (output / "probe.json").write_text(json.dumps(result, indent=2) + "\n")
            return 0
        if mode == "compatibility":
            from tests.records import test_mail_adoption_recovery as recovery
            selectors = COMPATIBILITY
            scope = patch.object(recovery, "run_python", cli_adapter(output))
        elif mode in {"behavior", "negative-control", "isolation"}:
            selectors = ["tests.records.test_fa1302." + {
                "behavior": "Behavior", "negative-control": "GateControl", "isolation": "Isolation"}[mode]]
            from contextlib import nullcontext
            scope = nullcontext()
        else:
            raise ValueError("unknown_worker_mode")
        with scope:
            suite = unittest.defaultTestLoader.loadTestsFromNames(selectors)
            selected = suite.countTestCases()
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        import campaign_tool.records.mail_adoption as module
        data = {"selectors": selectors, "selected": selected, "tests": result.testsRun,
                "failures": len(result.failures), "errors": len(result.errors), "skips": len(result.skipped),
                "module": module.__file__, "interpreter": sys.executable,
                "events": boundary.events,
                "modules": {name: mod.__file__ for name, mod in sorted(sys.modules.items())
                            if name.startswith(("campaign_tool", "tests.records", "tests.fixtures.fa1302"))
                            and getattr(mod, "__file__", None)}}
        (output / "tests.json").write_text(json.dumps(data, indent=2) + "\n")
        return int(not (selected > 0 and result.testsRun == selected and result.wasSuccessful()
                        and not result.skipped and not boundary.events))


if __name__ == "__main__":
    raise SystemExit(main())
