// TypeScript port of campaign_tool/discovery.py: offline location resolution and the
// ordered agency suggestions for a county or city. Semantics mirror the Python module;
// tests/test_discovery.py is the reference. Nothing here asserts that any agency uses ALPR.

export type AgencyKind = "sheriff" | "police" | "chp" | "district_attorney" | "county_board" | "city_council" | "other";

export interface SeedSource {
  title: string;
  url: string | null;
  accessed: string;
}

export interface SeedAgency {
  agency_id: string;
  name: string;
  kind: AgencyKind;
  jurisdiction_name: string;
  place_fips: string | null;
  records_url: string | null;
  records_email: string | null;
  portal: { vendor: string; url: string | null };
  flock_transparency_slug: string | null;
  muckrock_agency_id: number | null;
  verified: boolean;
  sources: SeedSource[];
}

export interface SeedCounty {
  county_fips: string;
  name: string;
  seat: string;
  agencies: SeedAgency[];
}

export interface AgencySeed {
  schema_version: number;
  jurisdiction: string;
  generated: string;
  counties: SeedCounty[];
}

export interface Location {
  county_fips: string;
  county_name: string;
  state: string;
  place_fips: string | null;
  place_name: string | null;
  match_kind: "county" | "city";
  query: string;
}

export type SuggestedAgency = SeedAgency & { suggested: true };

export const COUNTY_KINDS: readonly AgencyKind[] = ["sheriff", "county_board", "district_attorney", "chp"];
export const CITY_KINDS: readonly AgencyKind[] = ["police", "city_council"];
export const LAW_ENFORCEMENT_KINDS: readonly AgencyKind[] = ["sheriff", "police", "chp"];
export const DEFAULT_INCLUDE: readonly AgencyKind[] = [
  "sheriff",
  "police",
  "county_board",
  "city_council",
  "district_attorney",
  "chp",
];
/** Wizard defaults: checked kinds. district_attorney and chp start unchecked. */
export const DEFAULT_SELECTED_KINDS: readonly AgencyKind[] = ["sheriff", "police", "county_board", "city_council"];

export class LocationError extends Error {
  constructor(
    message: string,
    public readonly candidates: string[] = [],
  ) {
    super(message);
  }
}

export function normalize(text: unknown): string {
  let s = String(text)
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^\x00-\x7f]/g, "");
  s = s.toLowerCase().replace(/&/g, " and ");
  s = s.replace(/[^a-z0-9]+/g, " ");
  return s.replace(/\s+/g, " ").trim();
}

export function seedState(seed: AgencySeed): string {
  return seed.jurisdiction.split("-")[1].toUpperCase();
}

export function placeNameOf(agency: SeedAgency): string | null {
  const m = /^(?:City|Town) of (.+)$/.exec(agency.jurisdiction_name);
  return m ? m[1] : null;
}

export function citiesIn(county: SeedCounty): [string, string | null][] {
  const seen = new Map<string, string | null>();
  for (const entry of county.agencies) {
    if (CITY_KINDS.includes(entry.kind)) {
      const name = placeNameOf(entry);
      if (name && !seen.has(name)) seen.set(name, entry.place_fips);
    }
  }
  return [...seen.entries()];
}

type Candidate = ["county" | "city", string, SeedCounty, string | null, string | null];

function* candidates(seed: AgencySeed): Generator<Candidate> {
  for (const county of seed.counties) {
    yield ["county", normalize(county.name), county, null, null];
    for (const [name, fips] of citiesIn(county)) yield ["city", normalize(name), county, name, fips];
  }
}

function splitHint(query: string): [("county" | "city") | null, string] {
  let text = normalize(query);
  let hint: "county" | "city" | null = null;
  if (text.startsWith("city of ") || text.startsWith("town of ")) {
    hint = "city";
    text = text.split(" ").slice(2).join(" ");
  } else if (text.endsWith(" county")) {
    hint = "county";
    text = text.slice(0, -" county".length);
  }
  return [hint, text];
}

function find(seed: AgencySeed, hint: "county" | "city" | null, text: string): Candidate[] {
  return [...candidates(seed)].filter((c) => c[1] === text && (hint === null || c[0] === hint));
}

