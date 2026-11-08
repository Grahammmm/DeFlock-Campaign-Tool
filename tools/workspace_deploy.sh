#!/usr/bin/env bash
# One-command deploy of the Flock admin console (workers/workspace) to Cloudflare.
#
#   bash workspace_deploy.sh --sha <40-hex reviewed commit>             # dry run: prints, runs nothing
#   bash workspace_deploy.sh --sha <40-hex reviewed commit> --apply     # the real thing
#   bash workspace_deploy.sh --sha <sha> --apply --resume               # continue after a stop
#
# Runs on the deploying person's own computer (macOS or Linux), never on AgentOrange.
# Plain steps, in order: 1 preflight, 2 fresh clone + checks, 3 cost check, 4 Cloudflare login,
# 5 D1 + KV + deploy config, 6 migrations, 7 seed (approved by hash), 8 deploy, 9 pre-Access check,
# 10 Access dashboard steps, 11 secrets, 12 edge smoke check, then the final URL and rollback.
# (Secrets and Access values come AFTER the first deploy because a Worker must exist before it can
# hold secrets, and the Access audience tag only exists once the Access app is made.)
#
# Test hooks (env): DEPLOY_REPO_URL, DEPLOY_SEED_HOST, DEPLOY_SEED_DIR, WRANGLER_BIN, DEPLOY_SLEEP,
# DEPLOY_MIN_KB. No secret is ever written to disk, to the shell history or to the output.
set -euo pipefail
umask 077
export HISTFILE=/dev/null
export WRANGLER_SEND_METRICS=false

REPO_URL="${DEPLOY_REPO_URL:-https://github.com/Grahammmm/DeFlock-Campaign-Tool.git}"
SEED_HOST="${DEPLOY_SEED_HOST:-user@agentorange}"
SEED_DIR="${DEPLOY_SEED_DIR:-/home/user/agent-work/reports/admin-deploy/seed-out}"
SLEEP="${DEPLOY_SLEEP:-10}"
MIN_KB="${DEPLOY_MIN_KB:-1500000}"
D1_NAME="deflock-campaign"
KV_TITLE="deflock-workspace-CACHE"
WORKER_NAME="deflock-workspace"
CFG="wrangler.deploy.jsonc"
SELF="${BASH_SOURCE[0]}"

SHA=""
DRY=1
RESUME=0
DIR="${HOME:-.}/deflock-admin-deploy"
STEP="start"
APPLYING=0

say()  { printf '%s\n' "$*"; }
step() { STEP="$1"; printf '\n== Step %s ==\n' "$1"; }
die()  { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }
ask()  { printf '%s' "$1" >&2; IFS= read -r REPLY || die "no answer was given (input closed)."; }
dry()  { printf '  [dry-run] would run: %s\n' "$*"; }

on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ] && [ "$APPLYING" -eq 1 ]; then
    printf '\nThe deploy stopped at: %s. Nothing is half-hidden: fix the problem above, then run the same command again with --resume added.\n' "$STEP" >&2
  fi
}
trap on_exit EXIT

usage() {
  say "usage: bash workspace_deploy.sh --sha <40-hex> [--apply] [--dry-run] [--resume] [--dir <folder>]"
  say "  --dry-run  (default) print every command, run nothing"
  say "  --apply    really deploy"
  say "  --resume   skip steps already finished (state file in the folder)"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --sha) [ $# -ge 2 ] || die "--sha needs a value"; SHA="$2"; shift 2 ;;
    --apply) [ "${DRYFLAG:-}" != dry ] || die "--apply and --dry-run together; pick one."; DRY=0; DRYFLAG=apply; shift ;;
    --dry-run) [ "${DRYFLAG:-}" != apply ] || die "--apply and --dry-run together; pick one."; DRY=1; DRYFLAG=dry; shift ;;
    --resume) RESUME=1; shift ;;
    --dir) [ $# -ge 2 ] || die "--dir needs a value"; DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

case "$SHA" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
  *) die "give the reviewed commit with --sha <40 hex characters> (the guide has it)." ;;
esac
[ "${#SHA}" -eq 40 ] || die "--sha must be the full 40 hex characters."
case "$SHA" in *[!0-9a-f]*) die "--sha must be lowercase hex only." ;; esac

