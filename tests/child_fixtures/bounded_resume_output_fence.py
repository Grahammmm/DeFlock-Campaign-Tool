"""Output-fence child for tests/records/test_bounded_resume.py
(BoundedResumeTests.test_raw_fd_and_inherited_child_output_do_not_escape).

The adapter writes to the raw descriptors and starts one short-lived nested
child (bounded_resume_inherited_child.py) that inherits them. Takes no arguments.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import json, os, subprocess, sys
    from pathlib import Path
    from campaign_tool.records.bounded_resume import run_once
    CHILD = Path(__file__).resolve().with_name('bounded_resume_inherited_child.py')
    def adapter(request):
        os.write(1, b'synthetic-private-stdout')
        os.write(2, b'synthetic-private-stderr')
        subprocess.run([sys.executable, str(CHILD)], check=True)
    print(json.dumps(run_once(adapter, execute=True, seconds=2)))
