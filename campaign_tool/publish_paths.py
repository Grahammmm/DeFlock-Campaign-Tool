"""PUBLISH_PATHS: one entry for every code path that emits an artefact a member of the public could read, or that feeds one.

Plain data, no behaviour. tests/test_publish_path_coverage.py proves each entry runs the shared privacy lint
(campaign_tool/privacy_patterns.py), fails on an emitting module that is not named here, and fails on a second copy of the
patterns. Add the entry for a new output path in the same change that adds the path.

What this does NOT prove: the patterns have deliberate blind spots (bare 10-digit runs and plain-word emails are not matched),
so a registered path that passes the lint can still carry personal data. The registry covers paths, not detection. It sits under
the human publish gate and BF-df-redaction's sign-off, never in place of them.

Fields: id; module (repo path); entry_point; artefact_kind (page|feed|card|report|brief|draft|post|export); audience (public|pre-public);
lint (privacy|privacy+legal: privacy refuses, the legal-claim guard only flags and never refuses); redaction_required; marker (text the
module source must keep: the lint call); driver (how the test feeds it: site, newsletter, social, publish, report or static); note.
"""


def _entry(id, module, entry_point, kind, audience, lint, marker, driver, note, redaction=True):
    return {"id": id, "module": module, "entry_point": entry_point, "artefact_kind": kind, "audience": audience, "lint": lint,
            "redaction_required": redaction, "marker": marker, "driver": driver, "note": note}


PUBLISH_PATHS = (
    _entry("site-pages", "campaign_tool/site.py", "_plan_site", "page", "public", "privacy+legal", "_privacy_gate(model)", "site",
           "every HTML page; gate runs on the content model before anything renders"),
    _entry("atom-feed", "campaign_tool/site.py", "Site.feed_xml", "feed", "public", "privacy+legal", "_privacy_gate(model)", "site", "feed.xml"),
    _entry("agency-feeds", "campaign_tool/site.py", "Site.agency_feeds", "feed", "public", "privacy+legal", "_privacy_gate(model)", "site",
           "findings/agency/<slug>.xml"),
    _entry("open-graph-meta", "campaign_tool/site.py", "Site.finding_html", "page", "public", "privacy+legal", "_privacy_gate(model)", "site",
           "og:/twitter: meta tags on each finding page"),
    _entry("agency-cards", "campaign_tool/agency_cards.py", "render", "card", "public", "privacy+legal", "_privacy_gate(model)", "site",
           "agency-cards.js, built inside the site plan"),
    _entry("data-exports", "campaign_tool/site.py", "Site.findings_json", "export", "public", "privacy+legal", "_privacy_gate(model)", "site",
           "data/findings.json, .csv, sources, meetings, agencies"),
    _entry("runner-build-site", "runner/handlers/build_site.py", "run", "page", "public", "privacy+legal", "build_site", "static",
           "calls campaign_tool.site.build_site, so it inherits site-pages"),
    _entry("newsletter-draft", "campaign_tool/newsletter/draft.py", "build_draft", "draft", "pre-public", "privacy+legal", "categories(blob)",
           "newsletter", "pre-public: the draft is linted as the last step before it is returned"),
    _entry("social-drafts", "campaign_tool/social.py", "_write_drafts", "draft", "pre-public", "privacy+legal", "refusal(", "social",
           "kit/social/: finding, meeting and outcome drafts; nothing under public/"),
    _entry("social-post", "workers/workspace/src/executors/post_social.ts", "privacyHits", "post", "public", "privacy", "privacyHits(text)",
           "static", "the Worker's per-post lint; its own tests drive it", redaction=False),
    _entry("flock-report", "tools/flock_report.py", "lint", "report", "pre-public", "privacy+legal", "privacy.", "report", "private report lint"),
    _entry("flock-page", "tools/flock_page.py", "lint", "page", "pre-public", "privacy+legal", "privacy.", "static", "story page lint"),
    _entry("flock-publish", "tools/flock_publish.py", "finding_errors", "page", "pre-public", "privacy+legal", "privacy.", "publish",
           "PUBLISH.json finding text rules"),
    _entry("flock-publish-brief", "tools/flock_publish_brief.py", "brief", "brief", "pre-public", "privacy", "privacy.PLATE", "static",
           "agency brief; email, phone and plate"),
    _entry("flock-publish-stage", "tools/flock_publish_stage.py", "stage", "page", "pre-public", "privacy+legal", "text_rules", "static",
           "staged site text"),
    _entry("flock-email", "tools/flock_email.py", "rules", "draft", "pre-public", "privacy", "privacy.categories", "static",
           "outbound-email draft rules; never sent by this repo"),
    _entry("readiness-snapshot", "campaign_tool/cli.py", "readiness", "report", "pre-public", "privacy", "categories(sentence)", "static",
           "readiness JSON: next-action sentences are linted"),
)
