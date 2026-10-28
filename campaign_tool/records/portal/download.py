"""Explicit portal byte enrollment. Default is inventory-only; no stage promotion.

Trusted integration configuration supplies policy paths, not queue/record text.
This module neither provisions a policy nor changes the upstream acquisition owner.
"""
from contextlib import contextmanager
import fcntl
import math
import os
from pathlib import Path
import stat

from .engine import Limits, fetch_queue
from .https_transport import StdlibHTTPS, load_egress
from .ledger_adapter import WP1LedgerAdapter
from .policy import PortalError, load_approval, safe_ancestors, url_parts
from .store import check_hash, digest_bytes, identifier


@contextmanager
def processing_lock(root):
    """Same <processing>/run.lock inode/order as records.run.root_lock.

    No import of the model/extraction pipeline is needed for byte enrollment.
    Caller holds the outer records-run lock, then this lock, then Queue.lock.
    """
    root = safe_ancestors(root)
    value = root.stat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.geteuid() or value.st_mode & 0o077:
        raise PortalError('processing_not_private')
    fd = os.open(root / 'run.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid() or value.st_mode & 0o077 or value.st_nlink != 1:
            raise PortalError('processing_lock_unsafe')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PortalError('processing_busy') from None
        yield
    finally:
        os.close(fd)


def validate_queue(queue):
    """Preflight producer-shaped queue bindings before any request.

    Invalid URLs/schema/provenance fail the batch, not 'empty success'. A denied
    but syntactically valid host is an item-level block in the existing engine.
    Mail SHA references remain references, not invented canonical mail joins.
    """
    import json
    for item in queue.db.execute('SELECT * FROM portal_items'):
        identifier(item['request_id'])
        identifier(item['item_id'])
        check_hash(item['id'])
        if item['state'] not in {'pending','fetching','retry','received','blocked'}:
            raise PortalError('queue_invalid')
        if type(item['generation']) is not int or item['generation'] < 1 or type(item['tries']) is not int or item['tries'] < 0:
            raise PortalError('queue_invalid')
        if type(item['next_attempt']) not in (int,float) or not math.isfinite(item['next_attempt']):
            raise PortalError('queue_invalid')
        if url_parts(item['last_url_private']).hostname != item['host']:
            raise PortalError('initial_host_mismatch')
        expected = digest_bytes(json.dumps([item['host'],item['request_id'],item['item_id']],separators=(',',':')).encode())
        if expected != item['id']:
            raise PortalError('queue_identity_mismatch')
        sources = queue.db.execute('SELECT source_sha256 FROM portal_notices WHERE item=?',(item['id'],)).fetchall()
        if not sources:
            raise PortalError('queue_notice_missing')
        for source in sources:
            check_hash(source[0])
        if item['current_sha256'] is not None:
            check_hash(item['current_sha256'])


def run_download(queue, processing, *, apply=False, approval_loader=None,
                 egress_loader=None, transport=None, limits=None, fault=None):
    """Trusted caller only. Synthetic tests inject only loopback transport/loaders.

    A received item is download-DONE, even if canonical outbox delivery is pending.
    It is not preserve-DONE. Delivery failures remain durable/retryable locally.
    """
    if not apply:
        return fetch_queue(queue, apply=False)
    if approval_loader is None or egress_loader is None:
        raise PortalError('download_policies_required')
    approval_loader()
    egress_loader()
    limits = limits or Limits()
    processing = safe_ancestors(processing)
    database = safe_ancestors(processing / 'ledger.sqlite')
    blobs = safe_ancestors(processing / 'intake' / 'blobs')
    if not database.is_file() or not blobs.is_dir():
        raise PortalError('processing_uninitialized')
    with processing_lock(processing):
        adapter = WP1LedgerAdapter(queue=queue,database=database,cas_root=blobs)
        transport = transport or StdlibHTTPS(apply=True,approval_loader=approval_loader,
                                            egress_loader=egress_loader,max_bytes=limits.max_bytes)
        def egress(url):
            from datetime import datetime, timezone
            return egress_loader().allows(url_parts(url).hostname,datetime.now(timezone.utc))
        outcome = fetch_queue(queue,apply=True,approval_loader=approval_loader,
                              egress=egress,transport=transport,ledger=adapter,
                              limits=limits,fault=fault,preflight=validate_queue)
        return outcome


def from_stage(queue, args):
    """No CLI transport selector for loopback; production accepts HTTPS only."""
    limits = Limits(max_bytes=args.portal_max_bytes,max_total_bytes=args.portal_total_bytes,
                    max_items=args.portal_max_items,timeout=args.portal_request_timeout,
                    run_timeout=min(args.timeout-5,1200))
    return run_download(queue, Path(args.project)/'processing-v1',apply=True,
                        approval_loader=lambda:load_approval(args.approval_file),
                        egress_loader=lambda:load_egress(args.portal_egress_file),limits=limits)
