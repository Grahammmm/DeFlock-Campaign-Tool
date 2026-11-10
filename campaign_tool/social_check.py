"""Read-only check of every private social-draft folder (DF1622-04). Nothing is written or posted.

``check_folders`` covers the fixed roster of folders ``social.check_social`` does not read itself: finding drafts
per platform (``kit/social/<p>``), and meeting, outcome, thread and event drafts (``kit/social/<kind>`` and
``<kind>/<p>``), each with its ``.suggested`` sibling (and the legacy ``kit/social.suggested``). Absent folders are
skipped; a link or non-folder in the roster is named as a folder problem and never entered. States are the legacy
codes (ok, missing, outdated, edited, not_regular); each folder uses its own length rule. A draft whose source is gone
or changed is listed as stale with reasons, never as an ordinary problem. Meeting, outcome and event provenance comes from the
private ``.check.json`` that ``social._write_drafts`` writes; without a usable one the original starts_at / window is
unknown and is never guessed.
"""
import os
import re
import stat
from datetime import datetime, timedelta, timezone

from . import social as S
from . import social_threads as T

ROSTER_KINDS = ("meetings", "outcomes", "threads", "events")
_COUNTER = re.compile(r"^\(\d+/\d+\) ")


def _read(path):
    try:
        return S._read_regular(path)
    except OSError:
        return None


def _dir_state(path):
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unsafe"
    return "dir" if stat.S_ISDIR(mode) else "unsafe"