REPO="$DIR/repo"
WS="$REPO/workers/workspace"
STATE="$DIR/state"
LOGS="$DIR/logs"
SEEDD="$DIR/seed"
WRANGLER_BIN="${WRANGLER_BIN:-$REPO/workers/node_modules/.bin/wrangler}"
NEUTRAL="$DIR/neutral"
DB_ID=""; KV_ID=""; URL=""; CAMPAIGN_ID=""; ACCOUNT=""

# ---------- helpers ----------
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}
is_done() { [ -f "$STATE" ] && grep -qx "done $1" "$STATE"; }
mark()    { printf 'done %s\n' "$1" >> "$STATE"; }
kv_put()  { printf '%s=%s\n' "$1" "$2" >> "$STATE"; }
kv_get() {
  if [ -f "$STATE" ]; then sed -n "s/^$1=//p" "$STATE" | tail -n 1; fi
  return 0
}
skip_if_done() {
  if [ "$RESUME" -eq 1 ] && is_done "$1"; then say "Already finished earlier (--resume): skipping."; return 0; fi
  return 1
}

# Stop if wrangler output looks like a purchase or plan-upgrade offer.
cost_guard() {
  if printf '%s' "$1" | grep -Eqi 'purchase|payment|billing|paid plan|workers paid|credit card|upgrade (your|to)'; then
    die "Cloudflare output mentioned a purchase, payment or paid plan. This deploy must cost \$0. Do NOT accept anything; ask for help before going on."
  fi
}

# wr ARGS...: run wrangler in workers/workspace with no keyboard input; output captured into WR_OUT.
WR_OUT=""
wr() {
  local rc=0
  WR_OUT="$( cd "$WS" && "$WRANGLER_BIN" "$@" 2>&1 </dev/null )" || rc=$?
  cost_guard "$WR_OUT"
  return "$rc"
}
wr_neutral() {
  local rc=0
  WR_OUT="$( cd "$NEUTRAL" && "$WRANGLER_BIN" "$@" 2>&1 </dev/null )" || rc=$?
  cost_guard "$WR_OUT"
  return "$rc"
}
wr_must() {
  if ! wr "$@"; then printf '%s\n' "$WR_OUT" | tail -n 15 >&2; die "wrangler $1 $2 failed (last lines above)."; fi
}
json_objects() { tr -d '\n' | tr '}' '\n'; }
# jget MATCHKEY MATCHVALUE FIELD: from JSON on stdin, print FIELD of the object whose MATCHKEY is MATCHVALUE.
jget() { json_objects | grep -E "\"$1\": *\"$2\"" | grep -Eo "\"$3\": *\"[^\"]+\"" | sed 's/.*: *"//; s/"//' || true; }

# ---------- steps ----------
step_preflight() {
  step "1 of 12: preflight (tools, disk, reviewed commit)"
  if [ "$DRY" -eq 1 ]; then
    dry "check node >= 22, git, npm, openssl, scp, python3, sha256 tool"
    dry "check free disk >= ${MIN_KB} KB"
    dry "refuse unless this script's own checkout (if any) is clean and at $SHA"
    return 0
  fi
  local c
  for c in node git npm openssl scp python3 awk sed grep df; do
    command -v "$c" >/dev/null 2>&1 || die "$c is not installed. Install it and run again."
  done
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || die "no sha256 tool (sha256sum or shasum) found."
  local nv major
  nv="$(node --version)"; nv="${nv#v}"; major="${nv%%.*}"
  case "$major" in ''|*[!0-9]*) die "could not read the Node version ($nv)." ;; esac
  [ "$major" -ge 22 ] || die "Node $nv is too old; version 22 or newer is needed."
  say "Node $nv ok; git, npm, openssl, scp, python3 found."
  local parent kb
  parent="$(dirname "$DIR")"; [ -d "$parent" ] || die "the parent of $DIR does not exist."
  kb="$(df -Pk "$parent" | awk 'NR==2{print $4}')"
  case "$kb" in ''|*[!0-9]*) die "could not read free disk space." ;; esac
  [ "$kb" -ge "$MIN_KB" ] || die "only $kb KB free; need about $MIN_KB KB."
  say "Disk ok ($kb KB free)."
  local self_dir head
  self_dir="$(cd "$(dirname "$SELF")" && pwd)"
  if git -C "$self_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    head="$(git -C "$self_dir" rev-parse HEAD)"
    [ "$head" = "$SHA" ] || die "this checkout is at $head, not the reviewed commit $SHA. Use a fresh download."
    [ -z "$(git -C "$self_dir" status --porcelain)" ] || die "this checkout has changed or untracked files. Use a fresh download."
    say "This script's checkout is clean and at the reviewed commit."
  fi
  if [ "$RESUME" -eq 1 ]; then
    [ -f "$STATE" ] || die "--resume given but $STATE does not exist."
    [ "$(kv_get sha)" = "$SHA" ] || die "--resume must use the same reviewed commit as the first run (the saved one is $(kv_get sha))."
  else
    if [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
      die "$DIR already exists and is not empty. Run with --resume to continue, or pick a new folder with --dir."
    fi
    mkdir -p "$DIR"; chmod 700 "$DIR"
    : > "$STATE"; chmod 600 "$STATE"
    kv_put sha "$SHA"
  fi
  mkdir -p "$LOGS" "$NEUTRAL" "$SEEDD"
  APPLYING=1
}

