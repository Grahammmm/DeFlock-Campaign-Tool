"""PUBLISH_PATHS: one entry for every code path that emits an artefact a member of the public could read, or that feeds one.

Plain data, no behaviour. tests/test_publish_path_coverage.py drives each entry with a synthetic email, phone and plate and
proves it refuses; fails on an emitting module, an emitting function or a Worker emission site that is not named here; and fails
on a second copy of the patterns. Add the entry for a new output path in the same change that adds the path.

What this does NOT prove: the patterns have deliberate blind spots (bare 10-digit runs and plain-word emails are not matched),
so a registered path that passes the lint can still carry personal data. The registry covers paths, not detection. It sits under
the human publish gate and BF-df-redaction's sign-off, never in place of them.

Fields: id; module (repo path); entry_point; artefact_kind (page|feed|card|report|brief|draft|post|export); audience (public|pre-public);
lint (privacy|privacy+legal: privacy refuses, the legal-claim guard only flags and never refuses; upstream: the path sends text
another registered path already linted, named by inherits); redaction_required; marker (text, or a tuple of texts, the module source
must keep: each lint call); driver (how the test feeds it); inherits (id of the path that lints the text, or None); note.

EMISSION_SITES binds every Python function that writes a file inside a registered module to a registry id. WORKER_EMISSIONS pins how
many Worker (TypeScript) sends, uploads and serves each file makes. A new writer or send fails the test until it is added here.
"""


def _entry(id, module, entry_point, kind, audience, lint, marker, driver, note, redaction=True, inherits=None):
    return {"id": id, "module": module, "entry_point": entry_point, "artefact_kind": kind, "audience": audience, "lint": lint,
            "redaction_required": redaction, "marker": marker, "driver": driver, "inherits": inherits, "note": note}


GATE = "_privacy_gate(model, cfg)"
PUBLISH_PATHS = (
    _entry("site-pages", "campaign_tool/site.py", "_plan_site", "page", "public", "privacy+legal", GATE, "site",
           "every HTML page; gate runs on the content model, site.json, sources and agencies before anything renders"),
    _entry("atom-feed", "campaign_tool/site.py", "Site.feed_xml", "feed", "public", "privacy+legal", GATE, "site", "feed.xml"),
    _entry("agency-feeds", "campaign_tool/site.py", "Site.agency_feeds", "feed", "public", "privacy+legal", GATE, "site",
           "findings/agency/<slug>.xml"),
    _entry("corrections-feed", "campaign_tool/site.py", "Site.corrections_feed_xml", "feed", "public", "privacy+legal", GATE, "site",
           "corrections.xml"),
    _entry("events-calendar", "campaign_tool/site.py", "Site.events_ics", "feed", "public", "privacy", GATE, "site", "events.ics"),
    _entry("open-graph-meta", "campaign_tool/site.py", "Site.finding_html", "page", "public", "privacy+legal", GATE, "site",
           "og:/twitter: meta tags on each finding page"),
    _entry("agency-cards", "campaign_tool/agency_cards.py", "render", "card", "public", "privacy+legal", GATE, "site",
           "agency-cards.js, built inside the site plan"),
    _entry("data-exports", "campaign_tool/site.py", "Site.findings_json", "export", "public", "privacy+legal", GATE, "site",
           "data/findings.json, .csv, sources, meetings, agencies"),
    _entry("map-geojson", "campaign_tool/site.py", "_plan_site", "export", "public", "privacy", GATE, "site",
           "camera, city and county GeoJSON copies; all decoded JSON text is linted before any write"),
    _entry("runner-build-site", "runner/handlers/build_site.py", "run", "page", "public", "privacy+legal", "build_site(root)", "runner",
           "calls campaign_tool.site.build_site, so it inherits site-pages; a refusal fails the job and uploads nothing"),
    _entry("newsletter-draft", "campaign_tool/newsletter/draft.py", "build_draft", "draft", "pre-public", "privacy+legal",
           ("_EMAIL.search(blob)", "_PHONE.search(blob)", '"plate" in categories(blob)'), "newsletter",
           "pre-public: the draft is linted as the last step before it is returned"),
    _entry("social-drafts", "campaign_tool/social.py", "_write_drafts", "draft", "pre-public", "privacy+legal", "refusal(", "social",
           "kit/social/: finding, meeting, outcome and campaign-event drafts; nothing under public/"),
    _entry("social-post", "workers/workspace/src/executors/post_social.ts", "privacyHits", "post", "public", "privacy",
           "privacyHits(text)", "worker", "the Worker's per-post lint; its patterns live in social_adapter.ts", redaction=False),
    _entry("flock-report", "tools/flock_report.py", "lint", "report", "pre-public", "privacy+legal", "privacy.", "report",
           "private report lint"),
    _entry("flock-page", "tools/flock_page.py", "lint", "page", "pre-public", "privacy+legal", "privacy.", "page", "story page lint"),
    _entry("flock-publish", "tools/flock_publish.py", "finding_errors", "page", "pre-public", "privacy+legal", "text_rules", "publish",
           "PUBLISH.json finding text rules"),
    _entry("flock-publish-brief", "tools/flock_publish_brief.py", "brief", "brief", "pre-public", "privacy",
           ("publish.privacy.EMAIL.search(body)", "publish.privacy.PHONE.search(body)", "publish.privacy.PLATE.search(body)"), "brief",
           "agency brief; email, phone and plate"),
    _entry("flock-publish-stage", "tools/flock_publish_stage.py", "stage", "page", "pre-public", "privacy+legal", "text_rules", "stage",
           "staged site text"),
    _entry("flock-email", "tools/flock_email.py", "lint", "draft", "pre-public", "privacy", "privacy.categories", "email",
           "outbound-email draft rules; never sent by this repo"),
    _entry("agency-brief", "campaign_tool/agency_brief.py", "build_brief", "brief", "pre-public", "privacy+legal",
           ("refusal(", "legal_notes("), "agency_brief",
           "kit/briefs/: private printable meeting brief from published content; linted per row and on both outputs before any write"),
    _entry("meeting-comment-publish", "campaign_tool/meeting_comment_publish.py", "publish", "page", "pre-public", "privacy+legal",
           "comment_text_problems(", "meeting_comment",
           "content/meetings/<id>.json submitted_comment: the exact recorded text, linted per line before the one atomic write; "
           "the site-pages gate covers the final page"),
    _entry("readiness-snapshot", "campaign_tool/cli.py", "readiness", "report", "pre-public", "privacy", "categories(sentence)",
           "readiness", "readiness JSON: next-action sentences are linted"),
    _entry("worker-site-upload", "workers/workspace/src/runner.ts", "PUBLIC_BUCKET.put", "page", "public", "upstream",
           "PUBLIC_BUCKET.put(", "upstream", "stores the files runner-build-site built and scanned", inherits="runner-build-site"),
    _entry("worker-deploy-site", "workers/workspace/src/executors/deploy_site.ts", "CACHE.put", "page", "public", "upstream",
           'CACHE.put("site_version"', "upstream", "moves the site_version pointer to files already stored",
           inherits="worker-site-upload"),
    _entry("worker-public-serve", "workers/public-site/src/index.ts", "fetch", "page", "public", "upstream", "object.body", "upstream",
           "serves the stored files read-only", inherits="worker-site-upload"),
    _entry("worker-send-mail", "workers/workspace/src/executors/send_mail.ts", "sender.send", "draft", "public", "upstream",
           "sender.send(", "upstream", "sends the approved proposal text; no Worker-side privacy lint, the draft path lints",
           inherits="flock-email"),
    _entry("worker-outbox", "workers/workspace/src/executors/outbox_sender.ts", "fetcher", "draft", "public", "upstream",
           "this.fetcher(", "upstream", "outbound transport for the approved proposal text", inherits="flock-email"),
    _entry("worker-brevo", "workers/workspace/src/brevo.ts", "call", "draft", "public", "upstream", '"POST"', "upstream",
           "newsletter transport for the approved issue", inherits="newsletter-draft"),
    _entry("worker-social-adapter", "workers/workspace/src/executors/social_adapter.ts", "post", "post", "public", "upstream",
           'method: "POST"', "upstream", "the platform call, reached only through post_social after privacyHits", inherits="social-post"),
)

