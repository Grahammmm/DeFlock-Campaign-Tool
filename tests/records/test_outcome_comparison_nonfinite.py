"""Exercise non-finite input at the comparison publisher boundary."""
import json
import shutil
import unittest
from unittest.mock import patch

from campaign_tool.records.outcomes import comparison, reading
from tests.records import test_outcome_comparison as base
from tests.records.test_outcome_comparison_replay import FixedDay, RUN


def write_input(project, literal):
    payload = json.loads(base.FIXTURE.read_bytes())
    payload["agencies"][0]["facts"]["retention"]["value"]["data_layer"] = "synthetic-layer-marker"
    raw = json.dumps(payload).replace('"synthetic-layer-marker"', literal)
    (project / comparison.INPUT_NAME).write_text(raw, encoding="utf-8")


class NonfiniteComparisonInput(unittest.TestCase):
    def project(self, literal):
        project = base.OutputsStage().project(supplied=False).resolve()
        self.addCleanup(shutil.rmtree, project)
        write_input(project, literal)
        return project

    def test_nonfinite_numbers_refused_without_artifacts(self):
        # The observed value can reach both reports even when the data-layer check fails.
        for literal in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            with self.subTest(literal=literal):
                project = self.project(literal)
                out = project / "run-outputs" / RUN
                refusal = None
                try:
                    comparison.write_run_comparison(project, RUN)
                except OSError as error:
                    refusal = str(error)
                artifacts = sorted(p.name for p in out.iterdir()) if out.exists() else []
                self.assertEqual((refusal, artifacts), ("comparison_input_invalid", []))

    def test_finite_number_publishes_readable_comparison(self):
        # A blanket ban on JSON floats would keep the invalid cases green but break this fixture.
        project = self.project("3.75e1")
        with patch.object(comparison, "datetime", FixedDay):
            comparison.write_run_comparison(project, RUN)
        out = project / "run-outputs" / RUN
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["comparison.json", "comparison.md"])
        section = reading.render(out).split('<section id="comparison">', 1)[1].split("</section>", 1)[0]
        self.assertIn('<th scope="row">retention</th>', section)
        self.assertIn("<dt>data_layer</dt><dd>37.5</dd>", section)