# Runs on EVERY apply run (also --resume): clone at the reviewed commit, clean, and this script is the reviewed copy.
verify_repo() {
  [ -d "$REPO/.git" ] || die "$REPO is not a git clone."
  [ "$(git -C "$REPO" rev-parse HEAD)" = "$SHA" ] || die "the clone is not at the reviewed commit."
  [ -z "$(git -C "$REPO" status --porcelain)" ] || die "the clone has changed or untracked files. Nothing is deleted for you; start again with a new --dir."
  cmp -s "$SELF" "$REPO/tools/workspace_deploy.sh" || die "this script differs from the reviewed copy at that commit. Download it again from the guide's link."
}

step_clone() {
  step "2 of 12: fresh clone at the reviewed commit, install, and run the checks"
  if [ "$DRY" -eq 1 ]; then
    dry "git clone $REPO_URL $REPO"
    dry "git -C $REPO checkout --detach $SHA   (must be clean and equal $SHA)"
    dry "cmp this script with $REPO/tools/workspace_deploy.sh"
    dry "(cd $REPO/workers && npm ci && npm run check)"
    dry "wrangler version must equal the version pinned in workers/package.json"
    return 0
  fi
  if skip_if_done clone; then verify_repo; say "Reviewed commit, clean clone and script identity re-checked."; return 0; fi
  [ ! -e "$REPO" ] || die "$REPO already exists from an unfinished run. Pick a new folder with --dir (nothing is deleted for you)."
  say "Cloning (this is a new folder, never the AgentOrange repo)..."
  git clone --quiet "$REPO_URL" "$REPO" || die "git clone failed."
  git -C "$REPO" checkout --quiet --detach "$SHA" || die "the reviewed commit $SHA was not found in the clone."
  verify_repo
  say "Commit $SHA confirmed; this script matches the reviewed copy."
  say "Installing packages (npm ci) and running the checks; this takes a few minutes..."
  if ! ( cd "$REPO/workers" && npm ci --no-audit --no-fund ) >"$LOGS/npm-ci.log" 2>&1; then
    tail -n 15 "$LOGS/npm-ci.log" >&2; die "npm ci failed (log: $LOGS/npm-ci.log)."
  fi
  if ! ( cd "$REPO/workers" && npm run check ) >"$LOGS/npm-check.log" 2>&1; then
    tail -n 15 "$LOGS/npm-check.log" >&2; die "npm run check is not green. Do not deploy (log: $LOGS/npm-check.log)."
  fi
  say "npm run check: green."
  [ -x "$WRANGLER_BIN" ] || die "wrangler was not installed at $WRANGLER_BIN."
  local pinned actual
  pinned="$(sed -n 's/.*"wrangler": *"\([0-9][0-9.]*\)".*/\1/p' "$REPO/workers/package.json" | head -n 1)"
  [ -n "$pinned" ] || die "no exact pinned wrangler version in workers/package.json."
  actual="$( cd "$WS" && "$WRANGLER_BIN" --version 2>/dev/null </dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 )" || true
  [ "$actual" = "$pinned" ] || die "wrangler is $actual but the pinned version is $pinned."
  say "wrangler $actual matches the pinned version."
  mark clone
}

step_cost() {
  step "3 of 12: cost and plan check"
  say "This deploy must cost \$0: Workers Free plan, D1 free tier, KV free tier, Zero Trust Free plan (1 user)."
  say "If ANY Cloudflare screen or message offers a purchase, a payment method or a plan upgrade: do not accept; this script stops by itself on such wording."
  if [ "$DRY" -eq 1 ]; then dry "ask: type 'free' to confirm the dashboard shows Workers Free"; return 0; fi
  if skip_if_done cost; then return 0; fi
  say "In the Cloudflare dashboard, Workers & Pages > Plans should say 'Free'."
  ask "Type the word free once you have seen that (anything else stops): "
  [ "$REPLY" = "free" ] || die "plan not confirmed as Free."
  mark cost
}

