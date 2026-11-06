"""Shadow advice from the local H2O model beside the records rules.

The model answers typed questions about each received item. Answers and the
labels they would suggest are recorded in a private per-run file next to the
rule result, with an agreement measurement. Nothing here changes a rule kind,
draft, hold or ledger row, sends anything or controls the service. The client
never raises: off, refused and unavailable all leave the rules in charge.
"""
import argparse
import json
import math
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import h2o_questions as hq

ADVICE_SCHEMA = "records-model-advice-v1"
AGREEMENT_SCHEMA = "records-model-advice-agreement-v1"
CONFIG_NAME = "model-advice.json"
DEFAULT_ENDPOINT = "http://127.0.0.1:8091/v1/systemone"
LOOPBACK = ("127.0.0.1", "::1", "localhost")
THRESHOLD = 0.90
FLOORS = (0.801, 0.199)  # the service's yes/no commit floor: an unsure band, not a confident answer
PRIVACY_FLAG = 0.50
CHUNK_CHARS = 32000  # about 8k tokens
MAX_CHUNKS = 4
RUN_BUDGET_SECONDS = 120
MAX_FILE = 64 * 1024 * 1024
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
RULE_KINDS = ("response", "extension", "denial", "fee", "records", "other")
MODEL_TO_RULE = {"fee_notice": "fee", "acknowledgement": "other", "no_records": "other", "clarification": "other"}


def _post(url, body, timeout):
    """Default transport: POST (or GET when body is None) and return the response bytes."""
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback http only, checked
        return response.read(MAX_FILE)


def loopback_endpoint(endpoint):
    """True only for plain http on 127.0.0.1, ::1 or localhost with no credentials."""
    try:
        parts = urlsplit(endpoint)
        return (parts.scheme == "http" and parts.hostname in LOOPBACK and parts.username is None
                and parts.password is None and (parts.port is None or 0 < parts.port < 65536))
    except (ValueError, TypeError, AttributeError):
        return False


def load_config(project):
    """-> {"enabled", "endpoint", "timeout_seconds", "error"}. Absent file or enabled:false is off."""
    off = {"enabled": False, "endpoint": DEFAULT_ENDPOINT, "timeout_seconds": 10, "error": None}
    path = Path(project) / CONFIG_NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return off
    except OSError:
        return {**off, "enabled": True, "error": "config_unreadable"}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
            return {**off, "enabled": True, "error": "config_not_private"}
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(65537)
    finally:
        os.close(fd)
    try:
        doc = json.loads(raw)
    except ValueError:
        return {**off, "enabled": True, "error": "config_invalid"}
    if not isinstance(doc, dict) or type(doc.get("enabled", False)) is not bool:
        return {**off, "enabled": True, "error": "config_invalid"}
    if not doc.get("enabled", False):
        return off
    endpoint = doc.get("endpoint", DEFAULT_ENDPOINT)
    seconds = doc.get("timeout_seconds", 10)
    if not isinstance(endpoint, str) or type(seconds) not in (int, float) or not 1 <= seconds <= 30:
        return {**off, "enabled": True, "error": "config_invalid"}
    return {"enabled": True, "endpoint": endpoint, "timeout_seconds": seconds, "error": None}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and 0.0 <= value <= 1.0


def _normalise(key, raw):
    """One validated answer in the wire shape, or None."""
    spec = hq.QUESTIONS[key]
    if not isinstance(raw, dict):
        return None
    if spec["type"] == "choice":
        probs = raw.get("probabilities")
        if not isinstance(probs, dict) or set(probs) != set(spec["criteria"]) or raw.get("choice") not in probs \
                or not all(_finite(v) for v in probs.values()):
            return None
        return {"type": "choice", "choice": raw["choice"], "probabilities": {k: float(v) for k, v in probs.items()}}
    if spec["type"] == "noul":
        return {"type": "noul", "noul": float(raw["noul"])} if _finite(raw.get("noul")) else None
    probs = raw.get("probabilities")
    levels = {str(i) for i in range(len(spec["criteria"]))}
    score = raw.get("score")
    if not isinstance(probs, dict) or set(probs) != levels or not all(_finite(v) for v in probs.values()) \
            or type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= len(levels) - 1:
        return None
    return {"type": "score", "score": float(score), "probabilities": {k: float(v) for k, v in probs.items()}}


