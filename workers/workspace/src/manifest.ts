// Content manifest for build_site jobs: everything campaign_tool.content.load_content reads
// from a content/ directory, assembled from D1 so the runner can materialise the
// directory and run `campaign_tool build --check`. Only published (or corrected) findings
// appear; withdrawn ones never do. The manifest carries no originals, mail, subscribers
// or credentials. Its hash names the site version, so an unchanged manifest re-uses the
// same build job instead of enqueuing another.
import { sha256Hex } from "@deflock/shared/ids";
import { canonicalJson, type Json, type JsonObject } from "@deflock/shared/review";
import type { AgencyRow, CorrectionRow, FindingRow, MeetingRow, PublicationRow, Repo } from "./db.ts";

export const MANIFEST_SCHEMA_VERSION = 1;

/** Keys campaign_tool.content.FINDING_KEYS accepts; anything else in finding_json stays private. */
const FINDING_KEYS = [
  "id", "title", "slug", "summary", "body_md", "classification", "confidence", "event_date", "sources", "published_at",
  "updated_at", "corrections", "state", "author", "limitations", "counterevidence", "agency_id", "rule_version", "duty",
  "exceptions", "rule_ids",
] as const;
const SOURCE_KEYS = ["sha256", "locator", "title", "rule_id"] as const;
const STARTER_SLUGS = new Set(["index", "agencies", "findings", "sources", "meetings", "about"]);

export interface BrevoSettings {
  list_id: number | null;
  form_url: string | null;
  sender_name: string | null;
  sender_email: string | null;
  /** The campaign's reviewed consent/sender footer; the engine's template is never sent. */
  consent_footer?: string | null;
  updated_by?: string;
  updated_at?: string;
}

export interface ContentManifest {
  schema_version: number;
  site_version: string;
  manifest_sha256: string;
  site: JsonObject;
  findings: JsonObject[];
  meetings: JsonObject[];
  sources: Json[];
  agencies: JsonObject[];
  map: JsonObject | null;
}

/** Deterministic public slug for a finding: finding_json.slug when present, else from the summary. */
export function findingSlug(finding: FindingRow): string {
  const doc = JSON.parse(finding.finding_json) as JsonObject;
  const explicit = typeof doc.slug === "string" ? doc.slug : "";
  if (/^[a-z][a-z0-9-]{0,63}$/.test(explicit) && !STARTER_SLUGS.has(explicit)) return explicit;
  const base = finding.summary
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48)
    .replace(/-+$/g, "");
  const suffix = finding.finding_id.replace(/^fnd_/, "").slice(0, 8);
  const slug = (base && /^[a-z]/.test(base) ? base + "-" : "finding-") + suffix;
  return slug.slice(0, 64);
}

export function publicationPath(finding: FindingRow): string {
  return "/findings/" + findingSlug(finding) + ".html";
}

function pick(doc: JsonObject, keys: readonly string[]): JsonObject {
  const out: JsonObject = {};
  for (const k of keys) if (Object.hasOwn(doc, k)) out[k] = doc[k];
  return out;
}

