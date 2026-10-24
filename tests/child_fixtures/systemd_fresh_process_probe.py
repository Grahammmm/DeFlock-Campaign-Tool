"""Child probe for tests/records/test_systemd_control.py (SystemdControlTests.assert_fresh_process_denied).

Moved verbatim from its inline source; arguments arrive in sys.argv order.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import json, sys
    from pathlib import Path
    from campaign_tool.records import container_host as host, systemd_control as control
    from tests.records.test_container_host import FakeDocker
    profile = Path(sys.argv[1])
    fake = FakeDocker(profile)
    try:
        host.run(profile, invoke=fake.invoke, containment=fake.containment)
        direct = "unexpected"
    except host.Rejected:
        direct = "rejected"
    result = control.run(profile, "3" * 32, invoke=fake.invoke, containment=fake.containment)
    print(json.dumps({"direct": direct, "bound": result["status"], "calls": len(fake.calls)}))
