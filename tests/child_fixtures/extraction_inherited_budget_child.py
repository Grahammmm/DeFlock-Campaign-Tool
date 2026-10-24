"""Child probe for tests/records/test_extraction_inherited_budget.py (InheritedExtractionBudgetTests.test_native_text_respects_parent_caps_and_records_effective_memory).

Moved verbatim from its inline source; arguments arrive in sys.argv order.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import json,sys; from campaign_tool.records import extraction_routes as r; x=r.extract(sys.argv[1],sys.argv[2],sys.argv[3],form='txt',timeout=20); print(json.dumps(x))