# (registry id, module, function): every function that writes a file inside a registered Python module.
EMISSION_SITES = (
    ("site-pages", "campaign_tool/site.py", "build_site"), ("site-pages", "campaign_tool/site.py", "_write"),
    ("site-pages", "campaign_tool/site.py", "_copy"),
    ("newsletter-draft", "campaign_tool/newsletter/draft.py", "write_draft"),
    ("social-drafts", "campaign_tool/social.py", "_write_drafts"),
    ("agency-brief", "campaign_tool/agency_brief.py", "_write_file"),
    ("meeting-comment-publish", "campaign_tool/meeting_comment_publish.py", "publish"),
    ("readiness-snapshot", "campaign_tool/cli.py", "readiness"),
    ("flock-email", "tools/flock_email.py", "draft"),
    ("flock-page", "tools/flock_page.py", "build"),
    ("flock-publish", "tools/flock_publish.py", "export"), ("flock-publish", "tools/flock_publish.py", "recheck"),
    ("flock-publish", "tools/flock_publish.py", "change"),
    ("flock-publish-brief", "tools/flock_publish_brief.py", "link"),
    ("flock-publish-stage", "tools/flock_publish_stage.py", "link"), ("flock-publish-stage", "tools/flock_publish_stage.py", "stage"),
    ("flock-report", "tools/flock_report.py", "build"), ("flock-report", "tools/flock_report.py", "region"),
    ("flock-report", "tools/flock_report.py", "write"),
)

# file: (registry id, number of matches of the Worker emission pattern in that file)
WORKER_EMISSIONS = {
    "workers/workspace/src/runner.ts": ("worker-site-upload", 1),
    "workers/workspace/src/executors/deploy_site.ts": ("worker-deploy-site", 1),
    "workers/public-site/src/index.ts": ("worker-public-serve", 1),
    "workers/workspace/src/executors/send_mail.ts": ("worker-send-mail", 1),
    "workers/workspace/src/executors/outbox_sender.ts": ("worker-outbox", 2),
    "workers/workspace/src/brevo.ts": ("worker-brevo", 4),
    "workers/workspace/src/executors/social_adapter.ts": ("worker-social-adapter", 1),
}
