"""Pure T4B resolution plan (docs/RECORDS-LEGACY-MAIL-RESOLUTION.md section 8.2).

One function shared by the adopt-mail dry run and apply. It reads no file and no
ledger: the caller passes plain facts (receipt digests, shas, occurrence ids and
per-original ledger state) and the loaded policy. The result depends only on
those values, never on discovery order or file names. It resolves nothing; the
canonical mail backend does that inside its own transaction.
"""
from collections import Counter

RESOLVED, OPEN, BLOCKED = "resolved", "open", "blocked"
DRIFT_RECEIPT_SET = "resolution_policy_receipt_set_mismatch"
DRIFT_NOT_IN_POLICY = "resolution_not_in_policy"
DRIFT_HELD_RESOLVED = "resolution_held_original_resolved"
DRIFT_CYCLE = "resolution_dependency_cycle"
BINDING_BLOCKED = "resolution_binding_receipt_blocked"
DEPENDENCY_BLOCKED = "resolution_dependency_blocked"
EXISTING_MISMATCH = "resolution_existing_binding_mismatch"
RECEIPT_MISMATCH = "resolution_policy_receipt_mismatch"


def _components(nodes, edges):
    """Strongly connected components with more than one node (Tarjan, iterative)."""
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            node, children = work[-1]
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter[0]
                    counter[0] += 1
                    stack.append(child)
                    on.add(child)
                    work.append((child, iter(sorted(edges.get(child, ())))))
                    break
                if child in on:
                    low[node] = min(low[node], index[child])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    part = []
                    while True:
                        member = stack.pop()
                        on.discard(member)
                        part.append(member)
                        if member == node:
                            break
                    if len(part) > 1:
                        out.append(sorted(part))
    return out