step_login() {
  step "4 of 12: Cloudflare login (one browser click)"
  if [ "$DRY" -eq 1 ]; then
    dry "wrangler whoami"
    dry "wrangler login   (opens your browser; you click Allow)"
    dry "wrangler whoami  (prints the account name only)"
    return 0
  fi
  wr whoami || true
  if printf '%s' "$WR_OUT" | grep -qi 'not authenticated'; then
    say "Opening your browser. Click Allow, then come back here and wait."
    ( cd "$WS" && "$WRANGLER_BIN" login ) || die "wrangler login did not finish."
    wr whoami || die "wrangler whoami failed after login."
  fi
  printf '%s' "$WR_OUT" | grep -qi 'not authenticated' && die "still not logged in."
  local names count
  names="$(printf '%s\n' "$WR_OUT" | awk -F'│' 'NF>=3 && $2 !~ /Account Name/ && $2 ~ /[A-Za-z0-9]/ {gsub(/^ +| +$/,"",$2); if ($2!="") print $2}')"
  if [ -z "$names" ]; then die "logged in, but no account name could be read from wrangler whoami."; fi
  count="$(printf '%s\n' "$names" | wc -l | tr -d ' ')"
  [ "$count" -eq 1 ] || die "this login can reach $count Cloudflare accounts. Ask for help; the script will not guess which one."
  say "Logged in. Account name: $names"
  ask "Is that the DeFlock SLO Cloudflare account? Type yes: "
  [ "$REPLY" = "yes" ] || die "wrong account. Run 'wrangler logout' and start again."
  ACCOUNT="$names"
  if [ "$RESUME" -eq 1 ] && [ -n "$(kv_get account)" ] && [ "$(kv_get account)" != "$ACCOUNT" ]; then
    die "the account changed since the first run. Stopping."
  fi
  [ -n "$(kv_get account)" ] || kv_put account "$ACCOUNT"
}

# Identity check before anything destructive (runbook G-10): account, then name -> id three ways.
identity_check() {
  say "Identity check: account, then database name and id..."
  wr whoami || true
  printf '%s' "$WR_OUT" | grep -qF "$(kv_get account)" || die "the logged-in account is not the one confirmed earlier."
  wr_must d1 list --json
  local listed
  listed="$(printf '%s' "$WR_OUT" | jget name "$D1_NAME" uuid)"
  [ "$listed" = "$DB_ID" ] || die "database id mismatch: the account's $D1_NAME is not the id in $CFG."
  wr_must d1 info "$D1_NAME" -c "$CFG"
  printf '%s' "$WR_OUT" | grep -qF "$DB_ID" || die "d1 info does not show the id in $CFG."
  say "Identity check passed."
}

