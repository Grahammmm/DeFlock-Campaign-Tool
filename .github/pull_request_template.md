## Change

<!-- What changed and why. -->

## Tests and scans run

<!-- Exact commands and results, including the credential scan and checked scope. -->

## Import-manifest pin refresh

See [the checklist](../AGENTS.md#import-manifest-pin-refresh-checklist). List every changed path from its table, or write not applicable with a reason.

Changed pinned paths and their manifest entries: <!-- paths, or N/A: reason -->

- [ ] Author: `engine_sha256` refreshed in the matching manifest entry from the final file bytes (or not applicable, reason above).
- [ ] Author: whole-manifest `APPROVED_MANIFESTS` entry in `tools/check_secret_report.py` refreshed after the final manifest bytes (or not applicable).
- [ ] Author: validation command and result pasted: `python3 -B -m unittest tests.test_provenance_scan -v`.
- [ ] Reviewer: checked every changed path against the table, both hash layers and the actual validation output.
