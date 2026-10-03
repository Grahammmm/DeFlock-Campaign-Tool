"""Real TS signer -> loopback Python gateway -> synthetic transport acceptance.

Requires the pinned Workers node_modules. No credentials, real D1, mail or provider.
The Worker tests separately exercise the real D1 approval/recovery caller.
"""
import datetime as dt
import json
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from campaign_tool.outbox import ProviderReceipt  # noqa: E402
from runner.outbox_gateway import ApprovedOutboxGateway, make_handler  # noqa: E402


def main():
    calls = []
    def deliver(draft):
        calls.append(draft)
        return ProviderReceipt("email", "synthetic-cross-language-message")
    with tempfile.TemporaryDirectory(prefix="deflock-bridge-") as directory:
        path = Path(directory).resolve()
        bundle = path / "sender.mjs"
        build = "require(" + json.dumps(str(ROOT / "workers/node_modules/esbuild")) + ").buildSync(" + json.dumps({
            "entryPoints": [str(ROOT / "workers/workspace/src/executors/outbox_sender.ts")],
            "bundle": True, "platform": "node", "format": "esm", "outfile": str(bundle)}) + ")"
        subprocess.run(["node", "-e", build], cwd=ROOT, check=True, timeout=30)
        gateway = ApprovedOutboxGateway("synthetic", "synthetic-signing-key-for-tests-only-0000",
            path / "private/outbox.sqlite", "sender@example.invalid", {"email": deliver})
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(gateway))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        stamp = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        script = path / "accept.mjs"
        script.write_text('''import assert from "node:assert/strict";
import {SignedOutboxSender} from "./sender.mjs";
const approvedAt = STAMP;
const action = {action_id:"synthetic-action",campaign_id:"synthetic",state:"executing",
  kind:"send_request",subject_id:"request-1",idempotency_key:"worker-card-key",
  approved_by:"organizer@example.invalid",approved_at:approvedAt,
  proposal_json:JSON.stringify({channel:"email",to:"records@example.invalid",subject:"Synthetic request",body_md:"Synthetic body",
    outbox_binding:{agency_id:"synthetic-agency",scope_version:1,fee_cap_cents:0}})};
const repo = {campaignId:"synthetic",async action(){return action;},async assertExecuting(value){assert.equal(value,action);},
  async campaign(){return {external_sends:"approval_required"};},async request(){return {request_id:"request-1",agency_id:"synthetic-agency",scope_version:1,fee_cap_cents:0};}};
let networkCalls = 0;
const sender = new SignedOutboxSender("https://outbox.example.invalid/send","synthetic-signing-key-for-tests-only-0000",
  async (_url,init)=>{networkCalls++;return fetch(LOOPBACK,init);});
const message={action_id:action.action_id,kind:"send_request",channel:"email",request_id:"request-1",to:"records@example.invalid",
  subject:"Synthetic request",body_md:"Synthetic body",idempotency_key:"worker-card-key"};
const first=await sender.send(message,{repo});
const second=await sender.send(message,{repo});
assert.deepEqual(first,second);assert.equal(first.provider_message_id,"synthetic-cross-language-message");
assert.equal(networkCalls,2);console.log(JSON.stringify({protocol:"v1",replays:2,receipt:first}));
'''.replace("STAMP", json.dumps(stamp)).replace("LOOPBACK", json.dumps(f"http://127.0.0.1:{server.server_port}/send")))
        try:
            result = subprocess.run(["node", str(script)], cwd=ROOT, check=True, timeout=30, capture_output=True, text=True)
            receipt = json.loads(result.stdout)
            assert len(calls) == 1, "identical replay called provider twice"
            assert calls[0].agency_id == "synthetic-agency"
            assert calls[0].from_addr == "sender@example.invalid"
            print(json.dumps({"acceptance": "pass", "provider_calls": len(calls), "bridge": receipt}))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == "__main__":
    main()