step_resources() {
  step "5 of 12: D1 database, KV namespace, and the local deploy config"
  if [ "$DRY" -eq 1 ]; then
    dry "wrangler d1 list --json ; wrangler d1 create $D1_NAME   (only if absent)"
    dry "wrangler kv namespace list ; wrangler kv namespace create $KV_TITLE   (only if absent)"
    dry "write $WS/$CFG from wrangler.deploy.example.jsonc with the two real ids (git-ignored, mode 0600)"
    return 0
  fi
  if skip_if_done resources; then DB_ID="$(kv_get db_id)"; KV_ID="$(kv_get kv_id)"; return 0; fi
  wr_neutral d1 list --json || { printf '%s\n' "$WR_OUT" | tail -n 10 >&2; die "wrangler d1 list failed."; }
  DB_ID="$(printf '%s' "$WR_OUT" | jget name "$D1_NAME" uuid)"
  if [ -z "$DB_ID" ]; then
    say "Creating D1 database $D1_NAME..."
    wr_neutral d1 create "$D1_NAME" || { printf '%s\n' "$WR_OUT" | tail -n 10 >&2; die "wrangler d1 create failed."; }
    wr_neutral d1 list --json || die "wrangler d1 list failed."
    DB_ID="$(printf '%s' "$WR_OUT" | jget name "$D1_NAME" uuid)"
  else
    say "D1 database $D1_NAME already exists; reusing it."
  fi
  case "$DB_ID" in
    *[!0-9a-f-]*|'') die "could not read a valid D1 database id." ;;
  esac
  [ "$(printf '%s\n' "$DB_ID" | wc -l | tr -d ' ')" -eq 1 ] || die "more than one database named $D1_NAME; refusing to guess."
  wr_neutral kv namespace list || { printf '%s\n' "$WR_OUT" | tail -n 10 >&2; die "wrangler kv namespace list failed."; }
  KV_ID="$(printf '%s' "$WR_OUT" | jget title "$KV_TITLE" id)"
  if [ -z "$KV_ID" ]; then
    say "Creating KV namespace $KV_TITLE..."
    wr_neutral kv namespace create "$KV_TITLE" || { printf '%s\n' "$WR_OUT" | tail -n 10 >&2; die "wrangler kv namespace create failed."; }
    wr_neutral kv namespace list || die "wrangler kv namespace list failed."
    KV_ID="$(printf '%s' "$WR_OUT" | jget title "$KV_TITLE" id)"
  else
    say "KV namespace $KV_TITLE already exists; reusing it."
  fi
  case "$KV_ID" in
    *[!0-9a-f]*|'') die "could not read a valid KV namespace id." ;;
  esac
  [ "$(printf '%s\n' "$KV_ID" | wc -l | tr -d ' ')" -eq 1 ] || die "more than one KV namespace titled $KV_TITLE; refusing to guess."
  git -C "$REPO" check-ignore -q "workers/workspace/$CFG" || die "$CFG is not git-ignored; refusing to write account values."
  sed -e "s/<D1_DATABASE_ID>/$DB_ID/" -e "s/<KV_NAMESPACE_ID>/$KV_ID/" "$WS/wrangler.deploy.example.jsonc" > "$WS/$CFG"
  chmod 600 "$WS/$CFG"
  ! grep -Eq '<(D1_DATABASE_ID|KV_NAMESPACE_ID)>' "$WS/$CFG" || die "the deploy config still has a placeholder."
  [ -z "$(git -C "$REPO" status --porcelain)" ] || die "the working tree is no longer clean after writing the config."
  say "Wrote workers/workspace/$CFG (private, git-ignored)."
  kv_put db_id "$DB_ID"; kv_put kv_id "$KV_ID"
  mark resources
}

step_migrations() {
  step "6 of 12: apply the database schema (migrations)"
  if [ "$DRY" -eq 1 ]; then
    dry "identity check (whoami, d1 list, d1 info)"
    dry "wrangler d1 migrations apply $D1_NAME --remote -c $CFG"
    return 0
  fi
  if skip_if_done migrations; then return 0; fi
  identity_check
  wr_must d1 migrations apply "$D1_NAME" --remote -c "$CFG"
  say "Migrations applied."
  mark migrations
}

