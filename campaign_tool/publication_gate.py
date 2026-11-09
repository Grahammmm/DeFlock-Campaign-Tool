"""Publication gate: one check every publishing path calls before it emits.

It answers one question: does this exact content have a recorded approval by a named person? It reads ids, hashes and
enums only (never artefact text), has no writer, and cannot approve anything. A clean gate is not a claim that the
artefact is fit to publish: redaction sign-off and privacy lint are separate conditions (docs/PUBLICATION.md).
"""
import hashlib
import json
import re
import unicodedata

REASONS = ("no_approval", "approver_unnamed", "content_changed_after_approval", "approval_for_other_content",
           "approval_consumed", "redaction_unsigned", "status_not_approved", "approval_unbound")
PLACEHOLDERS = frozenset(("-", "n/a", "na", "none", "null", "tbd", "todo", "unknown", "anonymous", "auto", "system",
                          "agent", "bot", "ai", "llm", "model", "robot", "script", "cron", "automation", "automatic"))
MACHINE_WORDS = frozenset(("auto", "automatic", "automation", "system", "agent", "bot", "ai", "llm", "coder", "reviewer",
                           "worker", "model", "script", "cron", "robot"))
MODEL_PREFIXES = ("claude", "chatgpt", "gpt", "codex", "opus", "sonnet", "haiku", "hermes", "qwen", "gemini", "grok",
                  "fable", "astra", "llama", "mistral")  # model families: qwen3, gpt-5-codex, claude-opus-5-5
LANE = re.compile(r"(?:^|[^a-z])(?:sol|astra)[-_]")  # lane names such as sol-reviewer
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class PublicationRefused(ValueError):
    """Carries the artefact id and one named reason; legal flags ride along but are never the cause."""

    def __init__(self, artefact_id, reason, flags=()):
        assert reason in REASONS, reason
        self.artefact_id = artefact_id if isinstance(artefact_id, str) and _ID.fullmatch(artefact_id) else "#?"
        self.reason = reason
        self.flags = tuple(f for f in flags if isinstance(f, str) and _ID.fullmatch(f))
        self.reasons = [reason] + ["flag:" + f for f in self.flags]
        super().__init__("publication refused: artefact %s: %s" % (self.artefact_id, ", ".join(self.reasons)))


def named(who):
    """True only for a string that reads as a person: not empty, a placeholder, a model or a lane name."""
    if not isinstance(who, str):
        return False
    text = "".join(c for c in unicodedata.normalize("NFKC", who) if unicodedata.category(c) != "Cf").casefold().strip()
    words = re.findall(r"[a-z0-9]+", text)
    # Keep ASCII denial tokens visible even beside non-Latin letters (e.g. 李system).
    has_name = bool(words) or any(unicodedata.category(c).startswith("L") for c in text)
    return (has_name and text not in PLACEHOLDERS and not LANE.search(text) and not MACHINE_WORDS.intersection(words)
            and not any(w.startswith(MODEL_PREFIXES) for w in words))


def _defect(approval):
    """The first reason one content-matching approval does not count, or None when it does."""
    if approval.get("status") != "approved":
        return "status_not_approved"
    if not named(approval.get("approved_by")):
        return "approver_unnamed"
    if approval.get("consumed"):
        return "approval_consumed"
    if approval.get("redaction_signed", True) is not True:  # opt-in field: a path that has a sign-off sets it
        return "redaction_unsigned"
    return None


def assert_publishable(artefact_id, content_hash, approvals, flags=()):
    """Return None, or raise PublicationRefused with one named reason. Reads the fields only, never prose.

    approvals: records {artefact_id, content_hash, approved_by, status, ...} that the human review surface wrote.
    flags: alert-only notes (for example a legal-claim flag): recorded on a refusal, never its cause.
    """
    def refuse(reason):
        raise PublicationRefused(artefact_id, reason, flags)

    rows = [a for a in approvals if isinstance(a, dict)] if isinstance(approvals, (list, tuple)) else []
    if not rows:
        refuse("no_approval")
    mine = [a for a in rows if a.get("artefact_id") == artefact_id]
    if not mine:
        refuse("approval_for_other_content")
    same = [a for a in mine if isinstance(content_hash, str) and content_hash and a.get("content_hash") == content_hash]
    if not same:
        refuse("content_changed_after_approval")
    defects = [_defect(a) for a in same]
    if None not in defects:
        refuse(defects[0])


def content_digest(obj):
    """Same sha256 as campaign_tool.review.content_hash, kept here so the gate stays dependency-free."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                                     allow_nan=False).encode()).hexdigest()


def entry_refusals(entries, fresh):
    """PUBLISH.json entries {finding_id, review, ...}: one line per approved entry the gate refuses (ids and enums only).

    DF-APPROVAL-TIME-BIND: the approval is bound to review.content_hash, the hash of the entry (minus `review`) that the
    person recorded when approving. The content is the entry the run re-derives now, and the file's own entry must still
    equal it. A hash is never synthesized here: an approval with none recorded (a legacy approval) is approval_unbound
    and approves nothing until a person re-reads the text and records its hash (skills/flock-publish)."""
    body = lambda f: content_digest({k: v for k, v in f.items() if k != "review"})  # noqa: E731
    now = {f.get("finding_id"): f for f in (fresh or {}).get("findings", [])}
    lines = []
    for entry in (e for e in entries if e["review"]["status"] == "approved"):
        fid, recorded = entry["finding_id"], entry["review"].get("content_hash")
        try:
            if not (isinstance(recorded, str) and recorded):
                raise PublicationRefused(fid, "approval_unbound")
            current = body(now[fid]) if fid in now and body(now[fid]) == body(entry) else None
            assert_publishable(fid, current, [dict(entry["review"], artefact_id=fid, content_hash=recorded)])
        except PublicationRefused as exc:
            lines.append(str(exc))
    return lines
