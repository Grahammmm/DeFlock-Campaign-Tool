"""Explicit, opaque model views of private evidence.

Original locators and unrecognized metadata never enter these views. A model
may cite an issued unit reference (and a valid line within that unit), or the
fixed document sentinel. Restoration uses this local table, never model-supplied
locator fields. These views supplement text redaction; they do not make regex
redaction a complete identifier detector.
"""
from copy import deepcopy
import json
import math

from .detectors import POLICY_ELEMENTS
from .redact import Redactor
from .schema import CONFIDENCE


DOCUMENT_LOCATOR = {"scope": "document"}
NARRATIVE_FIELDS = ("scope", "actors", "dates", "statements", "omissions", "counterevidence", "conclusions")


class ModelInputError(ValueError):
    """A model view or citation cannot be constructed safely."""


def _fail():
    # Never include a locator, source text or rejected model value in errors.
    raise ModelInputError("model input or locator references failed validation")


def _key(locator):
    if not isinstance(locator, dict) or any(not isinstance(key, str) for key in locator):
        _fail()
    try:
        return json.dumps(locator, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        _fail()


def model_locator(locator, index):
    """Compatibility helper: every private locator becomes an opaque unit ID."""
    if type(index) is not int or index < 0:
        _fail()
    return {"unit": index}


class ModelSources:
    """One request's redactor, private locator snapshot and issued references.

    Call ``source_payload`` (or ``primary_payload``) before ``digest_payload``
    or ``restore_output``. Caller-supplied ``model_locator`` values are ignored.
    Neither serialization nor restoration mutates the supplied records.
    """

    def __init__(self, units, *, allowlist=(), denylist=()):
        self._redactor = Redactor(allowlist=allowlist, denylist=denylist)
        self._units = []
        self._by_locator = {}
        self._issued = set()
        for index, unit in enumerate(units):
            if not isinstance(unit, dict):
                _fail()
            locator = unit.get("locator")
            if locator is None:
                locator = {}
            key = _key(locator)
            text = unit.get("text")
            if text is None:
                text = ""
            if not isinstance(text, str):
                _fail()
            self._redactor.reserve(text)
            self._units.append({"locator": deepcopy(locator), "text": text})
            self._by_locator.setdefault(key, []).append(index)

    def _text(self, value):
        if not isinstance(value, str):
            _fail()
        return self._redactor.redact(value).text

    @staticmethod
    def _list(value):
        if not isinstance(value, list):
            _fail()
        return value

    def _issue(self, locator):
        self._issued.add(_key(locator))
        return locator

    def _reference(self, locator, *, issue=False):
        """Map a local original locator, including detector-added line numbers."""
        key = _key(locator)
        if locator == DOCUMENT_LOCATOR:
            reference = dict(DOCUMENT_LOCATOR)
        elif key in self._by_locator:
            candidates = self._by_locator[key]
            index = next((n for n in candidates if _key({"unit": n}) in self._issued), candidates[0])
            reference = {"unit": index}
        elif type(locator.get("line")) is int and locator["line"] > 0:
            base = {k: v for k, v in locator.items() if k != "line"}
            candidates = self._by_locator.get(_key(base), [])
            candidates = [n for n in candidates if self._valid_line(n, locator["line"])]
            if not candidates:
                _fail()
            index = next((n for n in candidates if _key({"unit": n}) in self._issued), candidates[0])
            reference = {"unit": index, "line": locator["line"]}
        else:
            _fail()
        if issue:
            return self._issue(reference)
        self._restore_locator(reference)
        return reference

    def _valid_line(self, index, line):
        unit = self._units[index]
        return ("line" not in unit["locator"] and type(line) is int and
                0 < line <= len(unit["text"].splitlines()))

    def source_payload(self, max_chars=60000):
        """Only opaque references and redacted text, with explicit truncation.

        ``None`` requests an untruncated view for local compatibility helpers.
        Model requests use the bounded default.
        """
        if max_chars is not None and (type(max_chars) is not int or max_chars < 0):
            _fail()
        self._issued.clear()
        lines, total = [], 0
        truncated = False
        for index, unit in enumerate(self._units):
            reference = {"unit": index}
            chunk = json.dumps(reference, sort_keys=True) + "\t" + self._text(unit["text"])
            if max_chars is not None and total + len(chunk) + 1 > max_chars:
                truncated = True
                break
            lines.append(chunk)
            total += len(chunk) + 1
            self._issue(reference)
        return {"document_locator": self._issue(dict(DOCUMENT_LOCATOR)),
                "record_text": lines, "truncated": truncated}

    def _detail(self, detail):
        """Retain useful detector values without forwarding arbitrary metadata."""
        if not isinstance(detail, dict):
            _fail()
        safe = {}
        if isinstance(detail.get("element"), str) and detail["element"] in POLICY_ELEMENTS:
            safe["element"] = detail["element"]
        if isinstance(detail.get("comparison"), str) and detail["comparison"] in {"exceeds_chp_60_days", "within_chp_60_days"}:
            safe["comparison"] = detail["comparison"]
        for name in ("stated_days", "reference_days"):
            value = detail.get(name)
            if type(value) in (int, float) and 0 <= value <= 1000000000 and math.isfinite(value):
                safe[name] = value
        return safe

    def primary_payload(self, hits, rules, max_chars=60000):
        sources = self.source_payload(max_chars=max_chars)
        safe_hits = []
        for hit in hits:
            if not isinstance(hit, dict):
                _fail()
            rule_id = hit.get("rule_id")
            safe_hits.append({
                "detector": self._text(hit.get("detector")),
                "rule_id": None if rule_id is None else self._text(rule_id),
                "locator": self._reference(hit.get("locator"), issue=True),
                "excerpt": self._text(hit.get("excerpt")),
                "kind": self._text(hit.get("kind")),
                "detail": self._detail(hit.get("detail", {})),
            })
        safe_rules = []
        for rule in rules:
            if not isinstance(rule, dict):
                _fail()
            safe_rules.append({"rule_id": self._text(rule.get("rule_id")),
                               "citation": self._text(rule.get("citation")),
                               "duty": self._text(rule.get("duty"))[:300]})
        return {"instructions": "Data follows. Each text line is <locator JSON><TAB><text>. "
                "Cite an opaque locator exactly as given. A line within a unit may be cited by "
                "adding its one-based line number. Use document_locator for document-wide observations.",
                **sources, "rules": safe_rules, "detector_hits": safe_hits}

    def digest_payload(self, digest):
        """Allowlisted challenge view; exact original locators remain local."""
        if not isinstance(digest, dict):
            _fail()
        result = {"scope": self._text(digest.get("scope")),
                  "actors": [self._text(actor) for actor in self._list(digest.get("actors"))]}
        for field in ("dates", "statements", "omissions", "counterevidence"):
            result[field] = []
            for item in self._list(digest.get(field)):
                if not isinstance(item, dict):
                    _fail()
                text_key = "date" if field == "dates" else "text"
                safe = {text_key: self._text(item.get(text_key))}
                if "locator" in item:
                    safe["locator"] = None if item["locator"] is None else self._reference(item["locator"])
                if field == "dates" and "note" in item:
                    safe["note"] = self._text(item["note"])
                result[field].append(safe)
        result["conclusions"] = []
        for conclusion in self._list(digest.get("conclusions")):
            if not isinstance(conclusion, dict) or conclusion.get("confidence") not in CONFIDENCE:
                _fail()
            safe = {"text": self._text(conclusion.get("text")),
                    "confidence": conclusion["confidence"], "sources": []}
            for source in self._list(conclusion.get("sources")):
                if not isinstance(source, dict):
                    _fail()
                rule_id = source.get("rule_id")
                safe["sources"].append({"locator": self._reference(source.get("locator")),
                                        "rule_id": None if rule_id is None else self._text(rule_id)})
            result["conclusions"].append(safe)
        return result

    def _restore_locator(self, reference):
        key = _key(reference)
        if reference == DOCUMENT_LOCATOR and key in self._issued:
            return dict(DOCUMENT_LOCATOR)
        if set(reference) not in ({"unit"}, {"unit", "line"}):
            _fail()
        index = reference.get("unit")
        if type(index) is not int or not 0 <= index < len(self._units):
            _fail()
        if "line" in reference and not self._valid_line(index, reference["line"]):
            _fail()
        if key not in self._issued and not ("line" in reference and _key({"unit": index}) in self._issued):
            _fail()
        locator = deepcopy(self._units[index]["locator"])
        if "line" in reference:
            locator["line"] = reference["line"]
        return locator

    def restore_output(self, output):
        """Restore only issued opaque references; never trust returned metadata."""
        result = deepcopy(output)
        if not isinstance(result, dict):
            _fail()
        for field in ("dates", "statements", "omissions", "counterevidence"):
            for item in self._list(result.get(field)):
                if not isinstance(item, dict):
                    _fail()
                if "locator" in item and item["locator"] is not None:
                    item["locator"] = self._restore_locator(item["locator"])
        for conclusion in self._list(result.get("conclusions")):
            if not isinstance(conclusion, dict):
                _fail()
            for source in self._list(conclusion.get("sources")):
                if not isinstance(source, dict):
                    _fail()
                source["locator"] = self._restore_locator(source.get("locator"))
        return result
