"""Explicit, credential-free CLI selection for the existing export bridge.

Validation reads path metadata only. Queue/schema creation is deferred until
the ordinary Pipeline.run root lock is held. This is not a retrieval approval.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path

from .intake.mail_delta import source_path
from .portal.collections import CollectionInventory, Scope
from .portal.policy import PortalError, safe_ancestors
from .portal.store import Queue


@dataclass(frozen=True)
class ExportSelection:
    root: Path
    receipts: tuple
    scope: Scope
    canonical_root: Path

    def __post_init__(self):
        if (type(self.scope) is not Scope or type(self.scope.hosts) is not frozenset
                or type(self.scope.excluded_hosts) is not frozenset):
            raise ValueError("invalid_export_scope")
        if type(self.receipts) is not tuple or not 1 <= len(self.receipts) <= 60:
            raise ValueError("export_receipt_bound")
        if any(not isinstance(item, (str, Path)) for item in self.receipts):
            raise ValueError("invalid_export_receipt")
        for value in (self.root, self.canonical_root):
            if not isinstance(value, (str, Path)) or not 0 < len(str(value)) <= 4096 or "\x00" in str(value):
                raise ValueError("invalid_export_root")
        root = safe_ancestors(self.root)
        canonical = safe_ancestors(self.canonical_root)
        receipts = tuple(safe_ancestors(source_path(root, str(item), canonical / "intake"))
                         for item in self.receipts)
        if len(set(receipts)) != len(receipts):
            raise ValueError("duplicate_export_receipt")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "canonical_root", canonical)
        object.__setattr__(self, "receipts", receipts)

    @contextmanager
    def open(self, canonical_root):
        from .run import require_root_lock
        canonical = Path(os.path.abspath(canonical_root))
        if canonical != self.canonical_root:
            raise ValueError("export_selection_root_mismatch")
        require_root_lock(canonical)
        # Repeat path-only validation at the use boundary. Actual receipt/blob
        # opening and byte/occurrence validation remain with the existing bridge.
        checked = ExportSelection(self.root, self.receipts, self.scope, canonical)
        queue = Queue(canonical / "portal")
        try:
            inventory = CollectionInventory(queue, checked.scope)
            yield {"root": checked.root, "receipts": checked.receipts, "inventory": inventory}
        finally:
            queue.close()


def from_cli(args):
    """Return a validated immutable selection, or None for ordinary input modes."""
    values = (args.export_root, args.export_receipt, args.collection_host, args.collection_exclude_host)
    if all(value is None for value in values):
        return None
    if (not args.export_root or not args.export_receipt
            or any(value is not None for value in
                   (args.inbox, args.mail_config, args.documents, args.document_manifest_sha256))):
        raise ValueError("invalid_export_selection")
    try:
        scope = Scope(frozenset(args.collection_host or ()), frozenset(args.collection_exclude_host or ()))
        return ExportSelection(args.export_root, tuple(args.export_receipt), scope, args.root)
    except (OSError, ValueError, PortalError):
        # Never print supplied paths, URLs or configuration through argparse.
        raise ValueError("invalid_export_selection") from None
