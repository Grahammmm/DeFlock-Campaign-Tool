"""Child probe for tests/records/test_mail_adoption_recovery.py (AdoptionRecoveryTests.test_crash_after_enrollment_resumes_in_fresh_process).

Moved verbatim from its inline source; arguments arrive in sys.argv order.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import os, sys
    from campaign_tool.records import mail_adoption
    from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
    def crash(*args):
        os._exit(17)   # hard crash: mail_messages already committed, nothing else runs
    def factory(*args):
        return CanonicalMailBackend(*args, stage_runner_factory=crash)
    mail_adoption.adopt(sys.argv[1], sys.argv[2], config_path=sys.argv[3], apply=True, backend_factory=factory)
