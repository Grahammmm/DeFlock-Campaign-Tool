# Hand over a campaign

For the outgoing and the incoming organizer, working through it together, in order. This checklist sends, publishes and deletes nothing. Stop if either organizer cannot account for a private file.

Names used below, all synthetic: `D` is the existing external campaign directory on the outgoing machine (for example `"/home/outgoing/campaigns/demo-handover"`); `ARCHIVE` is a new archive file outside `D`; `NEW_EMPTY_DIR` is an empty destination on the incoming machine; `DIGEST` is the `manifest_sha256` that the outgoing organizer keeps separately from the archive; `DRAFT` is a finding draft path or id. Replace them with your own values. Every command is offline.

## 1. What the campaign directory holds, and what never leaves the machine

| Part of `D` | What it holds | Custody |
| --- | --- | --- |
| `campaign.json` | the campaign's configuration | private; part of a backup |
| `content/` | approved public inputs the build reads | not a privacy bypass: it is only public once built and published |
| `kit/` | drafts, request history, sent and response records, private notes | private; part of a backup |
| `private/` | originals, the receipt ledger, finding drafts, review receipts, the reviewer roster, review assignments | private; only part of it is in a backup (task 4) |
| `public/` | rebuildable site output | not a backup: rebuild it, do not carry it |

"Never leaves the machine" means never public, never in source control and never in outreach. A custody transfer is a separate, intentional step: it goes only to the incoming organizer, over a channel both organizers have agreed, and the archive is encrypted. It is not permission to email an unencrypted archive or to send private notes. This guide has no bulk-copy, git-add, upload or hosted-export step, on purpose.

