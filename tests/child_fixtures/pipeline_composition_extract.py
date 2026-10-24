"""Installed-wrapper extraction child for tests/records/test_pipeline_composition.py
(PipelineCompositionTests.compose).

Runs from the installed WP4 root (its cwd); arguments arrive in sys.argv order:
original path, original SHA-256, extraction root.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import json,sys;from campaign_tool.records.extraction_routes import extract;print(json.dumps(extract(sys.argv[1],sys.argv[2],sys.argv[3],form='txt',timeout=20)))