def _body(data):
    lines = data.decode("utf-8", errors="replace").split("\n")
    if lines[0].lstrip().startswith("<!--") and lines[0].rstrip().endswith("-->"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _limit(platform):
    return S.PLATFORMS[platform][0] if platform else S.SOCIAL_MAX_CHARS


def _length(text, platform):
    return S.post_length(text, platform) if platform else len(text)


def _roster():
    """``(folder parts under root, kind, platform, suggested, parent parts or None)`` in deterministic order."""
    out = [(("kit", "social.suggested"), "findings", None, True, None)]
    for p in S.PLATFORMS:
        out.append((("kit", "social", p), "findings", p, False, None))
        out.append((("kit", "social", p + ".suggested"), "findings", p, True, None))
    for kind in ROSTER_KINDS:
        base = ("kit", "social", kind)
        out.append((base, kind, None, False, None))
        out.append((("kit", "social", kind + ".suggested"), kind, None, True, None))
        for p in S.PLATFORMS:
            out.append((base + (p,), kind, p, False, base))
            out.append((base + (p + ".suggested",), kind, p, True, base))
    return out


class _Context:
    def __init__(self, root, findings, base_url, now):
        self.root, self.base_url, self.now_arg = root, base_url, now
        self.published = {f["slug"]: f for f in findings}
        self._meetings = self._threads = None
        self._thread_cache = {}
        self._events = None
        self.event_now = now or datetime.now(timezone.utc)
        if self.event_now.tzinfo is None:
            self.event_now = self.event_now.replace(tzinfo=timezone.utc)

    def events(self):
        if self._events is None:
            try:
                self._events = {e["id"]: e for e in S.read_events(self.root) or []}
            except (ValueError, OSError):
                self._events = False
        return None if self._events is False else self._events

    def meetings(self):
        """``(meetings by id, now)`` or ``None`` when the staged meetings cannot be read."""
        if self._meetings is None:
            try:
                loaded, _, _, now = S._staged_meetings(self.root, self.now_arg)
                self._meetings = ({m["id"]: m for m in loaded}, now)
            except (ValueError, OSError):
                self._meetings = False
        return self._meetings or None

    def thread(self, finding, platform):
        """``(posts, bodies)`` for the finding's current thread, or ``None`` when it cannot be rendered."""
        key = (finding["id"], platform)
        if key not in self._thread_cache:
            try:
                f, model, base_url, pages = T._load(self.root, finding["id"])
                posts = T.thread_posts(f, model["sources"], base_url, pages, platform)
                self._thread_cache[key] = (posts, [_COUNTER.sub("", p) for p in posts])
            except (ValueError, OSError, KeyError):
                self._thread_cache[key] = None
        return self._thread_cache[key]


def _starts(meeting):
    stamp = meeting["starts_at"]
    return datetime.fromisoformat(stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp)


# --- per kind: stale reasons, required stems, fresh draft, edited-draft problems --------------------------------

def _eligible_meetings(ctx, window):
    got = ctx.meetings()
    if got is None:
        return {}
    meetings, now = got
    return {i: m for i, m in meetings.items() if m["status"] == "scheduled" and now <= _starts(m) <= now + timedelta(days=window)
            and m["participation"] is not None}


def _eligible_outcomes(ctx, window):
    got = ctx.meetings()
    if got is None:
        return {}
    meetings, now = got
    first = now.date() - timedelta(days=window - 1)
    return {i: m for i, m in meetings.items() if m["status"] == "scheduled" and m["outcome"] is not None
            and first <= S.date.fromisoformat(m["outcome"]["on"]) <= now.date()}


def _stale_meeting(ctx, stem, meta):
    meeting = ctx.meetings()[0].get(stem)
    if meeting is None:
        return ["meeting removed or unreadable"]
    reasons = []
    if meeting["status"] != "scheduled":
        reasons.append("meeting cancelled")
    if _starts(meeting) < ctx.meetings()[1]:
        reasons.append("meeting is past")
    if meeting["participation"] is None:
        reasons.append("meeting participation no longer verified")
    if meta and meta["starts_at"] != meeting["starts_at"]:
        reasons.append(f"rescheduled (was {meta['starts_at']}, now {meeting['starts_at']})")
    return reasons


def _stale_outcome(ctx, stem, meta):
    meeting = ctx.meetings()[0].get(stem)
    if meeting is None:
        return ["meeting removed or unreadable"]
    reasons = []
    if meeting["status"] != "scheduled":
        reasons.append("meeting cancelled")
    if meeting["outcome"] is None:
        return reasons + ["outcome removed"]
    if meta:
        if meta["outcome"] != meeting["outcome"]:
            reasons.append("outcome changed")
        first = ctx.meetings()[1].date() - timedelta(days=meta["within_days"] - 1)
        if not first <= S.date.fromisoformat(meeting["outcome"]["on"]) <= ctx.meetings()[1].date():
            reasons.append(f"outcome left its {meta['within_days']}-day window")
    else:
        # No bound provenance: fall back to what a fresh default-14-day generation would select.
        now = ctx.meetings()[1]
        if not now.date() - timedelta(days=13) <= S.date.fromisoformat(meeting["outcome"]["on"]) <= now.date():
            reasons.append("outcome is outside the default 14-day window and its original window is unknown; rebuild with social --outcomes")
    return reasons


def _eligible_events(ctx, window):
    return {i: e for i, e in ctx.events().items()
            if ctx.event_now <= S.parse_moment(e["starts_at"]) <= ctx.event_now + timedelta(days=window)}


def _stale_event(ctx, stem, meta):
    event = ctx.events().get(stem)
    if event is None:
        return ["event removed"]
    starts = S.parse_moment(event["starts_at"])
    reasons = ["event is past"] if starts < ctx.event_now else []
    if meta and meta["starts_at"] != event["starts_at"]:
        reasons.append(f"rescheduled (was {meta['starts_at']}, now {event['starts_at']})")
    window = meta["within_days"] if meta else 14
    if starts > ctx.event_now + timedelta(days=window):
        reasons.append(f"event left its {window}-day window" if meta else
                       "event is outside the default 14-day window and its original window is unknown; rebuild with social --events")
    return reasons


def _event_problems(event, data, ctx, platform):
    body = _body(data)
    problems = ["over_cap"] if _length(body, platform) > _limit(platform) else []
    if f"{ctx.base_url.rstrip('/')}/events.html#event-{event['id']}" not in body:
        problems.append("no_link")
    if S._event_when(event["starts_at"]) not in body or (event.get("venue") or "Online") not in body:
        problems.append("outdated")
    return problems


def _finding_problems(finding, data, ctx, platform):
    problems = [c for c in S._edit_problems(finding, data, ctx.base_url) if c != "over_cap"]
    if _length(_body(data), platform) > _limit(platform):
        problems.insert(0, "over_cap")
    return problems


def _meeting_problems(meeting, data, ctx, platform):
    body = _body(data)
    problems = ["over_cap"] if _length(body, platform) > _limit(platform) else []
    link = f"{ctx.base_url.rstrip('/')}/meetings/{meeting['id']}.html" if ctx.base_url else None
    if (link and link not in body) or S._when(meeting["starts_at"]) not in body:
        problems.append("outdated")
    return problems


def _outcome_problems(meeting, data, ctx, platform):
    body, outcome = _body(data), meeting["outcome"]
    problems = ["over_cap"] if _length(body, platform) > _limit(platform) else []
    wanted = [outcome["result"], outcome["source_url"]] + ([outcome["vote"]] if "vote" in outcome else [])
    if ctx.base_url:
        wanted.append(f"{ctx.base_url.rstrip('/')}/meetings/{meeting['id']}.html")
    if any(str(w) not in body for w in wanted):
        problems.append("outdated")
    return problems


def _thread_problems(finding, data, ctx, platform, rendered):
    parts = _body(data).split(T.SEPARATOR)
    problems = []
    if any(_length(part, platform) > _limit(platform) for part in parts):
        problems.append("over_cap")
    if not 1 <= len(parts) <= T.MAX_POSTS or any(not part.startswith(f"({n}/{len(parts)}) ") for n, part in enumerate(parts, 1)):
        problems.append("outdated")
    if f"{ctx.base_url.rstrip('/')}/findings/{finding['slug']}.html" not in parts[0]:
        problems.append("no_link")
    expected = rendered[1][1:] if rendered else []
    text = "\n".join(parts)
    if finding["corrections"]:
        if expected and expected[-1] not in text or not expected and not any("Correction " in p for p in parts):
            problems.append("missing_correction")
        expected = expected[:-1]
    if any(piece not in text for piece in expected) or rendered is None:
        problems.append("outdated")
    return list(dict.fromkeys(problems))


# --- folder check ---------------------------------------------------------------------------------------------

def _bound(entry, kind, stem, suggested, record, data):
    """The provenance entry when it is bound to this draft's bytes (suggested) or its written baseline (primary)."""
    if entry is None or entry.get("kind") != kind:
        return None
    if suggested:
        return entry if data is not None and S._sha(data) == entry["sha256"] else None
    return entry if record.get(stem) == entry["sha256"] else None


def _folder(root, parts, kind, platform, suggested, ctx):
    path = root.joinpath(*parts)
    out = {"drafts": [], "stale": [], "stale_reasons": {}, "problems": 0}
    notes = []
    names = None
    try:
        names = sorted(os.listdir(path))
    except OSError:
        pass
    if names is None:
        out["folder_problems"], out["problems"] = ["folder cannot be read"], 1
        return out
    stems = {n[:-3] for n in names if n.endswith(".md") and len(n) > 3}
    record, rstate = ({}, "absent") if suggested else S._read_state(path / ".written.json", "record")
    meta, mstate = S._read_state(path / ".check.json", "meta") if kind in ("meetings", "outcomes", "events") else ({}, "absent")
    for state, name in ((rstate, ".written.json"), (mstate, ".check.json")):
        if state == "bad":
            notes.append(f"{name} is not a usable regular file; its contents are ignored")
    if not suggested and kind == "threads" and not stems and not record:
        return None
    if not suggested and kind in ("meetings", "outcomes", "events") and platform is None and not stems and not os.path.lexists(path / ".written.json"):
        return None  # only platform sub-folders live here
    if suggested and not stems:
        return None

    if kind in ("meetings", "outcomes") and ctx.meetings() is None:
        notes.append("staged meetings cannot be read; meeting-based checks skipped")
        out["folder_problems"], out["problems"] = notes, len(notes)
        return out

    if kind == "events" and ctx.events() is None:
        notes.append("staged events cannot be read; event checks skipped")
        out["folder_problems"], out["problems"] = notes, len(notes)
        return out

    window = max((e["within_days"] for stem, e in meta.items() if e.get("kind") == kind
                  and (kind != "events" or _bound(e, kind, stem, suggested, record,
                                                 _read(path / (stem + ".md")) if suggested else None))), default=14)
    required = set()
    if not suggested:
        if kind == "findings":
            required = set(ctx.published)
        elif kind == "meetings":
            required = set(_eligible_meetings(ctx, window))
        elif kind == "events":
            required = set(_eligible_events(ctx, window))
        elif kind == "outcomes":
            required = set(_eligible_outcomes(ctx, window))
        else:
            required = {s for s in record if s in ctx.published}
    command = f"social --{kind}" + (f" --platform {platform}" if platform else "")
    for stem in sorted(stems | required):
        exists = stem in stems
        data = _read(path / (stem + ".md")) if exists else None
        entry_meta = _bound(meta.get(stem), kind, stem, suggested, record, data)
        reasons, subject, fresh = [], None, None
        if kind in ("findings", "threads"):
            subject = ctx.published.get(stem)
            if subject is None:
                reasons = ["finding unpublished (withdrawn, removed or slug changed)"]
            elif kind == "findings":
                try:
                    fresh = S._draft(subject, ctx.base_url, platform).encode("utf-8")
                except ValueError:
                    fresh = None
            else:
                rendered = ctx.thread(subject, platform)
                fresh = T._draft(subject["id"], rendered[0]).encode("utf-8") if rendered else None
        elif kind == "events":
            subject = ctx.events().get(stem)
            reasons = _stale_event(ctx, stem, entry_meta)
            if subject is not None and not reasons:
                try:
                    fresh = S._event_draft(subject, ctx.base_url, platform).encode("utf-8")
                except ValueError:
                    fresh = None
        else:
            subject = ctx.meetings()[0].get(stem)
            reasons = (_stale_meeting if kind == "meetings" else _stale_outcome)(ctx, stem, entry_meta)
            if subject is not None and not reasons:
                try:
                    fresh = (S._meeting_draft if kind == "meetings" else S._outcome_draft)(subject, ctx.base_url, platform).encode("utf-8")
                except (ValueError, KeyError):
                    fresh = None
        if reasons:
            if exists:
                out["stale"].append(stem)
                out["stale_reasons"][stem] = reasons
            continue
        problems = []
        if not exists:
            state = "missing"
            problems = ["missing"]
        elif data is None:
            state, problems = "not_regular", ["not_regular"]
        elif fresh is not None and data == fresh:
            state = "ok"
        elif suggested or S._sha(data) == record.get(stem):
            state, problems = "outdated", ["outdated"]
        else:
            state = "edited"
            if kind == "findings":
                problems = _finding_problems(subject, data, ctx, platform)
            elif kind == "meetings":
                problems = _meeting_problems(subject, data, ctx, platform)
            elif kind == "events":
                problems = _event_problems(subject, data, ctx, platform)
            elif kind == "outcomes":
                problems = _outcome_problems(subject, data, ctx, platform)
            else:
                problems = _thread_problems(subject, data, ctx, platform, ctx.thread(subject, platform))
        entry = {"slug": stem, "state": state, "problems": problems}
        if kind in ("meetings", "outcomes", "events") and entry_meta is None and state not in ("ok", "missing", "not_regular"):
            entry["note"] = f"original {'starts_at/window' if kind == 'events' else 'starts_at' if kind == 'meetings' else 'window'} cannot be checked; rebuild with {command}"
        out["drafts"].append(entry)
    out["stale"].sort()
    if notes:
        out["folder_problems"] = notes
    out["problems"] = sum(len(e["problems"]) for e in out["drafts"]) + len(out["stale"]) + len(notes)
    return out


def check_folders(root, findings, base_url, now=None):
    """Return ``{rel_folder: report}`` for every present roster folder (empty when there are none)."""
    ctx = _Context(root, findings, base_url, now)
    folders, bad = {}, set()
    for parts, kind, platform, suggested, parent in _roster():
        rel = "/".join(parts)
        if parent is not None and parent in bad:
            continue
        state = _dir_state(root.joinpath(*parts))
        if state == "absent":
            continue
        if state == "unsafe":
            bad.add(parts)
            folders[rel] = {"drafts": [], "stale": [], "stale_reasons": {}, "problems": 1,
                            "folder_problems": ["not a regular folder (link or other file); not entered"]}
            continue
        if parent is not None and _dir_state(root.joinpath(*parent)) != "dir":
            continue
        result = _folder(root, parts, kind, platform, suggested, ctx)
        if result is not None:
            folders[rel] = result
    return folders


def format_text(report):
    """Plain-text report grouped by folder; ``report`` is ``check_social``'s dict."""
    lines = [f"Checked {report['findings']} published finding(s)."]

    def group(heading, block):
        lines.append(heading + ":")
        for note in block.get("folder_problems", []):
            lines.append(f"  folder: {note}")
        for e in block["drafts"]:
            lines.append(f"  {e['slug']}: {e['state']}" + (f" ({', '.join(e['problems'])})" if e["problems"] else "")
                         + (f" - {e['note']}" if e.get("note") else ""))
        for stem in block["stale"]:
            reasons = block.get("stale_reasons", {}).get(stem)
            lines.append(f"  stale {stem}" + (": " + "; ".join(reasons) if reasons else ""))

    group("/".join(("kit", "social")), {"drafts": report["drafts"], "stale": report["stale"]})
    for rel, block in report.get("folders", {}).items():
        group(rel, block)
    lines.append(f"{report['problems']} problem(s).")
    lines.append("Read-only. Nothing was posted.")
    return "\n".join(lines) + "\n"