step_seed() {
  step "7 of 12: fetch the seed from AgentOrange and approve it by its hash"
  if [ "$DRY" -eq 1 ]; then
    dry "scp $SEED_HOST:$SEED_DIR/seed.sql $SEED_HOST:$SEED_DIR/seed-summary.txt $SEED_HOST:$SEED_DIR/seed-review.txt $SEEDD/"
    dry "show seed-review.txt and the sha256; you type the first 8 characters to approve"
    dry "identity check, then wrangler d1 time-travel info, then d1 execute --remote --file seed.sql"
    dry "confirm campaign.external_sends is 'disabled'"
    return 0
  fi
  if skip_if_done seed; then CAMPAIGN_ID="$(kv_get campaign_id)"; return 0; fi
  say "Fetching three files over Tailscale (needs Tailscale on and your AgentOrange login)..."
  scp "$SEED_HOST:$SEED_DIR/seed.sql" "$SEED_HOST:$SEED_DIR/seed-summary.txt" "$SEED_HOST:$SEED_DIR/seed-review.txt" "$SEEDD/" || die "scp failed. Is Tailscale on?"
  local f
  for f in seed.sql seed-summary.txt seed-review.txt; do [ -s "$SEEDD/$f" ] || die "$f is missing or empty."; done
  chmod 600 "$SEEDD"/*
  local actual claimed
  actual="$(sha256_of "$SEEDD/seed.sql")"
  claimed="$(sed -n 's/^seed\.sql sha256: *//p' "$SEEDD/seed-review.txt" | head -n 1)"
  [ "$claimed" = "$actual" ] || die "seed.sql does not match the hash written in seed-review.txt. Nothing was applied."
  if grep -Eq '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' "$SEEDD/seed.sql" "$SEEDD/seed-review.txt"; then
    die "the seed contains something that looks like an email address. Nothing was applied."
  fi
  CAMPAIGN_ID="$(sed -n 's/^campaign_id: *//p' "$SEEDD/seed-summary.txt" | head -n 1)"
  case "$CAMPAIGN_ID" in ''|*[!A-Za-z0-9_-]*) die "could not read a valid campaign_id from seed-summary.txt." ;; esac
  grep -qF "$CAMPAIGN_ID" "$SEEDD/seed.sql" || die "the campaign_id in seed-summary.txt is not in seed.sql."
  say "------ seed-summary.txt ------"; cat "$SEEDD/seed-summary.txt"
  say "------ seed-review.txt (every text value that will be uploaded) ------"; cat "$SEEDD/seed-review.txt"
  say "------ end ------"
  say "seed.sql sha256: $actual"
  say "This is everything that will leave the box. Names of requesters, staff emails and keys are NOT in it."
  ask "To approve, type the first 8 characters of that sha256 (anything else stops): "
  [ "$REPLY" = "${actual:0:8}" ] || die "the typed hash does not match. Nothing was applied."
  kv_put campaign_id "$CAMPAIGN_ID"
  identity_check
  wr d1 execute "$D1_NAME" --remote -c "$CFG" --command "SELECT count(*) AS n FROM campaign" --json || die "could not read the database."
  if printf '%s' "$WR_OUT" | tr -d ' \n' | grep -Eq '"n":[1-9]'; then
    say "The database already holds a campaign; not seeding twice."
  else
    say "Recording a rollback point (time-travel bookmark) before the seed..."
    wr_must d1 time-travel info "$D1_NAME" -c "$CFG"
    printf '%s\n' "$WR_OUT" > "$DIR/d1-bookmark-before-seed.txt"; chmod 600 "$DIR/d1-bookmark-before-seed.txt"
    printf '%s\n' "$WR_OUT" | tail -n 5
    wr_must d1 execute "$D1_NAME" --remote -c "$CFG" --file "$SEEDD/seed.sql" --yes
    say "Seed applied."
  fi
  wr_must d1 execute "$D1_NAME" --remote -c "$CFG" --command "SELECT external_sends FROM campaign" --json
  if printf '%s' "$WR_OUT" | grep -q 'approval_required'; then die "campaign.external_sends is approval_required. Outbound sending must stay OFF."; fi
  printf '%s' "$WR_OUT" | grep -q 'disabled' || die "campaign.external_sends is not 'disabled'."
  say "Outbound sending is OFF (external_sends = disabled)."
  mark seed
}

step_deploy() {
  step "8 of 12: deploy the Worker"
  if [ "$DRY" -eq 1 ]; then dry "wrangler deploy -c $CFG"; return 0; fi
  if skip_if_done deploy; then URL="$(kv_get url)"; return 0; fi
  say "Deploying. If it says a workers.dev subdomain must be registered: Cloudflare dashboard > Workers & Pages > set your subdomain, then run this command again with --resume."
  local rc=0
  ( cd "$WS" && "$WRANGLER_BIN" deploy -c "$CFG" ) 2>&1 | tee "$LOGS/deploy.log" || rc=$?
  cost_guard "$(cat "$LOGS/deploy.log")"
  [ "$rc" -eq 0 ] || die "wrangler deploy failed."
  URL="$(grep -Eo "https://$WORKER_NAME\.[a-z0-9-]+\.workers\.dev" "$LOGS/deploy.log" | head -n 1 || true)"
  if [ -z "$URL" ]; then
    ask "Paste the https://$WORKER_NAME.<name>.workers.dev address shown above: "
    URL="$REPLY"
  fi
  printf '%s' "$URL" | grep -Eq "^https://$WORKER_NAME\.[a-z0-9-]+\.workers\.dev$" || die "that is not a $WORKER_NAME workers.dev address."
  say "Deployed: $URL (nothing can open it yet: no Access secrets, so every page refuses)."
  kv_put url "$URL"
  mark deploy
}

