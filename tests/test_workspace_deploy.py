"""ADM-DEPLOY-SCRIPT-1: tools/workspace_deploy.sh against fake wrangler/scp/npm/node binaries.

Nothing here touches Cloudflare, the network or a real secret. The fakes record every call in a log.
"""
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "workspace_deploy.sh"
TEMPLATE = ROOT / "workers" / "workspace" / "wrangler.deploy.example.jsonc"
AUD = "ab" * 32
D1_UUID = "11111111-2222-3333-4444-555555555555"
KV_ID = "0123456789abcdef0123456789abcdef"

FAKE_NODE = "#!/bin/sh\necho v22.1.0\n"
FAKE_NPM = '''#!/bin/sh
echo "npm $*" >> "$FAKE_LOG"
if [ "$1" = ci ] && [ -n "$FAKE_NPM_FAIL_CI" ]; then exit 1; fi
if [ "$1" = run ] && [ -n "$FAKE_NPM_FAIL_CHECK" ]; then exit 1; fi
exit 0
'''
FAKE_SCP = '''#!/bin/sh
echo "scp $*" >> "$FAKE_LOG"
for a in "$@"; do last="$a"; done
for a in "$@"; do
  case "$a" in *:*) cp "$FAKE_SEED_SRC/$(basename "${a#*:}")" "$last/" ;; esac
done
'''
FAKE_WRANGLER = '''#!/bin/sh
echo "wrangler $*" >> "$FAKE_LOG"
S="$FAKE_STATE"
case "$1" in
  --version) echo "wrangler 4.145.0"; exit 0 ;;
  whoami)
    if [ -f "$S/login" ]; then
      echo "You are logged in with an OAuth Token."
      echo "┌──────────────────┬────────────┐"
      echo "│ Account Name     │ Account ID │"
      echo "├──────────────────┼────────────┤"
      echo "│ Test Account     │ acct123    │"
      echo "└──────────────────┴────────────┘"
    else echo "You are not authenticated. Please run wrangler login."; fi
    exit 0 ;;
  login) touch "$S/login"; exit 0 ;;
  deploy)
    echo "Uploaded deflock-workspace"; echo "  https://deflock-workspace.testsub.workers.dev"; exit 0 ;;
esac
if [ "$1 $2" = "d1 list" ]; then
  if [ -n "$FAKE_COST" ]; then echo "Please upgrade your plan"; exit 0; fi
  if [ -f "$S/d1" ]; then printf '[\\n {\\n "uuid": "%s",\\n "name": "deflock-campaign"\\n }\\n]\\n' "$FAKE_D1_UUID"; else echo "[]"; fi
  exit 0
fi
if [ "$1 $2" = "d1 create" ]; then touch "$S/d1"; exit 0; fi
if [ "$1 $2" = "d1 info" ]; then
  if [ -n "$FAKE_INFO_WRONG" ]; then echo "uuid 99999999-0000-0000-0000-000000000000"; else echo "uuid $FAKE_D1_UUID"; fi
  exit 0
fi
if [ "$1 $2 $3" = "kv namespace list" ]; then
  if [ -f "$S/kv" ]; then printf '[{"id": "%s", "title": "deflock-workspace-CACHE"}]\\n' "$FAKE_KV_ID"; else echo "[]"; fi
  exit 0
fi
if [ "$1 $2 $3" = "kv namespace create" ]; then touch "$S/kv"; exit 0; fi
if [ "$1 $2" = "d1 time-travel" ]; then echo "bookmark 00000001-aaaa"; exit 0; fi
if [ "$1 $2" = "d1 migrations" ]; then echo "applied"; exit 0; fi
if [ "$1 $2" = "d1 execute" ]; then
  case "$*" in
    *"--file"*) touch "$S/seeded"; echo ok ;;
    *"count(*)"*) if [ -f "$S/seeded" ]; then echo '[{"results":[{"n": 1}]}]'; else echo '[{"results":[{"n": 0}]}]'; fi ;;
    *external_sends*) echo '[{"results":[{"external_sends": "disabled"}]}]' ;;
  esac
  exit 0
fi
if [ "$1 $2" = "secret put" ]; then
  v="$(cat)"; echo "$3=$v" >> "$FAKE_SECRETS"; echo "$3" >> "$S/secret-names"
  echo "Success! Uploaded secret $3 value $v"; exit 0
fi
if [ "$1 $2" = "secret list" ]; then
  printf '['; sep=''
  if [ -f "$S/secret-names" ]; then for n in $(cat "$S/secret-names"); do printf '%s{"name": "%s", "type": "secret_text"}' "$sep" "$n"; sep=','; done; fi
  if [ -n "$FAKE_EXTRA_SECRET" ]; then printf '%s{"name": "%s", "type": "secret_text"}' "$sep" "$FAKE_EXTRA_SECRET"; fi
  echo ']'; exit 0
fi
exit 0
'''
FAKE_SMOKE = '''import os, sys
mode = sys.argv[sys.argv.index("--mode") + 1]
open(os.environ["FAKE_LOG"], "a").write("smoke %s %s\\n" % (mode, sys.argv[-1]))
bad = os.environ.get("FAKE_SMOKE_" + mode.upper())
for p in ("/", "/healthz", "/api/me"):
    print(("FAIL " if bad else "ok   ") + p)
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
'''


