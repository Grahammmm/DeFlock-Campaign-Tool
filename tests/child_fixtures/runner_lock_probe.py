"""Second-process writer-lock probe for tests/records/test_runner.py
(RunnerTests.test_second_process_lock).

``sys.argv[1]`` is the runner control root. Prints ``acquired`` or ``blocked``.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


def process_lock_probe(root):
    from campaign_tool.records.runner import core
    try:
        with core.writer_lock(root):print('acquired')
    except ValueError:print('blocked')


if __name__ == "__main__":
    process_lock_probe(sys.argv[1])
