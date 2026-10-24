"""Child probe for tests/records/test_ledger_catalog_acceptance.py
(SnapshotAcceptanceTests.test_package_composition_without_host_path_discovery).

Run under ``python -I``. ``sys.argv[1]`` is the composed package root that the
inline source used to interpolate; nothing else on the host path is used.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import sys,json;sys.path.insert(0,sys.argv[1]);from campaign_tool.records.ledger_catalog import dependency_contract;from campaign_tool.records.ledger_catalog_acceptance import accept_snapshot;print(json.dumps(dependency_contract()))
