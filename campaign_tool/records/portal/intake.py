"""Portal intake adapter: classify mail-delta notice links against the
administrator allowlist and either enroll or record a refusal.

The administrator allowlist (approval.json) is read-only here. This module
never edits it and never grants egress. Fetch remains a separate, explicit
step that independently re-reads the same allowlist, so intake classification
and fetch authorization cannot drift apart.

Fail-closed: if the allowlist cannot be loaded (missing, unsafe, or expired),
no link is enrolled. Every link whose host or path is outside the allowlist is
recorded as a refusal (visible for operator review) instead of being silently
dropped, but the signed URL itself is never retained.
"""
from datetime import datetime, timezone

from .notice import DOCUMENT_LINK, Links
from .policy import PortalError, url_parts
from .store import check_hash, identifier

MAX_BODY_BYTES = 2 * 1024 * 1024


def _extract_urls(body):
    parser = Links()
    parser.feed(body)
    # Decoded href attributes and text nodes are examined; never raw HTML
    # attributes (which would duplicate entity-escaped signed URLs).
    return dict.fromkeys(parser.urls)


def classify_link(queue, *, url, request_id, source_sha256, approval, now=None):
    """Classify one extracted portal link: enroll allowlisted, refuse otherwise.

    ``approval`` is an already-loaded :class:`policy.Approval`; ``now`` is the
    timezone-aware wall clock used for the approval-expiry check. Returns one of
    ``"enrolled"``, ``"refused"`` or ``"skipped"`` (a non-document URL).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        p = url_parts(url)
    except PortalError:
        return "skipped"  # Malformed/unrecognized URL data, not an operational failure.
    match = DOCUMENT_LINK.fullmatch(p.path)
    if not match:
        return "skipped"
    host = p.hostname
    try:
        approval.authorize(url, host, now)
    except PortalError as exc:
        queue.refuse(host, str(exc), url, source_sha256)
        return "refused"
    queue.inventory(host, request_id, match.group(1), url, source_sha256)
    return "enrolled"


def intake_notices(queue, *, notices, approval_loader, now=None):
    """Feed notices (each ``{body, request_id, source_sha256}``) into the queue.

    The allowlist is loaded once, so a missing or expired allowlist refuses the
    whole batch rather than dropping individual notices. Returns aggregate
    counts ``{"enrolled", "refused", "skipped"}`` across every notice.
    """
    approval = approval_loader()  # Fail closed: no allowlist, no enrollment.
    if now is None:
        now = datetime.now(timezone.utc)
    totals = {"enrolled": 0, "refused": 0, "skipped": 0}
    for notice in notices:
        if not isinstance(notice, dict) or set(notice) != {"body", "request_id", "source_sha256"}:
            raise PortalError("intake_invalid")
        body = notice["body"]
        if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise PortalError("notice_oversize")
        identifier(notice["request_id"])
        check_hash(notice["source_sha256"])
        for url in _extract_urls(body):
            outcome = classify_link(queue, url=url, request_id=notice["request_id"],
                                    source_sha256=notice["source_sha256"],
                                    approval=approval, now=now)
            totals[outcome] += 1
    return totals


def intake_notice(queue, *, body, request_id, source_sha256, approval_loader, now=None):
    """Feed one mail-delta notice into the portal queue, allowlist-gated."""
    return intake_notices(
        queue,
        notices=[{"body": body, "request_id": request_id, "source_sha256": source_sha256}],
        approval_loader=approval_loader,
        now=now,
    )
