"""Pure, snapshot-only section 8.2 planner. No acquisition or ledger writer.

Receipt records and MIME results come from the S3a census vocabulary. Internal
sets/ids never enter the public report; only counts, safe codes and digests do.
"""
from collections import Counter
import heapq
import json

from .ledger import original_resolution as resolution, original_resolution_mail as mail


def baseline(con):
    pairs = [tuple(row) for row in con.execute(
        "SELECT original_sha256,binding_sha256 FROM original_resolutions ORDER BY original_sha256"
    )] if resolution.installed(con) else []
    return {"rows": len(pairs), "sha256": resolution.digest(pairs)}


def _cycles(graph):
    """Iterative SCCs: bounded policies must not depend on Python recursion depth."""
    seen, order = set(), []
    for start in sorted(graph):
        if start in seen:
            continue
        stack = [(start, False)]
        while stack:
            node, finish = stack.pop()
            if finish:
                order.append(node)
            elif node not in seen:
                seen.add(node)
                stack.append((node, True))
                stack.extend((other, False) for other in sorted(graph[node], reverse=True) if other not in seen)
    reverse = {node: set() for node in graph}
    for node, targets in graph.items():
        for target in targets:
            reverse[target].add(node)
    seen = set()
    for start in reversed(order):
        if start in seen:
            continue
        component, stack = set(), [start]
        while stack:
            node = stack.pop()
            if node not in seen:
                seen.add(node)
                component.add(node)
                stack.extend(reverse[node] - seen)
        if len(component) > 1 or start in graph[start]:
            yield component


