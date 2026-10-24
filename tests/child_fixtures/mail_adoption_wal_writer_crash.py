"""Child probe for tests/records/test_mail_adoption_recovery.py (AdoptionRecoveryTests.test_nonempty_wal_with_missing_shm_dry_run_fails_closed_without_mutation).

Moved verbatim from its inline source; arguments arrive in sys.argv order.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import os, sqlite3, sys
    con = sqlite3.connect(sys.argv[1])
    con.execute("PRAGMA wal_autocheckpoint=0")
    # Rewrites header page 1 with the same value: a real WAL frame, no semantic change.
    con.execute("PRAGMA user_version=%d" % con.execute("PRAGMA user_version").fetchone()[0])
    con.commit()
    os._exit(0)   # committed WAL frames, never checkpointed
