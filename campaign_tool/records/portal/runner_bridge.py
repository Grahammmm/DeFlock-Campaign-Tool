"""WP2 boundary: enroll portal bytes, return pending work, never promote stages."""
from .engine import fetch_queue
from .policy import PortalError

INTERFACE_VERSION = "portal-runner-v1"


class PortalRunnerBridge:
    def __init__(self,*,queue,ledger,approval_loader,egress,transport):
        self.queue=queue;self.ledger=ledger;self.approval_loader=approval_loader
        self.egress=egress;self.transport=transport

    def run(self,*,apply=False,limits=None):
        outcome=fetch_queue(self.queue,apply=apply,approval_loader=self.approval_loader,
            egress=self.egress,transport=self.transport,ledger=self.ledger,limits=limits)
        return self._result(outcome,apply)

    def drain_outbox(self,*,apply=False,limit=100):
        """Retry already-preserved local bytes without DNS/HTTP or egress probing."""
        if type(limit) is not int or not 1<=limit<=1000:
            raise ValueError("invalid_limit")
        outcome={"mode":"local_outbox","ledger_delivered":0}
        if apply:
            with self.queue.lock():
                delivery=self.queue.deliver(self.ledger,limit)
                outcome["ledger_delivery"]=delivery
                outcome["ledger_pending"]=self.queue.status()["ledger_pending"]
                outcome["ledger_delivered"]=delivery["delivered"]
                if delivery["failed"]:
                    outcome["ledger_error"]="ledger_delivery_pending"
        else:
            outcome["ledger_pending"]=self.queue.status()["ledger_pending"]
        return self._result(outcome,apply)

    def _result(self,outcome,apply):
        return {"interface_version":INTERFACE_VERSION,"portal":outcome,
            "pending_card_inputs":self.ledger.pending_card_inputs() if apply else [],
            "stage_promotions":0,"canonical_integration_verified":False,"pipeline_complete":False}

    def validate_and_promote(self,*args,**kwargs):
        raise PortalError("trusted_portal_stage_adapter_unconfigured")