smoke() {  # smoke MODE
  local i=1 out rc
  while [ "$i" -le 6 ]; do
    rc=0; out="$(python3 "$REPO/tools/workspace_smoke.py" --mode "$1" "$URL" 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then printf '%s\n' "$out"; return 0; fi
    [ "$i" -lt 6 ] && { say "(check $i of 6 not passing yet; waiting ${SLEEP}s in case the address is still starting)"; sleep "$SLEEP"; }
    i=$((i + 1))
  done
  printf '%s\n' "$out"
  return 1
}

print_rollback() {
  cat <<EOF

ROLLBACK (fastest first; every command is run from $WS):
  1. Lock everyone out: Cloudflare dashboard > Zero Trust > Access > Applications > the app > Policies:
     remove the Allow rule (or add a Block for Everyone). Or: Workers & Pages > $WORKER_NAME >
     Settings > Domains & Routes > disable the workers.dev row.
  2. Remove the Worker version:  $WRANGLER_BIN deployments list -c $CFG
                                 $WRANGLER_BIN rollback <version-id> -c $CFG
     (or delete the Worker:     $WRANGLER_BIN delete -c $CFG)
  3. Undo the seed: use the bookmark in $DIR/d1-bookmark-before-seed.txt
                                 $WRANGLER_BIN d1 time-travel restore $D1_NAME --bookmark=<bookmark> -c $CFG
  4. The Tailscale copy of the console on AgentOrange (:8445) is untouched and still works.
EOF
}

step_appcheck() {
  step "9 of 12: check the Worker before Access (only /healthz may answer)"
  if [ "$DRY" -eq 1 ]; then dry "python3 tools/workspace_smoke.py --mode app <deployed url>"; return 0; fi
  if skip_if_done appcheck; then return 0; fi
  smoke app || { print_rollback; die "the pre-Access check FAILED."; }
  mark appcheck
}

TEAM=""; AUD=""; ORG=""
step_access() {
  step "10 of 12: Cloudflare Access (the login) - a few clicks in the dashboard"
  if [ "$DRY" -eq 1 ]; then
    dry "print the numbered dashboard clicks; wait for Return"
    dry "ask for team name, Application Audience tag, optional organizer emails"
    return 0
  fi
  if skip_if_done access; then TEAM="$(kv_get team)"; AUD="$(kv_get aud)"; ORG="$(kv_get org)"; return 0; fi
  cat <<EOF
Do these in your browser at https://dash.cloudflare.com :
  1. Left menu: Zero Trust. If it asks for a plan, choose Free (\$0). If it shows any purchase, stop.
  2. Zero Trust > Access > Applications > Add an application > Self-hosted.
  3. Name: DeFlock admin. Application domain: the host part of  $URL   (no https://).
  4. Session duration: 24 hours or less.
  5. Add ONE policy: Action = Allow; Include = Emails; type the email addresses allowed to sign in.
     No Everyone, no email domain, no Bypass, no Service Auth.
  6. Login method: One-time PIN.   Save the application.
  7. Open the application's Overview and copy the "Application Audience (AUD) Tag".
  8. Zero Trust > Settings > Custom Pages shows your team domain  <team>.cloudflareaccess.com. Note the <team> part.
EOF
  ask "Press Return when all eight are done... "
  ask "Team name (the <team> part, without .cloudflareaccess.com): "
  TEAM="${REPLY%.cloudflareaccess.com}"
  case "$TEAM" in ''|*[!a-z0-9-]*) die "the team name must be lowercase letters, digits and dashes." ;; esac
  ask "Application Audience (AUD) tag: "
  AUD="$REPLY"
  case "$AUD" in ''|*[!0-9a-f]*) die "the AUD tag should be a long string of hex characters." ;; esac
  [ "${#AUD}" -ge 32 ] || die "the AUD tag looks too short."
  ask "Optional: organizer emails as a comma list (a second lock behind Access), or just Return to skip: "
  ORG="$REPLY"
  if [ -n "$ORG" ]; then
    case "$ORG" in *[!A-Za-z0-9@._+,-]*) die "organizer list: letters, digits, @ . _ + - and commas only, no spaces." ;; esac
  fi
  kv_put team "$TEAM"; kv_put aud "$AUD"; kv_put org "$ORG"
  mark access
}

put_secret_stdin() {  # put_secret_stdin NAME  (value on stdin; never echoed)
  local rc=0
  ( cd "$WS" && "$WRANGLER_BIN" secret put "$1" -c "$CFG" ) >/dev/null 2>&1 || rc=$?
  return "$rc"
}

