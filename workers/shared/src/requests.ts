// Port of campaign_tool/kit.py render_combined_request: one draft covering every request
// scope in a law package. Placeholders stay literal until the organizer fills them in.

export interface LawSource {
  title: string;
  url: string;
  accessed: string;
}

export interface LawRule {
  rule_id: string;
  citation: string;
  actor: string;
  activity: string;
  duty: string;
  exceptions: string[];
  remedy: string;
  effective_from: string;
  effective_to: string | null;
  sources: LawSource[];
  review: "verified" | "likely" | "needs_attorney_review";
}

export interface RequestScope {
  scope_id: string;
  title: string;
  items: string[];
  rule_ids: string[];
}

export interface LawPackage {
  schema_version: number;
  jurisdiction: string;
  status: "draft" | "reviewed";
  reviewed_by: string[];
  reviewed_at: string | null;
  records_law: {
    name: string;
    citation: string;
    determination_days: number;
    determination_extension_days: number;
    day_type: string;
    fee_basis: string;
    appeal: string;
    sources: LawSource[];
  };
  rules: LawRule[];
  request_scopes: RequestScope[];
}

export interface DraftAgency {
  name: string;
  jurisdiction_name?: string;
  records_email?: string | null;
}

export const DEFAULT_FEE_CAP = "[explicit approved cap, e.g. $50]";
export const DEFAULT_REQUESTER = "[requester-approved contact details]";
export const DEFAULT_DATE_RANGE = "[date range, e.g. 2023-01-01 through today]";
export const DEFAULT_CUSTODIAN = "[official records custodian - verify on the agency's website]";

export interface DraftOptions {
  requesterBlock?: string;
  feeCap?: string;
  dateRange?: string;
}

export function cleanAgencyName(name: unknown): string {
  if (typeof name !== "string" || !name.trim() || name.includes("<") || name.includes(">")) {
    throw new Error(`Agency name must be plain text without angle brackets: ${JSON.stringify(name)}`);
  }
  return name.trim();
}

export function requestSubject(agencyName: string, dateRange: string = DEFAULT_DATE_RANGE): string {
  return `Records concerning any automated license plate reader (ALPR) use by ${agencyName}, ${dateRange}`;
}

/** Markdown draft with the same shape as kit.py; no statutory deadline is asserted. */
export function renderCombinedRequest(pkg: LawPackage, agency: DraftAgency, opts: DraftOptions = {}): string {
  const requesterBlock = opts.requesterBlock ?? DEFAULT_REQUESTER;
  const feeCap = opts.feeCap ?? DEFAULT_FEE_CAP;
  const dateRange = opts.dateRange ?? DEFAULT_DATE_RANGE;
  const name = cleanAgencyName(agency.name);
  const custodian = agency.records_email || DEFAULT_CUSTODIAN;
  const law = pkg.records_law;
  const rules = new Map(pkg.rules.map((r) => [r.rule_id, r]));
  const lines: string[] = [
    "# Draft records request: " + name,
    "",
    "DRAFT ONLY. Nothing has been sent. Verify the recipient on an official agency",
    "source and review the request process before sending. Law package status: " + pkg.status + ".",
    "",
    `To: ${custodian}`,
    `Agency: ${name} (${agency.jurisdiction_name ?? ""})`,
    `Subject: ${requestSubject(name, dateRange)}`,
    "",
    `Under the ${law.name}, ${law.citation}, please provide existing`,
    "records, if any, concerning the following. If your agency holds no responsive",
    "records for a section, please say so for that section.",
  ];
  for (const scope of pkg.request_scopes) {
    lines.push("", `## ${scope.title}`, "");
    for (const item of scope.items) lines.push(`- ${item}`);
  }
  lines.push(
    "",
    `The date range for this request is ${dateRange}.`,
    "",
    "Electronic native formats are preferred where maintained. Please identify omitted",
    "exhibits and explain any withholding with its stated legal basis. Please advise",
    `before incurring fees beyond ${feeCap}. Rolling production is welcome.`,
    "",
    requesterBlock,
    "",
    "## Statutory context (for the requester, not for sending)",
    "",
    `- Determination period: ${law.determination_days} ${law.day_type} days from receipt, ` +
      `with one extension of up to ${law.determination_extension_days} days in unusual circumstances.`,
    `- Fee basis: ${law.fee_basis}`,
    `- Enforcement: ${law.appeal}`,
    "",
    "Rules the scopes relate to:",
  );
  const cited: string[] = [];
  for (const scope of pkg.request_scopes) {
    for (const ruleId of scope.rule_ids) {
      if (rules.has(ruleId) && !cited.includes(ruleId)) cited.push(ruleId);
    }
  }
  for (const rid of cited) {
    const rule = rules.get(rid)!;
    lines.push(`- ${rid}: ${rule.citation} (review: ${rule.review})`);
  }
  lines.push(
    "",
    "Operator checklist: record exact scope, recipient source, date, fee cap, send",
    "approval and delivery receipt. This draft does not determine a statutory deadline",
    "and does not assert that this agency operates ALPR.",
    "",
  );
  return lines.join("\n");
}

/** Combined scope id used for the single combined request row per agency. */
export const COMBINED_SCOPE_ID = "combined";

/** Determination due date (calendar days) from a sent date; mirrors law.deadline for calendar day_type. */
export function determinationDue(pkg: LawPackage, sentAtIso: string, extension = false): string {
  const days = pkg.records_law.determination_days + (extension ? pkg.records_law.determination_extension_days : 0);
  const d = new Date(sentAtIso);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}
