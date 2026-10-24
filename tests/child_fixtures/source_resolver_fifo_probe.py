"""Child probe for tests/records/test_structured_source_resolver.py (SourceResolverTests.test_substituted_original_and_unit_fifos_refuse_within_subprocess_deadline).

Moved verbatim from its inline source; arguments arrive in sys.argv order.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import sys; from campaign_tool.records.detectors.sources import resolve_sources, ResolutionError;
    try:
     resolve_sources(database=sys.argv[1], original_root=sys.argv[2], derived_root=sys.argv[3], subject_sha256=sys.argv[4])
    except ResolutionError:
     sys.exit(0)
    raise SystemExit('FIFO incorrectly accepted')
