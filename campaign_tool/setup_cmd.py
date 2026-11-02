"""Guided, offline composition of the existing campaign commands."""
import json
import re
from contextlib import redirect_stdout
from types import SimpleNamespace


def ask(stdin, stdout, answers=None):
    """Ask only for missing campaign fields; abort before writing on EOF/cancel."""
    answers = dict(answers or {})

    def prompt(label):
        stdout.write(label + ": ")
        stdout.flush()
        value = stdin.readline()
        if not value:
            raise ValueError("Setup cancelled before any changes")
        return value.strip()

    try:
        for field in ("name", "county"):
            if answers.get(field) is None:
                answers[field] = prompt("Campaign name" if field == "name" else "County")
        if answers.get("state") is None:
            for _ in range(3):
                state = prompt("Two-letter state").upper()
                if re.fullmatch(r"[A-Z]{2}", state):
                    answers["state"] = state
                    break
                stdout.write("Use a two-letter state code.\n")
            else:
                raise ValueError("State must be a two-letter code; nothing changed")
        if answers.get("location") is None:
            answers["location"] = prompt("City/place (optional; Enter to skip)")
    except KeyboardInterrupt:
        raise ValueError("Setup cancelled before any changes") from None
    return answers


def run_cli(args, root, parser, stdin, stdout):
    answers = {key: getattr(args, key) for key in ("name", "county", "state", "location")}
    missing = ["--" + key for key in ("name", "county", "state") if answers[key] is None]
    if missing and (args.yes or not stdin.isatty()):
        parser.error("setup requires " + ", ".join(missing))
    if not args.yes and stdin.isatty() and (missing or answers["location"] is None):
        answers = ask(stdin, stdout, answers)
    return run_setup(root, answers)


def run_setup(root, answers, out=print):
    """Run init → offline kit → starter build → doctor, retaining finished work."""
    from .cli import doctor_report, initialize
    from .kit import build_kit
    from .site import build_site

    steps, open_items = [], []
    names = ("Campaign file", "Request drafts", "Starter site", "Readiness")
    for number, what in enumerate(names, 1):
        try:
            result = "done"
            if number == 1:
                path = root / "campaign.json"
                if path.exists():
                    cfg = json.loads(path.read_text(encoding="utf-8"))
                    if any(cfg.get(key) != answers[key] for key in ("name", "county")) or str(
                            cfg.get("state", "")).upper() != answers["state"].upper():
                        raise ValueError(f"campaign.json already describes {cfg.get('name')}, "
                                         f"{cfg.get('county')} {cfg.get('state')}; nothing changed. "
                                         "Use another directory or edit campaign.json.")
                    result = "kept"
                else:
                    # Keep progress to one summary line per step.
                    import io
                    with redirect_stdout(io.StringIO()):
                        initialize(SimpleNamespace(**answers), root)
            elif number == 2:
                build_kit(root, online=False)
            elif number == 3:
                build_site(root)
                out(str(root / "public"))
            else:
                report = doctor_report(root)
                open_items = report["next_actions"]
            steps.append(result)
            out(f"Step {number} of 4: {what} … {result}")
        except (OSError, ValueError) as exc:
            out(f"Step {number} of 4: {what} … stopped")
            raise ValueError(f"Stopped at step {number} ({what}): {exc}. "
                             "Fix it and run setup again; finished steps are kept.") from exc
    requests = sorted(str(path.relative_to(root)) for path in (root / "kit/requests").glob("*.md"))
    out("Drafted requests to review (nothing was sent):")
    for path in requests:
        out(path)
    out("Open items:")
    for item in open_items:
        out("- " + item)
    out("Setup finished. Review every draft before you send anything; sending is always your decision.")
    return {"steps": steps, "requests": requests, "open_items": open_items}