def _call(url, keys, text, timeout, transport):
    """-> (answers, None) or (None, code)."""
    body = json.dumps({"state": text or "(empty)",
                       "questions": {k: hq.QUESTIONS[k] for k in keys}}).encode()
    try:
        raw = transport(url, body, timeout)
    except urllib.error.HTTPError as error:
        return None, f"http_{error.code}"
    except TimeoutError:
        return None, "timeout"
    except Exception:  # noqa: BLE001 - never raises; refused/refusing service all read as "connection"
        return None, "connection"
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return None, "bad_json"
    answers = doc.get("answers") if isinstance(doc, dict) else None
    if not isinstance(answers, dict):
        return None, "schema"
    out = {}
    for key in keys:
        answer = _normalise(key, answers.get(key))
        if answer is None:
            return None, "schema"
        out[key] = answer
    return out, None


def _chunks(text):
    text = text if isinstance(text, str) else ""
    pieces = [text[i:i + CHUNK_CHARS] for i in range(0, max(len(text), 1), CHUNK_CHARS)]
    return pieces[:MAX_CHUNKS]


def _top(answer):
    return max(answer["probabilities"].values())


def ask(text, *, config, transport=None):
    """Ask the six typed questions about one text. Never raises."""
    try:
        if not isinstance(config, dict) or not config.get("enabled"):
            return {"status": "off", "decisions": None}
        if config.get("error"):
            return {"status": "refused", "code": config["error"], "decisions": None}
        endpoint = config.get("endpoint")
        if not loopback_endpoint(endpoint):
            return {"status": "refused", "code": "endpoint_not_loopback", "decisions": None}
        transport = transport or _post
        timeout = config.get("timeout_seconds", 10)
        pieces = _chunks(text)
        first, code = _call(endpoint, list(hq.QUESTIONS), pieces[0], timeout, transport)
        if code:
            return {"status": "unavailable", "code": code, "decisions": None}
        answers = dict(first)
        for piece in pieces[1:]:
            more, code = _call(endpoint, list(hq.CHUNKED), piece, timeout, transport)
            if code:
                return {"status": "unavailable", "code": code, "decisions": None}
            # Most severe: the non-other type with the highest probability; the highest urgency.
            current = answers["doc_type"]
            candidate = more["doc_type"]
            if candidate["choice"] != "other" and (current["choice"] == "other"
                                                    or _top(candidate) > _top(current)):
                answers["doc_type"] = candidate
            if more["urgency"]["score"] > answers["urgency"]["score"]:
                answers["urgency"] = more["urgency"]
        return {"status": "ok", "version": hq.VERSION, "answers": answers}
    except Exception:  # noqa: BLE001 - the client contract is that it never raises
        return {"status": "unavailable", "code": "schema", "decisions": None}


def _floored(p):
    return any(abs(p - f) < 1e-9 for f in FLOORS)