/** The finding as the site content model expects it: published state, public keys only. */
export function findingDocument(finding: FindingRow, publication: PublicationRow, corrections: CorrectionRow[]): JsonObject {
  const raw = JSON.parse(finding.finding_json) as JsonObject;
  const doc = pick(raw, FINDING_KEYS);
  doc.id = finding.finding_id;
  doc.slug = findingSlug(finding);
  doc.state = "published";
  doc.summary = finding.summary;
  doc.classification = finding.classification;
  doc.confidence = finding.confidence;
  doc.author = finding.author;
  if (typeof doc.title !== "string" || !doc.title.trim()) doc.title = finding.summary;
  doc.published_at = publication.published_at;
  doc.updated_at = corrections.length ? corrections[corrections.length - 1].corrected_at : finding.updated_at;
  if (Array.isArray(raw.sources)) {
    doc.sources = (raw.sources as Json[]).map((s) => (s && typeof s === "object" && !Array.isArray(s) ? pick(s as JsonObject, SOURCE_KEYS) : s));
  }
  const existing = Array.isArray(raw.corrections) ? (raw.corrections as Json[]) : [];
  doc.corrections = [
    ...existing,
    // The organizer's reason stays in the workspace: it never passed the two-reviewer gate
    // that the finding text did, so the public note records only the fact of the correction
    // and, when there is one, the reviewed replacement finding.
    ...corrections.map((c) => ({
      date: c.corrected_at.slice(0, 10),
      note: c.replacement_finding_id
        ? `Corrected; replaced by finding ${c.replacement_finding_id}.`
        : c.reason.startsWith("withdrawn:")
          ? "Withdrawn."
          : "Corrected.",
    })),
  ];
  for (const key of ["limitations", "counterevidence"]) if (!Array.isArray(doc[key])) doc[key] = [];
  return doc;
}

export function meetingDocument(m: MeetingRow): JsonObject {
  const doc: JsonObject = {
    id: m.meeting_id.replace(/[^a-z0-9-]/g, "-").replace(/^[^a-z]+/, "m-"),
    body: m.body_name,
    starts_at: m.starts_at,
    agenda_item: m.agenda_item ?? "",
    comment_kit_md: m.comment_kit_md ?? "",
  };
  if (m.agenda_url) doc.agenda_url = m.agenda_url;
  if (m.agency_id) doc.agency_id = m.agency_id;
  return doc;
}

export function agencyDocument(a: AgencyRow): JsonObject {
  let sources: Json = [];
  try {
    sources = JSON.parse(a.sources_json) as Json;
  } catch {
    sources = [];
  }
  return {
    agency_id: a.agency_id,
    name: a.name,
    kind: a.kind,
    jurisdiction_name: a.jurisdiction_name,
    records_url: a.records_url,
    records_email: a.records_email,
    portal: { vendor: a.portal_vendor, url: a.portal_url },
    selected: true,
    verified: a.verified === 1,
    sources,
    flock_transparency_slug: a.flock_transparency_slug,
    muckrock_agency_id: a.muckrock_agency_id,
  };
}

/** site.json: the `site_json` setting, else a neutral default; signup follows the Brevo setting. */
export async function siteDocument(repo: Repo): Promise<JsonObject> {
  const campaign = await repo.campaign();
  const stored = (await repo.setting<JsonObject>("site_json")) ?? null;
  const site: JsonObject = stored && typeof stored === "object" ? { ...stored } : {
    schema_version: 1,
    tagline: campaign ? `${campaign.name}: license plate reader records in ${campaign.county_name} County` : "Campaign",
    about_md: "This site publishes reviewed findings about automated license plate reader records, with a source hash for every claim.",
  };
  if (site.schema_version === undefined) site.schema_version = 1;
  if (campaign?.public_hostname && site.base_url === undefined) site.base_url = "https://" + campaign.public_hostname;
  const brevo = await repo.setting<BrevoSettings>("brevo");
  if (brevo?.form_url) {
    const signup = (site.signup && typeof site.signup === "object" && !Array.isArray(site.signup) ? { ...(site.signup as JsonObject) } : {}) as JsonObject;
    signup.mode = "brevo_hosted";
    signup.form_html_allowlisted_url = brevo.form_url;
    site.signup = signup;
  } else if (site.signup === undefined) {
    site.signup = { mode: "none" };
  }
  return site;
}