def plan(con, receipts, records, mime, context):
    """Return classification/order/dependencies and disjoint original states.

    Does not mutate receipts. ``records[digest]`` uses receipt_occurrences tuples.
    All reads use the caller's immutable snapshot and independently pinned context.
    """
    from .mail_adoption import binding_conflict
    policy = json.loads(context.policy.raw)
    entries, held = policy["originals"], set(policy["held"])
    by_digest = {r["receipt_sha256"]: r for r in receipts}
    containing, grouped = {}, {}
    for digest, values in records.items():
        grouped[digest] = {}
        for record in values:
            grouped[digest].setdefault(record[1], []).append(record)
            containing.setdefault(record[1], set()).add(digest)
    installed = resolution.installed(con)
    subjects = set(containing) | set(entries) | held
    declarations, rows, payloads, effective = {}, {}, {}, {}
    for subject in sorted(subjects):
        row = con.execute("SELECT * FROM originals WHERE sha256=?", (subject,)).fetchone()
        declarations[subject] = dict(row) if row else None
        row = con.execute("SELECT * FROM original_resolutions WHERE original_sha256=?", (subject,)).fetchone() if installed else None
        rows[subject] = row
        payloads[subject] = json.loads(row["payload_json"]) if row else None
        # Always validate existing evidence, including v1, before interpreting it.
        effective[subject] = context.effective_original(con, subject)
    states, reasons = {}, {}
    for subject, entry in sorted(entries.items()):
        row, original = rows[subject], declarations[subject]
        if row:
            payload = payloads[subject]
            same = (payload["schema"] == mail.MAIL_SCHEMA
                    and row["capture_receipt_sha256"] == entry["verification_receipt_sha256"]
                    and row["capture_occurrence_id"] == entry["binding_occurrence_id"]
                    and payload["legacy_binding_sha256"] == entry["legacy_binding_sha256"]
                    and original["bytes"] == entry["bytes"])
            states[subject] = "resolved" if same else "blocked"
            if not same:
                reasons[subject] = "resolution_existing_binding_mismatch"
        else:
            try:
                if original is None or original["bytes"] != entry["bytes"]:
                    raise ValueError("original_resolution_requires_unaccepted_original")
                # Role/stage/import checks do not depend on receipt state.
                mail.eligible(con, subject, original, entry, "attachment")
                states[subject] = "open"
            except ValueError as error:
                states[subject] = "blocked"
                reasons[subject] = ("resolution_import_binding_mismatch" if "import_" in str(error)
                                    else "resolution_requires_unaccepted_original")
    drift, drift_receipts = Counter(), {}

    def finding(code, members):
        drift[code] += 1
        for digest in sorted(members):
            drift_receipts.setdefault(digest, code)

    for subject, entry in sorted(entries.items()):
        creating = (context.historical_policies or {}).get(subject)
        old_dependents = (set(creating[1].admission(subject, entry["bytes"])["dependent_receipts"])
                          if creating and states[subject] == "resolved" else set())
        if (containing.get(subject, set()) != {entry["verification_receipt_sha256"], *entry["dependent_receipts"]}
                or not old_dependents.issubset(entry["dependent_receipts"])):
            finding("resolution_policy_receipt_set_mismatch", containing.get(subject, set()))
    for subject in sorted(subjects):
        original, payload = declarations[subject], payloads[subject]
        inventoried = original and ((original["scope"] == "unresolved" and not rows[subject])
                                   or (payload and payload["schema"] == mail.MAIL_SCHEMA))
        if containing.get(subject) and inventoried and subject not in entries and subject not in held:
            finding("resolution_not_in_policy", containing[subject])
        if subject in held and rows[subject]:
            finding("resolution_held_original_resolved", containing.get(subject, set()))
    graph = {digest: set() for digest in by_digest}
    predecessors = {digest: set() for digest in by_digest}
    for subject, entry in entries.items():
        if states[subject] != "open":
            continue
        binding = entry["verification_receipt_sha256"]
        if binding not in graph:
            continue
        for dependent in entry["dependent_receipts"]:
            if dependent in graph and by_digest[dependent]["state"] == "new":
                graph[binding].add(dependent)
                predecessors[dependent].add(binding)
    for component in _cycles(graph):
        finding("resolution_dependency_cycle", component)
    gaps = {}
    for digest, receipt in by_digest.items():
        if receipt["state"] != "new":
            continue
        gap = None
        for subject, values in sorted(grouped[digest].items()):
            if subject in entries:
                if states[subject] == "blocked":
                    gap = reasons[subject]
                    break
                if states[subject] == "open":
                    if any(record[3] != entries[subject]["bytes"] for record in values):
                        gap = "original_bytes_conflict"
                        break
                    continue
            gap = binding_conflict(con, context, context.output_root / "blobs", subject, values[0][3])
            if gap:
                break
        gaps[digest] = mime[digest] or drift_receipts.get(digest) or gap
    # Validate the binding occurrence/kind, even when the receipt's first gap
    # would otherwise hide it. Missing/enrolled/MIME-held bindings block P.
    for subject, entry in sorted(entries.items()):
        if states[subject] != "open":
            continue
        binding = entry["verification_receipt_sha256"]
        receipt = by_digest.get(binding)
        code = None
        if receipt is None or receipt["state"] != "new" or gaps.get(binding):
            code = "resolution_binding_receipt_blocked"
        else:
            values = grouped[binding].get(subject, [])
            chosen = [r for r in values if r[0] == entry["binding_occurrence_id"]]
            if len(chosen) != 1 or chosen[0][3] != entry["bytes"]:
                code = "resolution_policy_receipt_mismatch"
            else:
                try:
                    mail.eligible(con, subject, declarations[subject], entry, chosen[0][2])
                except ValueError:
                    code = "resolution_policy_receipt_mismatch"
        if code:
            states[subject], reasons[subject] = "blocked", code
            if receipt and receipt["state"] == "new" and not mime[binding] and binding not in drift_receipts:
                gaps[binding] = code
    # Monotone fixed point: a receipt may depend on one binding and bind another.
    changed = True
    while changed:
        changed = False
        for subject, entry in sorted(entries.items()):
            binding = entry["verification_receipt_sha256"]
            if states[subject] == "open" and gaps.get(binding):
                states[subject], reasons[subject] = "blocked", "resolution_binding_receipt_blocked"
                if not mime[binding] and binding not in drift_receipts:
                    gaps[binding] = "resolution_binding_receipt_blocked"
                changed = True
            if states[subject] != "blocked":
                continue
            for dependent in entry["dependent_receipts"]:
                if dependent not in gaps or mime[dependent] or dependent in drift_receipts:
                    continue
                if not gaps[dependent] or gaps[dependent] == "original_scope_unresolved":
                    gaps[dependent] = "resolution_dependency_blocked"
                    changed = True
    resolvable, binding_messages = set(), set()
    for digest, receipt in by_digest.items():
        if receipt["state"] == "new" and not gaps[digest]:
            opened = {s for s in grouped[digest] if states.get(s) == "open"}
            if opened:
                resolvable.add(digest)
                if any(entries[s]["verification_receipt_sha256"] == digest for s in opened):
                    binding_messages.add(digest)
    indegree = {d: len(predecessors[d] & resolvable) for d in resolvable}
    ready = [d for d, n in indegree.items() if n == 0]
    heapq.heapify(ready)
    ordered = []
    while ready:
        digest = heapq.heappop(ready)
        ordered.append(digest)
        for other in sorted(graph[digest] & resolvable):
            indegree[other] -= 1
            if indegree[other] == 0:
                heapq.heappush(ready, other)
    counts = Counter(states.values())
    report = {"policy_originals": len(entries), "policy_originals_resolved": counts["resolved"],
        "resolvable_originals": counts["open"], "policy_originals_blocked": counts["blocked"],
        "held_originals": sum(bool(containing.get(s)) for s in held),
        "resolvable_messages": len(resolvable), "resolvable_binding_messages": len(binding_messages),
        "resolvable_dependent_messages": len(resolvable - binding_messages),
        "resolvable_attachments": sum(by_digest[d]["attachments"] for d in resolvable),
        "resolution_drift": dict(sorted(drift.items())),
        "resolution_plan": {"state": "blocked" if drift or counts["blocked"] else "ok"},
        "resolution_policy": {"state": "loaded", "sha256": context.policy.sha256},
        "resolution_baseline": baseline(con), "resolved_originals": 0}
    return report, gaps, ordered, predecessors, states
