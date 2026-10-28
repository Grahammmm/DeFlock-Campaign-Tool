// Screen 3 -> 4 helpers: default agency selection and one combined request draft per
// selected law-enforcement agency, ported from campaign_tool kit.
import { DEFAULT_SELECTED_KINDS, LAW_ENFORCEMENT_KINDS, type SuggestedAgency } from "@deflock/shared/agencies";
import { renderCombinedRequest, requestSubject, type LawPackage } from "@deflock/shared/requests";
import type { AgencyChoice, Channel, RequestDraft } from "./state.ts";

export const DEFAULT_FEE_CAP_CENTS = 5000;

export function defaultChoices(suggested: SuggestedAgency[]): AgencyChoice[] {
  return suggested.map((s) => ({ agency_id: s.agency_id, kind: s.kind, name: s.name, selected: DEFAULT_SELECTED_KINDS.includes(s.kind) }));
}

export function defaultChannel(agency: SuggestedAgency): Channel {
  if (agency.records_email) return "email";
  if (agency.muckrock_agency_id) return "muckrock";
  if (agency.portal.url) return "portal_manual";
  return "email";
}

export function draftFor(pkg: LawPackage, agency: SuggestedAgency, feeCapCents = DEFAULT_FEE_CAP_CENTS): RequestDraft {
  const feeCap = "$" + (feeCapCents / 100).toFixed(0);
  return {
    agency_id: agency.agency_id,
    subject: requestSubject(agency.name),
    body_md: renderCombinedRequest(pkg, agency, { feeCap }),
    channel: defaultChannel(agency),
    fee_cap_cents: feeCapCents,
  };
}

/** Keep existing drafts for agencies that stay selected; add drafts for newly selected ones. */
export function reconcileDrafts(pkg: LawPackage, suggested: SuggestedAgency[], choices: AgencyChoice[], existing: Record<string, RequestDraft>): Record<string, RequestDraft> {
  const out: Record<string, RequestDraft> = {};
  for (const choice of choices) {
    if (!choice.selected || !LAW_ENFORCEMENT_KINDS.includes(choice.kind)) continue;
    const seed = suggested.find((s) => s.agency_id === choice.agency_id);
    if (!seed) continue;
    out[choice.agency_id] = existing[choice.agency_id] ?? draftFor(pkg, seed);
  }
  return out;
}