def plan(policy, receipts, facts):
    """Compute the resolution plan.

    ``receipts``: {digest: {"state", "gap", "foreign_gap", "originals": {sha: [occurrence ids]},
    "attachments"}}. ``state`` is the adopt-mail state (``new``, ``incomplete``, ``complete`` or an
    ``identity_*`` code); ``gap`` the MIME precheck code or None; ``foreign_gap`` today's binding
    code for originals that are not open policy originals, or None.
    ``facts``: {sha: {"scope", "resolution", "check", "format"}} for every sha in a receipt or the
    policy. ``resolution`` is None or {"schema", "receipt", "occurrence", "legacy_binding"};
    ``check`` is the safe code of eligibility checks 1-3 for an unresolved original, or None.
    """
    entries = {sha: policy.entry(sha) for sha in policy.originals()}
    held = set(policy.held())
    containing = {}
    for digest, receipt in receipts.items():
        for sha in receipt["originals"]:
            containing.setdefault(sha, set()).add(digest)

    state, blocked_codes = {}, {}
    for sha, entry in entries.items():
        fact = facts.get(sha) or {}
        row = fact.get("resolution")
        if row is not None:
            same = (row["schema"] == "original_resolution_mail_export_v1"
                    and (row["receipt"], row["occurrence"], row["legacy_binding"]) ==
                    (entry["verification_receipt_sha256"], entry["binding_occurrence_id"],
                     entry["legacy_binding_sha256"]))
            state[sha] = RESOLVED if same else BLOCKED
            if not same:
                blocked_codes[sha] = EXISTING_MISMATCH
        elif fact.get("check"):
            state[sha], blocked_codes[sha] = BLOCKED, fact["check"]
        else:
            state[sha] = OPEN

    drift, receipt_drift = Counter(), {}

    def note(code, digests):
        drift[code] += 1
        for digest in digests:
            if receipts[digest]["state"] != "complete":
                receipt_drift.setdefault(digest, code)

    for sha, entry in sorted(entries.items()):
        expected = {entry["verification_receipt_sha256"], *entry["dependent_receipts"]}
        if containing.get(sha, set()) != expected:
            note(DRIFT_RECEIPT_SET, containing.get(sha, set()) | (expected & set(receipts)))
    for sha in sorted(containing):
        fact = facts.get(sha) or {}
        covered = sha in entries or sha in held
        listed = fact.get("resolution") is not None or fact.get("scope") == "unresolved"
        if listed and not covered:
            note(DRIFT_NOT_IN_POLICY, containing[sha])
    for sha in sorted(held):
        if (facts.get(sha) or {}).get("resolution") is not None:
            note(DRIFT_HELD_RESOLVED, containing.get(sha, ()))
    graph = {}
    for sha, entry in entries.items():
        for dependent in entry["dependent_receipts"]:
            graph.setdefault(entry["verification_receipt_sha256"], set()).add(dependent)
    nodes = set(graph) | {d for targets in graph.values() for d in targets}
    for cycle in _components(nodes, graph):
        note(DRIFT_CYCLE, [d for d in cycle if d in receipts])

    def new(digest):
        return digest in receipts and receipts[digest]["state"] == "new"

    blocked_receipts = {}
    for sha, entry in sorted(entries.items()):
        if state[sha] == BLOCKED and new(entry["verification_receipt_sha256"]):
            blocked_receipts.setdefault(entry["verification_receipt_sha256"], BINDING_BLOCKED)
    changed = True
    while changed:
        changed = False
        for sha in sorted(entries):
            if state[sha] != OPEN:
                continue
            entry = entries[sha]
            binding = entry["verification_receipt_sha256"]
            code = None
            if not new(binding) or receipts[binding]["gap"] or receipts[binding]["foreign_gap"] \
                    or binding in receipt_drift or binding in blocked_receipts:
                code = BINDING_BLOCKED
            elif entry["binding_occurrence_id"] not in receipts[binding]["originals"].get(sha, ()):
                code = RECEIPT_MISMATCH
            elif (facts[sha].get("format") != "eml") and entry["binding_occurrence_id"] == _mail_id(receipts[binding]):
                code = BINDING_BLOCKED
            else:
                for other in sorted(receipts[binding]["originals"]):
                    if other == sha or other not in entries:
                        continue
                    peer = entries[other]
                    related = (peer["verification_receipt_sha256"] == binding
                               or binding in peer["dependent_receipts"])
                    if state[other] == BLOCKED or (state[other] == OPEN and not related):
                        code = BINDING_BLOCKED
                        break
            if code:
                state[sha], blocked_codes[sha] = BLOCKED, code
                if new(binding):
                    blocked_receipts.setdefault(binding, code)
                changed = True
        for digest in sorted(receipts):
            if not new(digest) or digest in blocked_receipts:
                continue
            for sha in sorted(receipts[digest]["originals"]):
                entry = entries.get(sha)
                if (entry is not None and digest in entry["dependent_receipts"]
                        and state[sha] == BLOCKED):
                    blocked_receipts[digest] = DEPENDENCY_BLOCKED
                    changed = True
                    break

    # Edges run from a binding receipt to each dependent of one of its open originals.
    edges = {}
    for sha, entry in entries.items():
        if state[sha] == OPEN:
            for dependent in entry["dependent_receipts"]:
                if new(dependent):
                    edges.setdefault(entry["verification_receipt_sha256"], set()).add(dependent)

    codes, resolvable, binding_receipts = {}, set(), set()
    for digest, receipt in receipts.items():
        if receipt["state"] != "new":
            continue
        mine = {sha for sha in receipt["originals"] if sha in entries and state[sha] == OPEN}
        code = receipt["gap"] or receipt_drift.get(digest) or blocked_receipts.get(digest)
        if code is None and not receipt["foreign_gap"] and mine:
            resolvable.add(digest)
            if any(entries[sha]["verification_receipt_sha256"] == digest for sha in mine):
                binding_receipts.add(digest)
        elif code is None:
            code = receipt["foreign_gap"]
        if code:
            codes[digest] = code
    order, remaining = [], {d: set() for d in resolvable}
    for source, targets in edges.items():
        for target in targets:
            if target in remaining and source in remaining:
                remaining[target].add(source)
    while remaining:
        ready = sorted(d for d, before in remaining.items() if not before)
        if not ready:  # only a hand-edited cycle; already counted as drift
            break
        order.extend(ready)
        for digest in ready:
            del remaining[digest]
        for before in remaining.values():
            before.difference_update(ready)

    counts = Counter(state.values())
    blockers = bool(drift) or counts[BLOCKED] > 0
    return {"state": state, "blocked_codes": blocked_codes, "drift": dict(sorted(drift.items())),
            "codes": codes, "resolvable": order, "binding": binding_receipts, "edges": edges,
            "held_present": sum(1 for sha in held if sha in containing),
            "counts": {"policy_originals": len(entries), "resolved": counts[RESOLVED],
                       "resolvable": counts[OPEN], "blocked": counts[BLOCKED]},
            "plan_state": "blocked" if blockers else "ok"}


def _mail_id(receipt):
    return receipt.get("mail_id")


def dependents_of(edges, failed):
    """Every receipt that depends, directly or transitively, on a failed receipt."""
    seen, stack = set(), list(failed)
    while stack:
        for target in edges.get(stack.pop(), ()):
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return seen
