"""Typed client for the workspace runner API (docs/CONTRACTS.md) plus an in-memory fake.

`Workspace` uses urllib only. Every call carries the bearer token, retries
5xx and connection errors with exponential backoff, and raises
`WorkspaceError` with the HTTP status otherwise. `FakeWorkspace` implements
the same interface over dictionaries so the loop and handlers are tested
without a network; it mirrors the Worker's result semantics (followups are
enqueued from `outputs.followups`, failed jobs requeue while attempts remain).
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request

HEX64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_PATH = re.compile(r"^(?:[A-Za-z0-9_][A-Za-z0-9._-]{0,120})(?:/[A-Za-z0-9_][A-Za-z0-9._-]{0,120})*$")
SAFE_VERSION = re.compile(r"^[a-z0-9._-]{1,64}$")
SITE_FILE_MAX_BYTES = 16 * 1024 * 1024


class WorkspaceError(Exception):
    def __init__(self, status, message="", retryable=False):
        super().__init__(f"workspace HTTP {status}: {message}" if status else message)
        self.status = status
        self.retryable = retryable


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


class Workspace:
    """HTTP client bound to one campaign's runner token."""

    def __init__(self, base_url, token, timeout=60, max_retries=4, sleep=time.sleep, opener=None, backoff_base=1.0):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("WORKSPACE_URL must be http(s)")
        if not token:
            raise ValueError("RUNNER_TOKEN is required")
        self.base = base_url.rstrip("/") + "/api/runner"
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep
        self.opener = opener or urllib.request.urlopen
        self.backoff_base = backoff_base

    # -- transport -----------------------------------------------------------
    def _request(self, method, path, body=None, content_type="application/json", raw=False):
        data = None
        headers = {"authorization": "Bearer " + self.token, "accept": "application/json"}
        if body is not None:
            data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
            headers["content-type"] = content_type
        attempt = 0
        while True:
            request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    status = response.status
                    payload = response.read()
                    if status == 204:
                        return None
                    if raw:
                        return payload, dict(response.headers.items())
                    return json.loads(payload.decode("utf-8")) if payload else {}
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                if exc.code >= 500 and attempt < self.max_retries:
                    attempt += 1
                    self.sleep(self.backoff_base * (2 ** (attempt - 1)))
                    continue
                raise WorkspaceError(exc.code, detail, retryable=exc.code >= 500) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    self.sleep(self.backoff_base * (2 ** (attempt - 1)))
                    continue
                raise WorkspaceError(0, "unreachable: " + str(getattr(exc, "reason", exc)), retryable=True) from None

    # -- jobs ----------------------------------------------------------------
    def lease_job(self, lease_seconds=300):
        return self._request("GET", f"/jobs?lease={int(lease_seconds)}")

    def post_result(self, job_id, status, outputs=None, receipt=None, error=None):
        if status not in ("done", "failed", "blocked"):
            raise ValueError("status must be done|failed|blocked")
        body = {"status": status, "outputs": outputs or {}, "receipt": receipt}
        if error:
            body["error"] = str(error)[:2000]
        return self._request("POST", f"/jobs/{job_id}/result", body)

    # -- originals -----------------------------------------------------------
    def get_original(self, sha256):
        if not HEX64.match(sha256):
            raise ValueError("sha256 must be 64 lowercase hex")
        payload, headers = self._request("GET", f"/originals/{sha256}", raw=True)
        if sha256_hex(payload) != sha256:
            raise WorkspaceError(0, "original bytes do not match their sha256")
        return payload, {k.lower(): v for k, v in headers.items()}.get("content-type", "application/octet-stream")

    def put_original(self, data, media_type="application/octet-stream"):
        sha = sha256_hex(data)
        result = self._request("PUT", f"/originals/{sha}", bytes(data), content_type=media_type)
        return {"sha256": sha, **(result or {})}

    # -- rows ----------------------------------------------------------------
    def propose(self, kind, subject_id, proposal, idempotency_key, proposed_by):
        return self._request("POST", "/proposals", {"kind": kind, "subject_id": subject_id, "proposal": proposal,
                                                    "idempotency_key": idempotency_key, "proposed_by": proposed_by})

    def post_correspondence(self, row):
        return self._request("POST", "/correspondence", row)

    def post_receipt(self, row):
        return self._request("POST", "/receipts", row)

    # -- public site ---------------------------------------------------------
    def put_site_file(self, version, path, data, media_type):
        if not SAFE_VERSION.match(version):
            raise ValueError("site version must match [a-z0-9._-]{1,64}")
        if not SAFE_PATH.match(path):
            raise ValueError("unsafe site path: " + path)
        if len(data) > SITE_FILE_MAX_BYTES:
            raise ValueError("site file exceeds size cap: " + path)
        return self._request("PUT", f"/site/{version}/{path}", bytes(data), content_type=media_type)


