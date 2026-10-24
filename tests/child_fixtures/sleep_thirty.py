"""Unrelated long-lived child for tests/records/test_worker_lifecycle.py
(WorkerLifecycleTests.test_unrelated_process_not_signalled).

Takes no arguments; sleeps 30 seconds unless signalled.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import time; time.sleep(30)
