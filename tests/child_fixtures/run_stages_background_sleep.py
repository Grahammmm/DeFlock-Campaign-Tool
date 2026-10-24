"""Stage child for tests/test_records_run_stages.py (DeadlineTests).

Stands in for ``/bin/sh -c "sleep 30 & echo $! > PIDFILE; wait"`` (mode
``wait``, test_child_group_killed_on_timeout) and for
``/bin/sh -c "sleep 30 & echo $! > PIDFILE; echo '{"ok": 1}'"`` (mode ``ok``,
test_child_descendants_killed_after_normal_exit). As with the shell's
asynchronous list, the sleep grandchild stays in this process's group and
session, inherits stdout and stderr, reads stdin from /dev/null and ignores
SIGINT and SIGQUIT. Arguments: pidfile, mode.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''
import os
import signal
import subprocess


def ignore_interrupts():
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGQUIT, signal.SIG_IGN)


if __name__ == "__main__":
    pidfile, mode = sys.argv[1], sys.argv[2]
    sleep = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL, preexec_fn=ignore_interrupts)
    # The grandchild must share the stage's group and session, as under sh.
    if os.getpgid(sleep.pid) != os.getpgrp() or os.getsid(sleep.pid) != os.getsid(0):
        sys.exit("grandchild left the stage process group or session")
    with open(pidfile, "w") as handle:
        handle.write("%d\n" % sleep.pid)
    if mode == "wait":
        sleep.wait()
    else:
        print('{"ok": 1}', flush=True)