Read first: [keep the campaign directory outside the repository](FORK-AND-RUN.md#2-keep-the-campaign-directory-outside-the-repository), [never commit these](FORK-AND-RUN.md#6-never-commit-these) and [data handling](DATA-HANDLING.md).

Next: task 2.

## 2. Pause edits and make a fresh backup

Agree, by word, that neither organizer edits `D` until the handover is signed off. No command locks the directory. Review the private inventory together (task 10 prints it), then on the outgoing machine:

```sh
python3 -m campaign_tool backup --directory D --out ARCHIVE
python3 -m campaign_tool backup --directory D --out ARCHIVE --encrypt-to KEY.pub
```

Run one of the two lines. The second writes an encrypted archive for a public key that the incoming organizer made; no key material belongs in `D`. The output prints `manifest_sha256`: copy it as `DIGEST` into a place that is not the archive (a password manager, a private note on another device).

After the archive is copied over the approved encrypted channel, check it with the digest:

```sh
python3 -m campaign_tool verify --file ARCHIVE --expect-manifest-sha256 DIGEST
```

A bare `verify` is only an integrity check: whoever can rewrite a member can rewrite the manifest in the same archive. Only the separately kept digest is tamper evidence. Details: [commands](BACKUP.md#commands), [limits and cautions](BACKUP.md#limits-and-cautions), [encrypted archives](BACKUP.md#encrypted-archives).

Stop here if `verify` fails or the digest differs; make a new archive. Next: task 3.

## 3. A passing backup-drill does not prove the whole campaign was transferred

**A passing backup-drill does not prove the whole campaign was transferred.** The archive holds `campaign.json`, `content/`, `kit/`, the receipt ledger and the stored originals. It does not hold `private/drafts/`, `private/reviews/`, `private/review-assignments/` or `private/reviewers.json` (finding drafts, review receipts, review assignments and the reviewer roster), and it does not hold this machine's campaign register or `public/`. The tool does not yet back those up and this guide does not change that.

Before sign-off, list what is in `D/private/` and compare it with what the archive restored on the incoming machine. Agree how each missing file reaches the incoming organizer through a separately controlled private transfer, and who confirms it arrived intact. There is no flag that makes a complete archive, and no step here claims one.

Until that is done, write this in your sign-off notes: `Handover incomplete: private files outside the backup still need a verified custody transfer.`

Next: task 4.

## 4. Check the archive and restore it on the incoming machine

On the incoming machine, with the archive and `DIGEST` in hand:

```sh
python3 -m campaign_tool backup-drill --file ARCHIVE --expect-manifest-sha256 DIGEST --receipt OUT.json
python3 -m campaign_tool restore --file ARCHIVE --directory NEW_EMPTY_DIR
```

`backup-drill` verifies the archive, restores it into a temporary copy, runs the store check and `doctor` on that copy, prints a receipt and removes the copy. `--receipt OUT.json` is optional and must name a new private file. The drill is not the permanent restore; `restore` is, and it needs an empty `NEW_EMPTY_DIR` that is not a symlink. A failed drill or verify, or a non-empty or symlinked target, stops this task: never overwrite or delete to get past it. An encrypted archive needs the private key arranged outside the campaign and outside this guide; see [restore drill](BACKUP.md#restore-drill) and [commands](BACKUP.md#commands).

Next: task 5.

## 5. Reconcile excluded files, then register the campaign on this machine

Bring over the files that task 3 listed and check review state before relying on any restored output. Then give the incoming machine its own register entry:

```sh
python3 -m campaign_tool campaigns list
python3 -m campaign_tool campaigns add demo-handover --directory NEW_EMPTY_DIR
```

The register is per machine and holds only a name and a path. A name that already exists is not permission to remove someone else's entry; pick another agreed name. Neither a backup nor a register entry proves account access, identity, review validity or that the handover is complete. See [the register](MULTI-CAMPAIGN.md#the-register) and [`--campaign NAME`](MULTI-CAMPAIGN.md#--campaign-name). Later tasks use `--directory`; with a registered name you may use `--campaign NAME` instead.

Next: task 6.

## 6. Open records requests

```sh
python3 -m campaign_tool kit-status --directory D
python3 -m campaign_tool kit-due --directory D
```

`kit-status` lists each request and its recorded state; `kit-due` lists the ones whose reminder date has passed and exits 3 when work is past due. Compare request names, recorded responses and who takes the next step, by role. A provisional date is a reminder, not a finding that any law was broken. Do not mark anything sent or answered, and do not regenerate kits, to make this list look clean. See [recording a hand-sent request](AGENCY-DISCOVERY.md#recording-a-hand-sent-request) and [several requests to one agency](AGENCY-DISCOVERY.md#several-requests-to-one-agency).

Next: task 7.

## 7. Staged meetings and written-comment deadlines

```sh
python3 -m campaign_tool today --directory D
```

`today` is a triage view, not a full meeting inventory: also open the staged files under `D/content/meetings/` yourself. For each meeting read the source, the UTC offset, any cancellation and any comment already recorded as submitted. No command guesses a deadline or submits a comment. See [participation (organizer-verified)](MEETINGS.md#participation-organizer-verified) and [recording a comment you submitted](MEETINGS.md#recording-a-comment-you-submitted).

Next: task 8.

## 8. Findings in review, their assignments and the reviewer roster

```sh
python3 -m campaign_tool finding status --directory D
python3 -m campaign_tool finding review-status DRAFT --directory D
python3 -m campaign_tool finding reviewers list --directory D
```

Run `review-status` once for each outstanding draft, and print the roster only on a private screen. An assignment is a request to review, not a receipt, and a roster name or role is not authentication. Do not reassign, re-sign or publish as a handover shortcut. Compare the assignments, receipts and roster on the incoming machine with the outgoing machine's own files, not with a passing backup (task 3). See [offline review receipts](PUBLICATION.md#offline-review-receipts-cli), [the reviewer roster](PUBLICATION.md#reviewer-roster-role-assignment-not-authentication) and [what is not done](PUBLICATION.md#not-done).

Next: task 9.

## 9. The personal-data register

```sh
python3 -m campaign_tool personal-data --directory D
python3 -m campaign_tool personal-data --directory D --json
```

Run one of the two lines. It lists the private files that can hold a person's name, email, phone or postal address, by kind, purpose and date, without printing any value. Discuss each kind, and each unknown kind, and why it is kept, together and privately. Never paste the output into an issue or a public document. It is not a retention or deletion policy and not an inventory of the whole host or of the backups; unknown files need an organizer's review and are never deleted automatically. See [the private personal-data register](DATA-HANDLING.md#private-personal-data-register).

Next: task 10.

## 10. Accounts, by role, and sign-off

Name the person for each role below. Write only the role and a person, never a login, credential, key, account id, subscriber export, approval signature or recovery code, in this guide or anywhere in `D`. Confirm access out of band, in person or by a call, and not by message.

| Role | Person (fill in) | Access confirmed out of band |
| --- | --- | --- |
| Domain and DNS owner | | |
| Hosting owner | | |
| Mailbox owner | | |
| Newsletter owner | | |
| Backup custodian | | |
| Review coordinator | | |

See [accounts and costs](../README.md#accounts-and-costs) and [STATUS.md](STATUS.md). This guide provisions nothing, runs no worker or runner, and gives no standing permission to send.

Check the incoming machine, then fill in the sign-off by hand:

```sh
python3 -m campaign_tool doctor --directory D --checklist
```

- [ ] Custody reconciled, including the files outside the backup (task 3)
- [ ] Open requests, meetings and reviews are assigned by role (tasks 6 to 8)
- [ ] The sensitive-file inventory was reviewed (task 9)
- [ ] The incoming machine passes the doctor checklist
- [ ] Account access is confirmed out of band
