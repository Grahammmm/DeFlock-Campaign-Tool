"""POSIX owner-only atomic report replacement without following symlinks.

Callers must keep the enclosing private workspace owner-controlled. This guards
path traversal and replaces hardlinks rather than modifying their target inode.
Multiple report files are individually atomic, not one multi-file transaction.
"""
import os
from pathlib import Path
import secrets
import stat


def write_private(path, text, *, create_parents=False):
    path = Path(path)
    if ".." in path.parts:
        raise ValueError("output path must not contain parent traversal")
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.name:
        raise ValueError("output must name a file")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(path.anchor, flags)
    temporary = None
    try:
        for part in path.parts[1:-1]:
            try:
                child = os.open(part, flags, dir_fd=directory)
            except FileNotFoundError:
                if not create_parents:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child

        def check_target():
            try:
                info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("output target is not a regular file")

        check_target()
        # O_EXCL prevents a pre-existing temporary file/link from being followed.
        for _ in range(10):
            candidate = ".records-report-" + secrets.token_hex(16)
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
            except FileExistsError:
                continue
            temporary = candidate
            break
        else:
            raise FileExistsError("cannot allocate private report temporary file")
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        check_target()
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
        os.fsync(directory)
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)
