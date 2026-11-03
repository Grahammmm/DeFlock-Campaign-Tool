#!/usr/bin/env python3
"""G's approval of one Flock EMAIL-DRAFT.md as a signed FIELD. Never sends; the only file it writes is EMAIL-APPROVAL.json.

  flock_approve.py payload RUNDIR > payload.txt        the exact text G reads and signs
  ssh-keygen -Y sign -f KEY -n deflock-approval payload.txt   (G, by hand; writes payload.txt.sig)
  flock_approve.py approve RUNDIR --signature payload.txt.sig   writes EMAIL-APPROVAL.json only if it verifies
  flock_approve.py verify RUNDIR                       exit 0 only while the stored approval still verifies (run before sending)
  flock_approve.py status STORE                        flock_email status with every signature checked; exit 1 on any
                                                       approval that does not verify

A signature verifies only against the root-owned trust file in campaign_tool.approval; a typed name cannot approve.
`verified` is the check flock_email.stage accepts as its verifier; flock_requests open and --check use it too.
--signature must be a regular, non-link file of at most approval.MAX_SIGNATURE bytes of UTF-8 SSH-armored text.
"""
import errno, hashlib, importlib.util, json, os, stat, sys, tempfile
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import approval  # noqa: E402
USAGE = (__doc__ or "").strip()


def _email():  # the sibling is loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_approve_flock_email", Path(__file__).resolve().parent / "flock_email.py")
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module


def _run(rundir):
    run = Path(rundir).absolute()
    if run.is_symlink() or not (len(run.parents) >= 4 and run.parent.name == "runs" and run.parents[2].name == "agencies"):
        raise ValueError("RUNDIR must be <store>/agencies/<slug>/runs/<date>")
    return run


def read_signature(path):
    """The --signature text, read without following a link or blocking; ValueError names the rule it breaks."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:  # O_NOFOLLOW fails with ELOOP on a link
        raise (ValueError("signature file is a link") if exc.errno == errno.ELOOP else exc) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("signature file is not a regular file")
        raw = os.read(fd, approval.MAX_SIGNATURE + 1)  # a regular file returns all it has up to the limit
    finally:
        os.close(fd)
    if len(raw) > approval.MAX_SIGNATURE:
        raise ValueError("signature file is too large")
    try:  # newlines translated as Path.read_text did, so the stored signature is unchanged
        lines = (text := raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")).split("\n")
    except UnicodeDecodeError:
        raise ValueError("signature file is not UTF-8 text") from None
    begin = "-----BEGIN SSH SIGNATURE-----"
    if begin not in lines or "-----END SSH SIGNATURE-----" not in lines[lines.index(begin) + 1:]:
        raise ValueError("signature file is not an SSH signature")
    return text


def _ready(email, run):
    """The run's email stage (no verifier) allows payload/approve: a drafted email, or an existing approval (approve then
    fails with `already exists`). A send recorded without approval, or any draft that is not ready, raises ValueError."""
    st = email.stage(run)
    if st == email.STAGES[5]:
        raise ValueError("email already recorded as sent")
    if st not in ("email drafted", email.STAGES[6], email.STAGES[7]):
        raise ValueError("email draft is not ready for approval")
    return run


def _payload(email, run):
    subject, body = email.approved_text(run)
    return approval.payload("flock-email", subject, body), subject, body


def verified(run, rec, email=None):
    """True only when rec's signature, by its recorded approver, verifies over the run's current exact text."""
    try:
        email = email or _email()
        return approval.verify(_payload(email, Path(run))[0], rec["signature"]) == rec["approved_by"]
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, approval.ApprovalError):
        return False


def main(argv):
    if argv in (["--help"], ["-h"]):
        return print(USAGE) or 0
    email = _email()
    try:
        if len(argv) == 2 and argv[0] == "payload":
            sys.stdout.write(_payload(email, _ready(email, _run(argv[1])))[0].decode("utf-8"))
            return 0
        if len(argv) == 4 and argv[0] == "approve" and argv[2] == "--signature":
            run = _ready(email, _run(argv[1]))
            data, subject, body = _payload(email, run)
            sig = read_signature(argv[3])
            who = approval.verify(data, sig)
            rec = {"v": 1, "state": "approved", "scope": "flock-email", "subject": subject, "approved_by": who,
                   "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(), "signature": sig}
            fd, tmp = tempfile.mkstemp(dir=run, prefix=f".{email.APPROVAL}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, indent=1, sort_keys=True)
                os.link(tmp, run / email.APPROVAL)  # never replaces an existing approval
            finally:
                os.unlink(tmp)
            return print(f"approve: state=approved approved_by={who}") or 0
        if len(argv) == 2 and argv[0] == "verify":
            run = _run(argv[1])
            rec = email.approval_field(run)
            if not rec:
                raise ValueError("no well-formed approval bound to the current draft")
            if approval.verify(_payload(email, run)[0], rec["signature"]) != rec["approved_by"]:
                raise ValueError("signer is not the recorded approver")
            return print(f"verify: ok approved_by={rec['approved_by']}") or 0
        if len(argv) == 2 and argv[0] == "status":
            return email.check(argv[1], lambda folders: email.status(folders, lambda run, rec: verified(run, rec, email)))
    except FileExistsError:
        return print(f"{argv[0]}: {email.APPROVAL} already exists", file=sys.stderr) or 1
    except (OSError, UnicodeDecodeError, ValueError, approval.ApprovalError) as exc:
        return print(f"{argv[0]}: refused: {exc}", file=sys.stderr) or 1
    return print(USAGE, file=sys.stderr) or 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