/** Assemble the manifest from D1. `now` bounds the meetings included (upcoming only). */
export async function assembleManifest(repo: Repo, now: Date = new Date()): Promise<ContentManifest> {
  const findings: JsonObject[] = [];
  for (const finding of await repo.findingsByState(["published", "corrected"])) {
    const publication = await repo.publicationForFinding(finding.finding_id);
    if (!publication) continue; // published state without a publication row is not renderable
    findings.push(findingDocument(finding, publication, await repo.correctionsFor(publication.publication_id)));
  }
  findings.sort((a, b) => String(b.published_at).localeCompare(String(a.published_at)) || String(a.slug).localeCompare(String(b.slug)));
  const meetings = (await repo.upcomingMeetings(now.toISOString())).map(meetingDocument);
  const agencies = (await repo.selectedAgencies()).map(agencyDocument);
  const sources = (await repo.setting<Json[]>("sources_json")) ?? [];
  const map = (await repo.setting<JsonObject>("map_config")) ?? null;
  const site = await siteDocument(repo);
  const body: JsonObject = { schema_version: MANIFEST_SCHEMA_VERSION, site, findings, meetings, sources: Array.isArray(sources) ? sources : [], agencies, map };
  const hash = await sha256Hex(canonicalJson(body));
  return { ...(body as unknown as Omit<ContentManifest, "site_version" | "manifest_sha256">), site_version: "m" + hash.slice(0, 16), manifest_sha256: hash };
}

/** Inputs of a newsletter_draft job: what campaign_tool.newsletter.build_draft consumes. No personal data. */
export async function newsletterManifest(repo: Repo, now: Date = new Date()): Promise<JsonObject> {
  const campaign = await repo.campaign();
  const lastSend = await repo.latestSubscriberEvent("campaign_sent");
  const since = lastSend?.occurred_at ?? null;
  const findings: JsonObject[] = [];
  for (const finding of await repo.findingsByState(["published", "corrected"])) {
    const pub = await repo.publicationForFinding(finding.finding_id);
    if (!pub || (since && pub.published_at <= since)) continue;
    const doc = JSON.parse(finding.finding_json) as JsonObject;
    findings.push({
      title: typeof doc.title === "string" && doc.title.trim() ? doc.title : finding.summary,
      summary: finding.summary,
      classification: finding.classification,
      confidence: finding.confidence,
      path: pub.path,
      published_at: pub.published_at,
      corrected: finding.state === "corrected",
    });
  }
  findings.sort((a, b) => String(a.published_at).localeCompare(String(b.published_at)));
  const meetings = (await repo.upcomingMeetings(now.toISOString())).slice(0, 10).map((m) => ({
    body: m.body_name,
    starts_at: m.starts_at,
    agenda_item: m.agenda_item ?? "",
    agenda_url: m.agenda_url,
    relevance: m.relevance,
  }));
  return {
    schema_version: 1,
    campaign: {
      name: campaign?.name ?? "Campaign",
      base_url: campaign?.public_hostname ? "https://" + campaign.public_hostname : null,
      county_name: campaign?.county_name ?? "",
      consent_footer: (await repo.setting<BrevoSettings>("brevo"))?.consent_footer ?? null,
    },
    since,
    generated_at: now.toISOString(),
    findings,
    meetings,
  };
}

/**
 * Enqueue a build_site job for the current manifest and propose the matching deploy_site
 * card. Idempotent on the manifest hash: rebuilding unchanged content returns the existing
 * job and card. The deploy card is approved by an organizer only after the build job is done.
 */
export async function proposeRebuild(repo: Repo, proposedBy: string, reason: string): Promise<{ manifest: ContentManifest; job_id: string; job_created: boolean; action_id: string; action_created: boolean }> {
  const manifest = await assembleManifest(repo);
  const job = await repo.enqueueJob("build_site", await sha256Hex("build_site:" + manifest.manifest_sha256), { site_version: manifest.site_version, reason, manifest });
  const action = await repo.propose(
    "deploy_site",
    job.row.job_id,
    { site_version: manifest.site_version, build_job_id: job.row.job_id, reason, note: "Approve only after the build_site job is done and the preview was checked." },
    await sha256Hex("deploy_site:" + manifest.site_version),
    proposedBy,
  );
  return { manifest, job_id: job.row.job_id, job_created: job.inserted, action_id: action.row.action_id, action_created: action.inserted };
}
