"""Create a synthetic committed WAL database without closing or checkpointing."""
import os
import sqlite3
import sys


if __name__ == "__main__":
    c = sqlite3.connect(sys.argv[1])
    c.execute('pragma journal_mode=wal')
    c.execute('create table synthetic(value)')
    c.executemany('insert into synthetic values (?)', [(n,) for n in range(42)])
    c.commit()
    # Bypass connection cleanup so committed frames and both sidecars remain.
    os._exit(0)