def decide(answers, rule_kind=None):
    """Pure. What the answers would suggest; nothing is done with it here."""
    typed = {}
    for key, answer in answers.items():
        if answer["type"] == "choice":
            value, p = answer["choice"], answer["probabilities"][answer["choice"]]
            binary = len(answer["probabilities"]) == 2
            confident = p >= THRESHOLD and not (binary and _floored(p))
        elif answer["type"] == "noul":
            p = answer["noul"]
            value = p >= PRIVACY_FLAG or abs(p - 0.801) < 1e-9
            confident = (p >= THRESHOLD or p <= 1 - THRESHOLD) and not _floored(p)
        else:
            p, value = max(answer["probabilities"].values()), answer["score"]
            confident = p >= THRESHOLD
        typed[key] = {"value": value, "p": p, "confident": confident}
    doc, deadline, whole, follow = (typed[k] for k in ("doc_type", "deadline_stated", "completeness",
                                                       "followup_needed"))
    model_kind = MODEL_TO_RULE.get(doc["value"], doc["value"]) if doc["confident"] else None
    return {
        "typed": typed,
        "model_kind": model_kind,
        "suggested_kind": doc["value"] if doc["confident"] and rule_kind == "other" and doc["value"] != "other"
        else None,
        "kind_disagreement": bool(model_kind is not None and rule_kind is not None and model_kind != rule_kind),
        "possible_deadline": bool(deadline["value"] == "yes" and deadline["confident"] and doc["value"] != "other"),
        "possible_withholding": bool(whole["confident"] and whole["value"] in ("partial_withheld", "fully_withheld")),
        "followup_suggested": bool(follow["value"] == "yes" and follow["confident"]),
        "privacy_flag": bool(typed["privacy_present"]["value"]),
        "urgency_rank": typed["urgency"]["value"],
        "unsure": sorted(k for k, v in typed.items() if not v["confident"] and k not in ("privacy_present", "urgency")),
    }


def _write(directory, name, doc):
    data = (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary = directory / f".{name}.tmp"
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / name)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            raise OSError("MODEL_ADVICE_INPUT")
        return json.loads(stream.read())


def item_texts(project, items):
    """{original_sha256: text} from the ledger's current extraction, the same accessor the rules used."""
    import sqlite3
    from .outcomes import received_items
    from .run_stages import snapshot_ro
    project = Path(project)
    out = {}
    with snapshot_ro(project / "processing-v1" / "ledger.sqlite") as db:
        db.row_factory = sqlite3.Row
        for item in items:
            units, _, _ = received_items._current_units(db, project / "processing-v1", item["original_sha256"])
            out[item["original_sha256"]] = "\n\n".join(u["text"] for u in units)
    return out


def _drafts(project):
    try:
        from .approval_queue import load_queue
        return {row["item_id"] for row in load_queue(project)}
    except Exception:  # noqa: BLE001 - the draft comparison is optional context
        return None


def _agreement(run_id, records, drafts):
    matrix, compared, agreed = {}, 0, 0
    follow = {"suggested": 0, "suggested_with_draft": 0, "suggested_without_draft": 0, "draft_not_suggested": 0}
    privacy = deadlines = unavailable = 0
    for record in records:
        if record["status"] != "ok":
            unavailable += 1
            continue
        d = record["decisions"]
        if d["model_kind"] is not None:
            raw = record["answers"]["doc_type"]["choice"]
            matrix.setdefault(record["rule_kind"], {}).setdefault(raw, 0)
            matrix[record["rule_kind"]][raw] += 1
            compared += 1
            agreed += d["model_kind"] == record["rule_kind"]
        privacy += d["privacy_flag"]
        deadlines += d["possible_deadline"]
        if drafts is not None:
            has = record["item_id"] in drafts
            if d["followup_suggested"]:
                follow["suggested"] += 1
                follow["suggested_with_draft" if has else "suggested_without_draft"] += 1
            elif has:
                follow["draft_not_suggested"] += 1
    return {"schema": AGREEMENT_SCHEMA, "run_id": run_id, "version": hq.VERSION, "items": len(records),
            "unavailable": unavailable, "kind_matrix": matrix, "kind_compared": compared,
            "kind_agreement_rate": (agreed / compared) if compared else None, "privacy_flags": privacy,
            "possible_deadlines": deadlines, "followup_vs_draft": follow if drafts is not None else None,
            "shadow_only": True, "review_required": True}


