# Plan A — attended approval-anchor installation / rollback

Build reference: approved design SHA256
`afd44123ec1479f61b84d3b7e12b972691410820135514f1ce9784c18ff56cab`.
**This PR installs nothing.** G approved Plan A on 2026-11-14 at 18:15; the
operator schedules 21:00 or the next accepted window. Reserve **60–90 minutes**
of records/mail downtime (last historical swap: 55 minutes; not a current estimate
of disk fit). No send, publication, image/engine-pin change or key in Git.

1. **Before any recreate:** normal PR review/CI must pass. Separately activate the
   DEFERRED-capable engine through the existing reviewed REPIN process; keep
   decision application paused using the wrapper's existing rollback until then.
   Set `RECIPE` to the clean, current-main checkout's `container/flock-ocr`, `ENGINE`
   to the wrapper's in-container engine, `TAG` to the unchanged running
   image tag, `PREV` to the verified run that built that image, and `PRIVATE` to its
   existing owner-private parent. Set `PROJECT` to the private in-container project,
   and `WRAPPER` to the existing host records wrapper (read-only input; no pin edit).
   The wrapper's `ENGINE_DIR=` is the host source tree, not this engine: `ENGINE` is the
   single `/workspace/.../engine-<sha>` value of its container `PYTHONPATH=` lines, and
   `<sha>` must be a prefix of its `RELEASE_COMMIT=`. The swap tool derives the same
   value and refuses on none, two distinct values or a mismatch, and its dry-run
   refuses unless the mounts holding both `ENGINE` and its work directory (which also
   holds pylib-pillow, records-project and sandbox) are snapshotted and no excluded
   mount lies inside that work directory.
   Do not guess these values, print container Env, or commit runtime settings.
   Check activation and record its timestamp on the execution card:
   ```sh
   docker exec -u coder -w "$ENGINE" agentorange-flock /opt/campaign-tool/venv/bin/python -B -m campaign_tool.approval --anchor-status
   ```
   Expect exit 0, `anchor unavailable: ...` before the mount. Missing command,
   nonzero exit or no `anchor ` prefix: STOP; never apply with the old engine.
2. **Single G step (HOSTA-1 step 11, his keyboard only):** G supplies only his
   public key line, formed as `<principal> namespaces="deflock-approval"
   <key-type> <public-key-base64> deflock-approval`; no private key/password/token.
   G runs the following and pastes that line followed by Ctrl-D at the second command:
   ```sh
   sudo install -d -o root -g root -m 0755 /etc/deflock
   sudo install -o root -g root -m 0644 /dev/stdin /etc/deflock/approvers.allowed_signers
   stat -c '%u %g %a' /etc/deflock /etc/deflock/approvers.allowed_signers
   ```
   Expect `0 0 755`, `0 0 644`. Agents never install/edit keys or print that file.
3. **Attended preconditions:** no active records run; allow the full downtime
   before the next slot. The existing swap tool owns pause, snapshots, space/restore
   checks, health and rollback. A retained `*-pre-ocr` container must be disposed of
   only under its existing owner procedure, never deleted to force this run.
   Run the reviewed design's A0 probe (operator only):
   `python3 /home/user/agent-work/r4-df-05-f1b/prototype/run_probes.py` → `RESULT 13/13`.
   This is still required: synthetic tests do not measure rootless ownership or hardware keys.
   Read-only R7 check: fold the live queue with `verify=False`, inspect event chains
   for reject/second-approve over a recorded approval without an intervening Edit.
   If found, STOP for the data owner; do not repair history. Rehearse the mount:
   ```sh
   python3 "$RECIPE/rootless_swap.py" --rehearse --private "$PRIVATE/trust-rehearsal" --trust-mount add --records-wrapper "$WRAPPER"
   ```
   Expect `rehearsal: OK`. It uses synthetic data and a simulated engine check;
   step 1 proves actual activation independently. Then dry-run and inspect its plan:
   ```sh
   python3 "$RECIPE/rootless_swap.py" --dry-run --container agentorange-flock --pause-unit records-run --pause-unit deflockslo-daily-mail --private "$PRIVATE" --new-tag "$TAG" --reuse-image "$PREV" --trust-mount add --records-wrapper "$WRAPPER"
   ```
   Only allowed settings delta: bind source/target `/etc/deflock`, read-only;
   unchanged image. Space/restore checks must pass; no deletion to make room.
4. **Attended recreate (not a worker command):** run the identical dry-run arguments
   with `--apply` in place of `--dry-run`. Exit 0 = complete, 1 = failed/rolled back,
   2 = refused before changes, 3 = held A2 for owner review, 4 = rollback failed,
   everything left stopped: call the operator. Never blindly retry.
   ```sh
   python3 "$RECIPE/rootless_swap.py" --apply --container agentorange-flock --pause-unit records-run --pause-unit deflockslo-daily-mail --private "$PRIVATE" --new-tag "$TAG" --reuse-image "$PREV" --trust-mount add --records-wrapper "$WRAPPER"
   docker inspect --format '{{range .Mounts}}{{if eq .Destination "/etc/deflock"}}{{.Source}} {{.Destination}} {{.RW}}{{end}}{{end}}' agentorange-flock
   docker exec -u coder -w "$ENGINE" agentorange-flock /opt/campaign-tool/venv/bin/python -B -m campaign_tool.approval --anchor-status
   ```
   Expect `/etc/deflock /etc/deflock false`, then `anchor ok`. Next `EXPORT.json`
   must have `approval_anchor: {state: ok}`. Check interval approvals since step 1:
   still DEFERRED or APPLIED; any final SIGNATURE_REFUSED must genuinely fail current
   read-only signature verification. A now-valid refused signature is a rollout FAIL.
   G's first real page Approve, when he chooses, must become APPLIED next slot.
   No test key goes into the real anchor. Keep old container and all backups.

**Rollback:** failed apply rolls back automatically. Interrupted/held run:
`python3 "$RECIPE/rootless_swap.py" --rollback "$RUN_DIR"` (exact recorded run).
After a completed install, repeat dry-run/apply with `--trust-mount drop` instead
of `add` (omit `--records-wrapper` on drop), same unchanged-image reuse arguments and downtime; execute only in the
attended approved rollback window. Never remove `/etc/deflock` while mounted.
Before any old-engine rollback, pause decision application and leave it paused
until the DEFERRED-capable engine returns and step 1 passes. History/receipts remain
immutable; queue approvals re-read pending and switches OFF during an outage;
restoring the same anchor restores approval evidence and notice dates automatically.

**Rotation/recovery:** adding a line while retaining the old one preserves approvals.
G should remove an old line only when he means to revoke what it signed. Revoked
queue rows require Edit → current-key Approve; Reject/second Approve cannot overwrite
unverified approval. Packets retain approved status, but fresh writes fail (switch
OFF, then approval_required after current-key switch enable). Recovery for unwritten
packets: operator `public-packets cancel --project "$PROJECT" --id ID --actor operator`,
then re-propose identical content via its original proposal command (r+1), then G
approves that revision with his current key. Reject/re-Approve is not recovery.
Written packets/output remain written and exported; list revoked-principal written
packets for the site-PR reviewer to exclude. Withdrawal needs a separate design.
No automatic send, publication, or history rewrite is authorized by this runbook.
