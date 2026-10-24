"""Owned-client child for tests/records/test_container_host.py
(ContainerHostTests.test_real_owned_client_timeout_and_output_cap).

Writes one MiB to stdout so the output cap fires. Takes no arguments.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import sys; sys.stdout.buffer.write(b'x'*1048576)