/** Resolve a county or city name offline; throws LocationError (with candidates when ambiguous). */
export function locate(query: string, state: string, seed: AgencySeed): Location {
  if (typeof query !== "string" || !query.trim()) throw new LocationError("Location query must be a non-empty string");
  state = String(state).toUpperCase();
  if (state !== seedState(seed)) throw new LocationError(`Seed covers ${seedState(seed)}, not ${state}`);
  const [hint, text] = splitHint(query);
  if (!text) throw new LocationError(`Could not parse location query ${JSON.stringify(query)}`);
  let matches = find(seed, hint, text);
  if (!matches.length && hint === null && text.endsWith(" city")) {
    matches = find(seed, "city", text.slice(0, -" city".length));
  }
  if (!matches.length) {
    throw new LocationError(
      `No county or city named ${JSON.stringify(query)} in the ${state} agency seed; check spelling or choose from the list`,
    );
  }
  if (matches.length > 1) {
    const labels = [
      ...new Set(matches.map((c) => (c[0] === "county" ? `${c[2].name} County` : `City of ${c[3]} (${c[2].name} County)`))),
    ].sort();
    throw new LocationError(`Ambiguous location ${JSON.stringify(query)}; candidates: ${labels.join(", ")}`, labels);
  }
  const [kind, , county, place, fips] = matches[0];
  return {
    county_fips: county.county_fips,
    county_name: county.name,
    state,
    place_fips: fips,
    place_name: place,
    match_kind: kind,
    query,
  };
}

export function countyFor(location: Location, seed: AgencySeed): SeedCounty {
  const county = seed.counties.find((c) => c.county_fips === location.county_fips);
  if (!county) throw new LocationError(`County FIPS ${location.county_fips} is not in the agency seed`);
  return county;
}

/**
 * Suggested agencies for `location`, each with `suggested: true`.
 * City match: that city's agencies first, then county-wide. County match: county-wide,
 * then every city's agencies in seed order. Within a group, `include` order is kept.
 */
export function agenciesFor(
  location: Location,
  seed: AgencySeed,
  include: readonly AgencyKind[] = DEFAULT_INCLUDE,
): SuggestedAgency[] {
  const unknown = [...new Set(include)].filter((k) => !DEFAULT_INCLUDE.includes(k) && k !== "other").sort();
  if (unknown.length) throw new LocationError(`Unknown agency kind(s) ${unknown.join(", ")}`);
  const county = countyFor(location, seed);
  const rank = new Map<AgencyKind, number>();
  include.forEach((kind, index) => {
    if (!rank.has(kind)) rank.set(kind, index);
  });
  const pick = (entries: SeedAgency[]) =>
    entries.filter((e) => rank.has(e.kind)).sort((a, b) => rank.get(a.kind)! - rank.get(b.kind)!);

  const countyWide = county.agencies.filter((e) => COUNTY_KINDS.includes(e.kind));
  const byCity = new Map<string | null, SeedAgency[]>();
  for (const entry of county.agencies) {
    if (CITY_KINDS.includes(entry.kind)) {
      const key = placeNameOf(entry);
      if (!byCity.has(key)) byCity.set(key, []);
      byCity.get(key)!.push(entry);
    }
  }
  let ordered: SeedAgency[] = [];
  if (location.match_kind === "city") {
    if (!byCity.has(location.place_name)) {
      throw new LocationError(`City ${JSON.stringify(location.place_name)} is not in the seed for ${county.name} County`);
    }
    ordered = ordered.concat(pick(byCity.get(location.place_name)!), pick(countyWide));
  } else {
    ordered = ordered.concat(pick(countyWide));
    for (const entries of byCity.values()) ordered = ordered.concat(pick(entries));
  }
  return ordered.map((entry) => ({ ...(JSON.parse(JSON.stringify(entry)) as SeedAgency), suggested: true as const }));
}

/** Human label for a resolved location. */
export function locationLabel(location: Location): string {
  return location.match_kind === "city"
    ? `${location.place_name} (${location.county_name} County, ${location.state})`
    : `${location.county_name} County, ${location.state}`;
}

/** All selectable places for a seed, for a datalist. */
export function placeChoices(seed: AgencySeed): string[] {
  const out: string[] = [];
  for (const county of seed.counties) {
    out.push(`${county.name} County`);
    for (const [name] of citiesIn(county)) out.push(`City of ${name}`);
  }
  return out;
}