class FakeWorkspace:
    """In-memory stand-in with the same interface, for tests and `--once --fake`."""

    def __init__(self, privacy_tier="redacted_cloud"):
        self.privacy_tier = privacy_tier
        self.jobs = {}
        self.order = []
        self.results = []
        self.originals = {}
        self.media_types = {}
        self.proposals = {}
        self.correspondence = []
        self.receipts = {}
        self.site_files = {}
        self.fail_next = 0
        self.calls = []
        self._n = 0

    def _maybe_fail(self):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise WorkspaceError(503, "simulated outage", retryable=True)

    def enqueue(self, kind, inputs=None, idempotency_key=None, max_attempts=3):
        key = idempotency_key or sha256_hex((kind + json.dumps(inputs or {}, sort_keys=True)).encode())
        for job in self.jobs.values():
            if job["idempotency_key"] == key:
                return job
        self._n += 1
        job = {"job_id": "job_%016x" % self._n, "campaign_id": "01test0000000000000000000a", "kind": kind,
               "idempotency_key": key, "inputs": dict(inputs or {}), "attempt": 0, "max_attempts": max_attempts,
               "enqueued_at": "2026-09-30T00:00:00Z", "leased_until": None, "privacy_tier": self.privacy_tier,
               "state": "queued", "outputs": None, "error": None}
        self.jobs[job["job_id"]] = job
        self.order.append(job["job_id"])
        return job

    def lease_job(self, lease_seconds=300):
        self.calls.append(("lease", lease_seconds))
        self._maybe_fail()
        for job_id in self.order:
            job = self.jobs[job_id]
            if job["state"] == "queued" and job["attempt"] < job["max_attempts"]:
                job["state"] = "leased"
                job["attempt"] += 1
                return {k: v for k, v in job.items() if k not in ("state", "outputs", "error")}
        return None

    def post_result(self, job_id, status, outputs=None, receipt=None, error=None):
        self.calls.append(("result", job_id, status))
        self._maybe_fail()
        job = self.jobs.get(job_id)
        if not job:
            raise WorkspaceError(404, "unknown job")
        if job["state"] != "leased":
            raise WorkspaceError(409, f"job is {job['state']}, not leased")
        outputs = dict(outputs or {})
        outputs["receipt"] = receipt
        state = status
        if status == "failed" and job["attempt"] < job["max_attempts"]:
            state = "queued"
        job.update(state=state, outputs=outputs, error=error)
        self.results.append({"job_id": job_id, "status": status, "outputs": outputs, "error": error})
        followups = []
        if status == "done":
            for item in outputs.get("followups") or []:
                followups.append(self.enqueue(item["kind"], item.get("inputs") or {}, item.get("idempotency_key"))["job_id"])
        return {"job_id": job_id, "state": state, "followups": followups}

    def get_original(self, sha256):
        self._maybe_fail()
        if sha256 not in self.originals:
            raise WorkspaceError(404, "unknown original")
        return self.originals[sha256], self.media_types.get(sha256, "application/octet-stream")

    def put_original(self, data, media_type="application/octet-stream"):
        self._maybe_fail()
        sha = sha256_hex(data)
        created = sha not in self.originals
        self.originals[sha] = bytes(data)
        self.media_types.setdefault(sha, media_type)
        return {"sha256": sha, "byte_count": len(data), "created": created}

    def propose(self, kind, subject_id, proposal, idempotency_key, proposed_by):
        self._maybe_fail()
        created = idempotency_key not in self.proposals
        if created:
            self.proposals[idempotency_key] = {"action_id": "act_%016x" % (len(self.proposals) + 1), "kind": kind, "subject_id": subject_id,
                                               "proposal": proposal, "proposed_by": proposed_by, "state": "proposed"}
        row = self.proposals[idempotency_key]
        return {"action_id": row["action_id"], "state": row["state"], "created": created}

    def post_correspondence(self, row):
        self._maybe_fail()
        for existing in self.correspondence:
            if row.get("provider_message_id") and existing["provider_message_id"] == row.get("provider_message_id"):
                return {"correspondence_id": existing["correspondence_id"], "created": False}
        stored = {"correspondence_id": "cor_%016x" % (len(self.correspondence) + 1), **row}
        self.correspondence.append(stored)
        return {"correspondence_id": stored["correspondence_id"], "created": True}

    def post_receipt(self, row):
        self._maybe_fail()
        if row["sha256"] not in self.originals:
            raise WorkspaceError(409, "original not stored")
        receipt_id = sha256_hex(json.dumps([row["source_id"], row["sha256"]], separators=(",", ":")).encode())
        created = receipt_id not in self.receipts
        self.receipts.setdefault(receipt_id, row)
        return {"receipt_id": receipt_id, "created": created}

    def put_site_file(self, version, path, data, media_type):
        self._maybe_fail()
        if not SAFE_VERSION.match(version) or not SAFE_PATH.match(path):
            raise WorkspaceError(400, "unsafe path")
        if len(data) > SITE_FILE_MAX_BYTES:
            raise WorkspaceError(413, "too large")
        self.site_files[f"sites/{version}/{path}"] = (bytes(data), media_type)
        return {"key": f"sites/{version}/{path}", "sha256": sha256_hex(data), "bytes": len(data)}