step_secrets() {
  step "11 of 12: secrets (values are never shown or saved; only 'set' / 'not set' is printed)"
  if [ "$DRY" -eq 1 ]; then
    dry "openssl rand -hex 32 | wrangler secret put DOWNLOAD_SIGNING_KEY -c $CFG"
    dry "wrangler secret put CAMPAIGN_ID / ACCESS_TEAM_DOMAIN / ACCESS_AUD / ORGANIZER_EMAILS(optional) -c $CFG"
    dry "wrangler secret list -c $CFG   (any sending secret present = stop)"
    return 0
  fi
  local name
  CAMPAIGN_ID="${CAMPAIGN_ID:-$(kv_get campaign_id)}"
  for name in DOWNLOAD_SIGNING_KEY CAMPAIGN_ID ACCESS_TEAM_DOMAIN ACCESS_AUD ORGANIZER_EMAILS; do
    if [ "$RESUME" -eq 1 ] && is_done "secret_$name"; then say "$name: already set (skipping)."; continue; fi
    case "$name" in
      DOWNLOAD_SIGNING_KEY) openssl rand -hex 32 | put_secret_stdin "$name" ;;
      CAMPAIGN_ID) printf '%s' "$CAMPAIGN_ID" | put_secret_stdin "$name" ;;
      ACCESS_TEAM_DOMAIN) printf '%s' "$TEAM" | put_secret_stdin "$name" ;;
      ACCESS_AUD) printf '%s' "$AUD" | put_secret_stdin "$name" ;;
      ORGANIZER_EMAILS)
        if [ -z "$ORG" ]; then say "$name: skipped (not set)."; continue; fi
        printf '%s' "$ORG" | put_secret_stdin "$name" ;;
    esac || die "$name: not set (wrangler secret put failed)."
    say "$name: set"
    mark "secret_$name"
  done
  wr_must secret list -c "$CFG"
  for name in RUNNER_TOKEN BREVO_API_KEY SUBSCRIBE_WEBHOOK_SECRET OUTBOX_GATEWAY_URL OUTBOX_SIGNING_KEY MASTODON_URL MASTODON_TOKEN LEGISTAR_FEEDS ACCESS_CERTS_JSON; do
    if printf '%s' "$WR_OUT" | grep -qF "\"$name\""; then
      die "$name is set on the Worker. Outbound sending and test hooks must stay OFF: delete it with 'wrangler secret delete $name -c $CFG'."
    fi
  done
  for name in DOWNLOAD_SIGNING_KEY CAMPAIGN_ID ACCESS_TEAM_DOMAIN ACCESS_AUD; do
    if printf '%s' "$WR_OUT" | grep -qF "\"$name\""; then say "check $name: set"; else die "check $name: not set"; fi
  done
  say "No sending secrets present. Outbound stays OFF."
}

step_edge() {
  step "12 of 12: final check from the outside (every path must be refused without a login)"
  if [ "$DRY" -eq 1 ]; then dry "python3 tools/workspace_smoke.py --mode edge <deployed url>"; return 0; fi
  if skip_if_done edge; then return 0; fi
  if ! smoke edge; then
    say "FAIL: a page answered without a login, so Access is NOT in front of the Worker."
    print_rollback
    die "the edge check FAILED. Lock everyone out with rollback step 1 now."
  fi
  mark edge
}

step_final() {
  step "done"
  if [ "$DRY" -eq 1 ]; then
    say "This was a dry run: nothing was executed. Add --apply to do it for real."
    return 0
  fi
  cat <<EOF

DONE. The admin console is live at:
  $URL
What you should see first:
  - Cloudflare's sign-in page (not the console). Enter an allowed email, get a one-time PIN by email, enter it.
  - Then the console opens on "Waiting on you". Settings should say outbound sending is OFF.
  - A different email, not on the policy, must be refused.
Data in it is a one-time snapshot (seed); it does not update itself. Nothing can send email from it.
EOF
  print_rollback
}

# ---------- run ----------
if [ "$DRY" -eq 1 ]; then
  say "DRY RUN (the default): every command is printed, nothing is executed, no folder is made."
  say "Reviewed commit: $SHA   Work folder: $DIR"
else
  say "Reviewed commit: $SHA   Work folder: $DIR"
fi
step_preflight
step_clone
step_cost
step_login
step_resources
step_migrations
step_seed
step_deploy
step_appcheck
step_access
step_secrets
step_edge
step_final
