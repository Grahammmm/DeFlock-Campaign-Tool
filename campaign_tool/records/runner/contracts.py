"""Explicit injectable boundaries. Record content never selects executable code."""
from dataclasses import dataclass
import hashlib
import json
from typing import Protocol, Iterable

class IntegrationGap(RuntimeError):
    pass

@dataclass(frozen=True)
class Folder:
    name: str
    uidvalidity: int

@dataclass(frozen=True)
class Preserved:
    message_id: str
    eml_sha256: str
    receipt_sha256: str
    documents: tuple[str, ...]
    attachments: tuple[tuple[str, str], ...]  # (one-based hierarchical MIME path, hash)

@dataclass(frozen=True)
class StageReceipt:
    subject_sha256: str
    stage: str
    body: bytes
    @property
    def sha256(self):return hashlib.sha256(self.body).hexdigest()
    def validate(self):
        if not isinstance(self.body,bytes) or len(self.body)>1024*1024:raise ValueError('receipt_bound')
        obj=json.loads(self.body.decode('utf-8'))
        if not isinstance(obj,dict) or obj.get('subject_sha256')!=self.subject_sha256 or obj.get('stage')!=self.stage:
            raise ValueError('receipt_binding')
        return self

class Exporter(Protocol):
    def folders(self) -> Iterable[Folder]: ...
    def uids(self, folder: Folder, after_uid: int) -> Iterable[int]: ...
    def receipt(self, folder: Folder, uid: int) -> str: ...

class LedgerBackend(Protocol):
    """Implementations must atomically preserve/promote, with exact replay binding.

    A runner crash after backend commit and before journal commit is retried.
    Both methods MUST be idempotent for the same identity/content, and reject
    changed bytes/receipts for an existing identity. They may not send or publish.
    """
    def preserve(self, receipt_path: str, account: str, folder: Folder, uid: int) -> Preserved: ...
    def validate_and_promote(self, receipt: StageReceipt, run_identity: dict) -> None: ...
