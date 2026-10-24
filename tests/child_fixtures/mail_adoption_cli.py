"""Child probe for tests/records/test_mail_adoption_recovery.py (AdoptionRecoveryTests.cli via run_python).

Moved verbatim from its inline source; arguments arrive in sys.argv order.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import sys
    from campaign_tool.records import mail_adoption
    sys.argv[0]='adopt-mail'
    sys.exit(mail_adoption.main())
