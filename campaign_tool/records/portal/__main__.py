"""Offline portal sidecar. Apply requires caller-injected transport and egress."""
import argparse
import json
from datetime import datetime, timezone
from .engine import fetch_queue
from .policy import PortalError, load_approval, url_parts
from .store import Queue


def main(argv=None, *, transport=None, egress=None, ledger=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",required=True)
    sub=parser.add_subparsers(dest="command",required=True)
    inventory=sub.add_parser("inventory")
    inventory.add_argument("--input",required=True,help="Private JSON array of hash-bound notice links")
    sub.add_parser("status")
    fetch=sub.add_parser("fetch")
    fetch.add_argument("--apply",action="store_true")
    fetch.add_argument("--approval-file")
    fetch.add_argument("--egress-file")
    fetch.add_argument("--https-transport",action="store_true",help="Opt in to bounded HTTPS; requires apply and two administrator policies")
    args=parser.parse_args(argv)
    queue=None
    try:
        queue=Queue(args.root)
        if args.command=="inventory":
            with open(args.input,"rb") as stream:
                raw=stream.read(1024*1024+1)
            if len(raw)>1024*1024:
                raise PortalError("inventory_oversize")
            rows=json.loads(raw)
            if not isinstance(rows,list) or len(rows)>1000:
                raise PortalError("inventory_invalid")
            for row in rows:
                if not isinstance(row,dict) or set(row)!={"host","request_id","item_id","url","source_sha256"}:
                    raise PortalError("inventory_invalid")
                # Untrusted notice payloads cannot opt themselves into refresh.
                queue.inventory(**row)
            result=queue.status()
        elif args.command=="status":
            result=queue.status()
        else:
            loader=(lambda:load_approval(args.approval_file)) if args.approval_file else None
            if args.https_transport:
                if not args.apply or not args.approval_file or not args.egress_file or transport is not None:
                    raise PortalError("https_opt_in_incomplete")
                from .https_transport import StdlibHTTPS,load_egress
                egress_loader=lambda:load_egress(args.egress_file)
                loader();egress_loader()
                egress=lambda url:egress_loader().allows(url_parts(url).hostname,datetime.now(timezone.utc))
                transport=StdlibHTTPS(apply=True,approval_loader=loader,egress_loader=egress_loader)
            result=fetch_queue(queue,apply=args.apply,approval_loader=loader,transport=transport,egress=egress,ledger=ledger)
        print(json.dumps(result,sort_keys=True))
        return 0
    except (PortalError,OSError,ValueError,TypeError):
        # CLI deliberately does not print input values or arbitrary exception text.
        print(json.dumps({"error":"portal_operation_failed","details":"Use private structured queue status; apply requires approved policy and configured adapters."}))
        return 2
    finally:
        if queue:
            queue.close()


if __name__=="__main__":
    raise SystemExit(main())
