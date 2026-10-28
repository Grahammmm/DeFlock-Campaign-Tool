// Public-comment kit for a meeting, generated from published findings only. Output is the
// Markdown subset campaign_tool.markdown_lite renders (## headings, - lists, **bold**,
// https links); it is stored as text on the meeting row and rendered by the site build.
// Mirrors campaign_tool.meetings.comment_kit_md.
import type { JsonObject } from "@deflock/shared/review";
import type { FindingRow, MeetingRow, PublicationRow } from "./db.ts";

export interface KitFinding {
  title: string;
  summary: string;
  classification: string;
  confidence: string;
  path: string;
  sources: { sha256: string; locator: string; title: string }[];
}

export function kitFinding(finding: FindingRow, publication: PublicationRow): KitFinding {
  const doc = JSON.parse(finding.finding_json) as JsonObject;
  const sources = Array.isArray(doc.sources) ? doc.sources : [];
  return {
    title: typeof doc.title === "string" && doc.title.trim() ? doc.title : finding.summary,
    summary: finding.summary,
    classification: finding.classification,
    confidence: finding.confidence,
    path: publication.path,
    sources: sources
      .filter((s): s is JsonObject => Boolean(s) && typeof s === "object" && !Array.isArray(s))
      .map((s) => ({ sha256: String(s.sha256 ?? ""), locator: String(s.locator ?? ""), title: String(s.title ?? "Source record") })),
  };
}

const ASKS: Record<string, string> = {
  alpr_item: "Continue this item until the public has had at least 30 days with the written policy, the vendor agreement and the sharing list.",
  budget: "Separate the license plate reader line from the consent calendar and hear it as a discussion item with a staff report.",
  consent_calendar: "Pull this item from the consent calendar so residents can comment on it individually.",
};

function clean(text: string): string {
  return text.replace(/[<>]/g, "").replace(/\s+/g, " ").trim();
}

export function commentKitMarkdown(meeting: MeetingRow, findings: KitFinding[], campaignName: string, baseUrl: string | null): string {
  const lines: string[] = [];
  const when = meeting.starts_at.slice(0, 10);
  const link = (path: string) => (baseUrl ? `${baseUrl.replace(/\/$/, "")}${path}` : path);
  lines.push(`## Public comment: ${clean(meeting.body_name)}, ${when}`);
  if (meeting.agenda_item) lines.push(`Agenda item: ${clean(meeting.agenda_item)}`);
  lines.push("");
  lines.push("## Two-minute comment");
  lines.push(`Good evening. My name is [name] and I live in [city]. I am speaking with ${clean(campaignName)} about automated license plate readers.`);
  if (findings.length) {
    lines.push(`We requested public records and published ${findings.length} reviewed finding${findings.length === 1 ? "" : "s"}, each tied to a document hash:`);
    for (const f of findings.slice(0, 3)) lines.push(`- ${clean(f.summary)} (${f.classification.replace(/_/g, " ")}, ${f.confidence.replace(/_/g, " ")})`);
  } else {
    lines.push("We have requested the policy, the vendor agreement and the sharing records and will publish what we receive with source hashes.");
  }
  lines.push("We are not asking you to take our word for it. Every claim links to the record it came from. Please read them before this item moves forward.");
  lines.push("");
  lines.push("## Three asks");
  const primary = ASKS[meeting.relevance ?? ""] ?? "Direct staff to publish the current license plate reader policy, agreement and sharing list before any renewal.";
  lines.push(`- ${primary}`);
  lines.push("- Require an annual public audit of searches, retention and outside-agency sharing, presented at a noticed meeting.");
  lines.push("- Answer in writing which agencies can query this data today and under what written agreement.");
  lines.push("");
  lines.push("## Sources");
  if (!findings.length) lines.push("- No published findings yet; cite the records request itself and its date.");
  for (const f of findings) {
    lines.push(`- **${clean(f.title)}** ${link(f.path)}`);
    for (const s of f.sources) lines.push(`- ${clean(s.title)}, ${clean(s.locator)}, sha256 ${s.sha256.slice(0, 16)}`);
  }
  lines.push("");
  lines.push("Findings describe records and published rules as of the event date. This is not legal advice.");
  return lines.join("\n") + "\n";
}
