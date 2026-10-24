"""Owned-client child for tests/records/test_container_host.py
(ContainerHostTests.test_real_owned_client_timeout_and_output_cap).

Sleeps ten seconds so the client deadline fires. Takes no arguments.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import time; time.sleep(10)