def write_run_model_advice(project, run_id, *, transport=None, clock=time.monotonic, texts=None):
    """Advice for this run's received items. Returns {"status": ...}; writes nothing when off or refused."""
    project = Path(project)
    config = load_config(project)
    if not config["enabled"]:
        return {"status": "off"}
    if config["error"] or not loopback_endpoint(config["endpoint"]):
        return {"status": "refused", "code": config["error"] or "endpoint_not_loopback"}
    if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
        raise OSError("MODEL_ADVICE_RUN_ID")
    outcomes = project / "run-outputs" / run_id / "outcomes"
    doc = _read_json(outcomes / "received-items.json")
    if not isinstance(doc, dict) or doc.get("run_id") != run_id or not isinstance(doc.get("items"), list):
        raise OSError("MODEL_ADVICE_INPUT")
    items = doc["items"]
    started = clock()
    texts = texts if texts is not None else item_texts
    by_original = texts(project, items) if callable(texts) else texts
    records = []
    for item in items:
        record = {"item_id": item["item_id"], "rule_kind": item.get("kind"), "status": "unavailable",
                  "code": None, "answers": None, "decisions": None}
        if clock() - started >= RUN_BUDGET_SECONDS:
            record["code"] = "budget"
        elif not by_original.get(item.get("original_sha256")):
            record["code"] = "no_text"
        else:
            result = ask(by_original[item["original_sha256"]], config=config, transport=transport)
            record["status"], record["code"] = result["status"], result.get("code")
            if result["status"] == "ok":
                record["answers"] = result["answers"]
                record["decisions"] = decide(result["answers"], record["rule_kind"])
        if record["status"] == "ok":
            del record["code"]
        records.append(record)
    advice = {"schema": ADVICE_SCHEMA, "run_id": run_id, "version": hq.VERSION, "shadow_only": True,
              "review_required": True, "items": records}
    _write(outcomes, "model-advice.json", advice)
    _write(outcomes, "model-advice-agreement.json", _agreement(run_id, records, _drafts(project)))
    return {"status": "ok", "items": len(records)}


def health(project, *, transport=None):
    config = load_config(project)
    if not config["enabled"]:
        return {"status": "off"}
    if config["error"] or not loopback_endpoint(config["endpoint"]):
        return {"status": "refused", "code": config["error"] or "endpoint_not_loopback"}
    parts = urlsplit(config["endpoint"])
    url = f"{parts.scheme}://{parts.netloc}/health"
    try:
        (transport or _post)(url, None, config["timeout_seconds"])
    except urllib.error.HTTPError as error:
        return {"status": "unavailable", "code": f"http_{error.code}"}
    except TimeoutError:
        return {"status": "unavailable", "code": "timeout"}
    except Exception:  # noqa: BLE001
        return {"status": "unavailable", "code": "connection"}
    return {"status": "ok"}


TRANSPORT = None  # test seam: the CLI uses this transport when set


def main():
    parser = argparse.ArgumentParser(prog="records model-advice", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run", help="record advice for one run's received items")
    run.add_argument("--project", required=True)
    run.add_argument("--run-id", required=True)
    check = sub.add_parser("health", help="GET /health through the loopback check")
    check.add_argument("--project", required=True)
    args = parser.parse_args()
    if args.action == "health":
        out = health(args.project, transport=TRANSPORT)
        print(out["status"] + (f" {out['code']}" if out.get("code") else ""))
        return 0 if out["status"] == "ok" else 1
    try:
        out = write_run_model_advice(args.project, args.run_id, transport=TRANSPORT)
    except (OSError, ValueError, KeyError) as error:
        print(f"unavailable {error}" if isinstance(error, OSError) and str(error).startswith("MODEL_ADVICE")
              else "unavailable")
        return 1
    print(out["status"] + (f" {out['code']}" if out.get("code") else ""))
    return 0 if out["status"] in ("ok", "off") else 1


if __name__ == "__main__":
    sys.exit(main())