def write_exec(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class Fixture:
    def __init__(self, tc):
        self.tmp = Path(tempfile.mkdtemp(prefix="wsdeploy-"))
        tc.addCleanup(shutil.rmtree, self.tmp, True)
        t = self.tmp
        self.repo = t / "origin"
        (self.repo / "workers" / "workspace").mkdir(parents=True)
        (self.repo / "tools").mkdir()
        (self.repo / "workers" / "package.json").write_text('{"devDependencies": {"wrangler": "4.145.0"}}\n')
        shutil.copy(TEMPLATE, self.repo / "workers" / "workspace" / "wrangler.deploy.example.jsonc")
        (self.repo / ".gitignore").write_text("workers/workspace/wrangler.deploy.jsonc\n")
        shutil.copy(SCRIPT, self.repo / "tools" / "workspace_deploy.sh")
        (self.repo / "tools" / "workspace_smoke.py").write_text(FAKE_SMOKE)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "fixture")
        self.sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, stdout=subprocess.PIPE,
                                  text=True).stdout.strip()
        self.bin = t / "bin"
        self.bin.mkdir()
        for name, body in (("node", FAKE_NODE), ("npm", FAKE_NPM), ("scp", FAKE_SCP)):
            write_exec(self.bin / name, body)
        self.wrangler = t / "fake-wrangler"
        write_exec(self.wrangler, FAKE_WRANGLER)
        self.seed = t / "seedsrc"
        self.seed.mkdir()
        self.write_seed("INSERT INTO campaign (campaign_id,name) VALUES ('camp0123456789','DeFlock SLO');\n")
        self.state = t / "fstate"
        self.state.mkdir()
        self.log = t / "calls.log"
        self.log.write_text("")
        self.secrets = t / "secrets.txt"
        self.secrets.write_text("")
        self.dl = t / "dl"
        self.dl.mkdir()
        self.script = self.dl / "workspace_deploy.sh"
        shutil.copy(SCRIPT, self.script)
        self.dir = t / "work"
        self.home = t / "home"
        self.home.mkdir()

    def write_seed(self, sql, claimed=None):
        (self.seed / "seed.sql").write_text(sql)
        digest = hashlib.sha256(sql.encode()).hexdigest()
        self.digest = digest
        (self.seed / "seed-review.txt").write_text("seed.sql sha256: %s\nagency.name [1] = Example County\n" % (claimed or digest))
        (self.seed / "seed-summary.txt").write_text("workspace seed summary\ncampaign_id: camp0123456789\n")

    def env(self, **extra):
        e = dict(os.environ)
        e.update({
            "PATH": "%s:%s" % (self.bin, os.environ["PATH"]), "HOME": str(self.home),
            "WRANGLER_BIN": str(self.wrangler), "DEPLOY_REPO_URL": str(self.repo), "DEPLOY_SLEEP": "0",
            "DEPLOY_SEED_HOST": "fake@host", "DEPLOY_SEED_DIR": "/seed/out",
            "FAKE_LOG": str(self.log), "FAKE_STATE": str(self.state), "FAKE_SECRETS": str(self.secrets),
            "FAKE_SEED_SRC": str(self.seed), "FAKE_D1_UUID": D1_UUID, "FAKE_KV_ID": KV_ID,
        })
        e.update(extra)
        return e

    def answers(self, prefix=None, org="boss@example.invalid"):
        return "free\nyes\n%s\n\nteamx\n%s\n%s\n" % (prefix or self.digest[:8], AUD, org)

    def run(self, *args, stdin="", sha=None, script=None, env=None):
        cmd = ["bash", str(script or self.script), "--sha", sha or self.sha, "--dir", str(self.dir), *args]
        return subprocess.run(cmd, input=stdin, text=True, capture_output=True, env=env or self.env(), timeout=120,
                              cwd=str(self.dl))

    def calls(self):
        return [l for l in self.log.read_text().splitlines() if l]


class DeployScriptTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture(self)

    def apply_ok(self, **kw):
        r = self.f.run("--apply", stdin=self.f.answers(), **kw)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_dry_run_is_default_and_executes_nothing(self):
        r = self.f.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertIn("[dry-run] would run", r.stdout)
        self.assertEqual(self.f.calls(), [])
        self.assertFalse(self.f.dir.exists())
        self.assertEqual(self.f.secrets.read_text(), "")
        r2 = self.f.run("--dry-run", stdin="")
        self.assertEqual(r2.returncode, 0)
        self.assertEqual(self.f.calls(), [])

    def test_apply_and_dry_run_together_refused(self):
        r = self.f.run("--apply", "--dry-run")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.f.calls(), [])

    def test_bad_sha_format_refused(self):
        r = self.f.run("--apply", sha="main")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.f.calls(), [])

    def test_full_run_order_and_files(self):
        r = self.apply_ok()
        calls = self.f.calls()

        def first(prefix):
            for i, c in enumerate(calls):
                if c.startswith(prefix):
                    return i
            self.fail("no call starting with %r in %s" % (prefix, calls))
        order = [first(p) for p in (
            "npm ci", "npm run check", "wrangler login", "wrangler d1 create", "wrangler kv namespace create",
            "wrangler d1 migrations apply", "scp ", "wrangler d1 time-travel", "wrangler d1 execute deflock-campaign --remote -c wrangler.deploy.jsonc --file",
            "wrangler deploy", "smoke app", "wrangler secret put", "smoke edge")]
        self.assertEqual(order, sorted(order), calls)
        for c in calls:
            if c.startswith("wrangler d1 ") and not c.startswith(("wrangler d1 list", "wrangler d1 create")):
                self.assertIn("-c wrangler.deploy.jsonc", c)
            if c.startswith("wrangler secret"):
                self.assertIn("-c wrangler.deploy.jsonc", c)
        scp = [c for c in calls if c.startswith("scp ")]
        self.assertEqual(len(scp), 1)
        for name in ("seed.sql", "seed-summary.txt", "seed-review.txt"):
            self.assertIn("fake@host:/seed/out/" + name, scp[0])
        self.assertEqual(scp[0].count("fake@host:"), 3)
        self.assertNotIn("*", scp[0])
        self.assertTrue([c for c in calls if "d1 migrations apply deflock-campaign --remote" in c])
        cfg = self.f.dir / "repo" / "workers" / "workspace" / "wrangler.deploy.jsonc"
        text = cfg.read_text()
        self.assertIn(D1_UUID, text)
        self.assertIn(KV_ID, text)
        self.assertNotRegex(text, r"<(D1_DATABASE_ID|KV_NAMESPACE_ID)>")
        self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)
        state = self.f.dir / "state"
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        self.assertIn("https://deflock-workspace.testsub.workers.dev", r.stdout)
        self.assertIn("Account name: Test Account", r.stdout)
        self.assertIn("ROLLBACK", r.stdout)
        self.assertIn("PASS", r.stdout)
        secret_names = [l.split("=")[0] for l in self.f.secrets.read_text().splitlines()]
        self.assertEqual(sorted(secret_names), sorted(["DOWNLOAD_SIGNING_KEY", "CAMPAIGN_ID", "ACCESS_TEAM_DOMAIN", "ACCESS_AUD", "ORGANIZER_EMAILS"]))
        for bad in ("BREVO_API_KEY", "RUNNER_TOKEN", "OUTBOX", "MASTODON"):
            self.assertNotIn(bad, self.f.secrets.read_text())

    def test_no_secret_in_output_state_or_files(self):
        r = self.apply_ok()
        values = dict(l.split("=", 1) for l in self.f.secrets.read_text().splitlines())
        key = values["DOWNLOAD_SIGNING_KEY"]
        self.assertGreaterEqual(len(key), 64)  # 32 bytes as hex
        self.assertRegex(key, r"^[0-9a-f]+$")
        self.assertIn("DOWNLOAD_SIGNING_KEY: set", r.stdout)
        out = r.stdout + r.stderr
        self.assertNotIn(key, out)
        for call in self.f.calls():
            self.assertNotIn(key, call)
        for p in self.f.dir.rglob("*"):
            if p.is_file() and "node_modules" not in p.parts and ".git" not in p.parts:
                self.assertNotIn(key.encode(), p.read_bytes(), str(p))
        # the AUD tag and organizer list are also passed by stdin only
        for call in self.f.calls():
            self.assertNotIn(AUD, call)
            self.assertNotIn("boss@example.invalid", call)
        self.assertNotIn(AUD, out)
        self.assertNotIn("boss@example.invalid", out)

    def test_hash_mismatch_typed_refuses_before_applying(self):
        r = self.f.run("--apply", stdin=self.f.answers(prefix="deadbeef"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not match", r.stdout + r.stderr)
        self.assertFalse([c for c in self.f.calls() if "--file" in c])
        self.assertFalse([c for c in self.f.calls() if c.startswith("wrangler secret")])
        self.assertFalse([c for c in self.f.calls() if c.startswith("wrangler deploy")])

    def test_seed_that_differs_from_its_review_hash_refused(self):
        self.f.write_seed("INSERT INTO campaign (campaign_id,name) VALUES ('camp0123456789','X');\n", claimed="0" * 64)
        r = self.f.run("--apply", stdin=self.f.answers())
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse([c for c in self.f.calls() if "--file" in c])

    def test_seed_with_email_refused(self):
        self.f.write_seed("INSERT INTO agency (n) VALUES ('person@example.invalid');\n-- camp0123456789\n")
        r = self.f.run("--apply", stdin=self.f.answers())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("email", r.stdout + r.stderr)
        self.assertFalse([c for c in self.f.calls() if "--file" in c])

    def test_unknown_commit_refused_before_any_wrangler_call(self):
        r = self.f.run("--apply", stdin=self.f.answers(), sha="a" * 40)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse([c for c in self.f.calls() if c.startswith("wrangler")])
        self.assertFalse([c for c in self.f.calls() if c.startswith("npm")])

    def test_tampered_script_refused(self):
        tampered = self.f.dl / "tampered.sh"
        tampered.write_text(SCRIPT.read_text() + "\n# extra\n")
        r = self.f.run("--apply", stdin=self.f.answers(), script=tampered)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("differs", r.stdout + r.stderr)
        self.assertFalse([c for c in self.f.calls() if c.startswith(("wrangler", "npm"))])

    def test_checkout_at_other_commit_or_dirty_refused(self):
        checkout = self.f.tmp / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.f.repo), str(checkout)], check=True)
        script = checkout / "tools" / "workspace_deploy.sh"
        (checkout / "NEW.txt").write_text("x")
        r = self.f.run("--apply", stdin=self.f.answers(), script=script)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("changed or untracked", r.stdout + r.stderr)
        (checkout / "NEW.txt").unlink()
        git(checkout, "commit", "-q", "--allow-empty", "-m", "later")
        r = self.f.run("--apply", stdin=self.f.answers(), script=script)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not the reviewed commit", r.stdout + r.stderr)
        self.assertEqual(self.f.calls(), [])

    def test_resume_skips_finished_steps(self):
        env = self.f.env(FAKE_SMOKE_EDGE="1")
        r1 = self.f.run("--apply", stdin=self.f.answers(), env=env)
        self.assertNotEqual(r1.returncode, 0)
        self.assertIn("ROLLBACK", r1.stdout)
        self.assertIn("FAIL", r1.stdout)
        before = self.f.calls()
        r2 = self.f.run("--apply", "--resume", stdin="yes\n")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        after = self.f.calls()[len(before):]
        self.assertIn("skipping", r2.stdout)
        for forbidden in ("npm ci", "scp ", "wrangler d1 create", "wrangler kv namespace create", "wrangler deploy",
                          "wrangler secret put", "wrangler d1 migrations", "smoke app"):
            self.assertFalse([c for c in after if c.startswith(forbidden)], (forbidden, after))
        self.assertFalse([c for c in after if "--file" in c])
        self.assertTrue([c for c in after if c.startswith("smoke edge")])
        self.assertIn("DONE", r2.stdout)

    def test_resume_without_state_and_rerun_without_resume_refused(self):
        r = self.f.run("--apply", "--resume", stdin="")
        self.assertNotEqual(r.returncode, 0)
        self.apply_ok()
        r = self.f.run("--apply", stdin=self.f.answers())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--resume", r.stdout + r.stderr)

    def test_existing_resources_are_reused_and_identity_checked(self):
        (self.f.state / "d1").touch()
        (self.f.state / "kv").touch()
        self.apply_ok()
        calls = self.f.calls()
        self.assertFalse([c for c in calls if c.startswith(("wrangler d1 create", "wrangler kv namespace create"))])
        info = [i for i, c in enumerate(calls) if c.startswith("wrangler d1 info")]
        mig = [i for i, c in enumerate(calls) if c.startswith("wrangler d1 migrations")]
        self.assertTrue(info and mig and info[0] < mig[0])

    def test_identity_mismatch_stops_before_migrations(self):
        r = self.f.run("--apply", stdin=self.f.answers(), env=self.f.env(FAKE_INFO_WRONG="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse([c for c in self.f.calls() if "migrations apply" in c or "--file" in c])

    def test_purchase_wording_stops(self):
        (self.f.state / "d1").touch()
        r = self.f.run("--apply", stdin=self.f.answers(), env=self.f.env(FAKE_COST="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("purchase", r.stdout + r.stderr)
        self.assertFalse([c for c in self.f.calls() if "migrations apply" in c])

    def test_cost_confirmation_required(self):
        r = self.f.run("--apply", stdin="paid\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse([c for c in self.f.calls() if c.startswith("wrangler login")])

    def test_wrong_account_answer_stops(self):
        r = self.f.run("--apply", stdin="free\nno\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse([c for c in self.f.calls() if c.startswith("wrangler d1")])

    def test_sending_secret_present_stops(self):
        r = self.f.run("--apply", stdin=self.f.answers(), env=self.f.env(FAKE_EXTRA_SECRET="BREVO_API_KEY"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("BREVO_API_KEY", r.stdout + r.stderr)
        self.assertFalse([c for c in self.f.calls() if c.startswith("smoke edge")])

    def test_organizer_list_can_be_skipped(self):
        r = self.f.run("--apply", stdin=self.f.answers(org=""))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("ORGANIZER_EMAILS", self.f.secrets.read_text())

    def test_pre_access_check_failure_stops_with_rollback(self):
        r = self.f.run("--apply", stdin=self.f.answers(), env=self.f.env(FAKE_SMOKE_APP="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ROLLBACK", r.stdout)
        self.assertFalse([c for c in self.f.calls() if c.startswith("wrangler secret")])

    def test_npm_check_red_stops(self):
        r = self.f.run("--apply", stdin=self.f.answers(), env=self.f.env(FAKE_NPM_FAIL_CHECK="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse([c for c in self.f.calls() if c.startswith("wrangler")])


class StaticTests(unittest.TestCase):
    def test_syntax_and_portability(self):
        text = SCRIPT.read_text()
        self.assertTrue(text.startswith("#!/usr/bin/env bash\n"))
        self.assertIn("set -euo pipefail", text)
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
        for banned in ("declare -A", "mapfile", "readarray", "${", ",,}"):
            if banned == "${":
                continue
            self.assertNotIn(banned, text)  # bash 3.2 on macOS
        self.assertNotIn("sed -i", text)
        self.assertNotIn("stat -c", text)

    def test_shellcheck_when_available(self):
        sc = shutil.which("shellcheck")
        if not sc:
            self.skipTest("shellcheck not installed")
        r = subprocess.run([sc, "-x", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
