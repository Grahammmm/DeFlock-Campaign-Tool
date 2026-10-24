"""Child probe for tests/records/test_mail_adoption_recovery.py (AdoptionRecoveryTests.test_crash_after_stage_claim_is_durably_incomplete_until_lease_expiry).

Moved verbatim from its inline source; arguments arrive in sys.argv order.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import os, sys
    from campaign_tool.records import mail_adoption
    from campaign_tool.records.runner import canonical_mail
    real = canonical_mail.installed_preservation_runner
    class Crashing:
        def __init__(self, inner): self.inner = inner
        def __getattr__(self, name): return getattr(self.inner, name)
        def promote(self, *args, **kw): os._exit(18)   # lease claimed, receipt not promoted
    canonical_mail.installed_preservation_runner = lambda *a, **kw: Crashing(real(*a, **kw))
    mail_adoption.adopt(sys.argv[1], sys.argv[2], config_path=sys.argv[3], apply=True)
