"""Nested child of bounded_resume_output_fence.py for
tests/records/test_bounded_resume.py; writes only to inherited stdout.

Takes no arguments.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import os; os.write(1, b"synthetic-private-child")
