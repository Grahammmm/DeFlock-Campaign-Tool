#!/usr/bin/env python3
"""G's approval of one Flock EMAIL-DRAFT.md as a signed FIELD. Never sends; the only file it writes is EMAIL-APPROVAL.json.

  flock_approve.py payload RUNDIR > payload.txt        the exact text G reads and signs
  ssh-keygen -Y sign -f KEY -n deflock-approval payload.txt   (G, by hand; writes payload.txt.sig)
  flock_approve.py approve RUNDIR --signature payload.txt.sig   writes EMAIL-APPROVAL.json only if it verifies
  flock_approve.py verify RUNDIR                       exit 0 only while the stored approval still verifies (run before sending)

A signature verifies only against the root-owned trust file in campaign_tool.approval; a typed name cannot approve.
"""
import hashlib, importlib.util, json, os, sys, tempfile
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


def _payload(email, run):
    subject, body = email.approved_text(run)
    return approval.payload("flock-email", subject, body), subject, body


def main(argv):
    if argv in (["--help"], ["-h"]):
        return print(USAGE) or 0
    email = _email()
    try:
        if len(argv) == 2 and argv[0] == "payload":
            sys.stdout.write(_payload(email, _run(argv[1]))[0].decode("utf-8"))
            return 0
        if len(argv) == 4 and argv[0] == "approve" and argv[2] == "--signature":
            run = _run(argv[1])
            data, subject, body = _payload(email, run)
            sig = Path(argv[3]).read_text(encoding="utf-8")
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
    except FileExistsError:
        return print(f"{argv[0]}: {email.APPROVAL} already exists", file=sys.stderr) or 1
    except (OSError, UnicodeDecodeError, ValueError, approval.ApprovalError) as exc:
        return print(f"{argv[0]}: refused: {exc}", file=sys.stderr) or 1
    return print(USAGE, file=sys.stderr) or 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
