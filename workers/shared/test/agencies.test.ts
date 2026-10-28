import { describe, expect, it } from "vitest";
import seed from "../../wizard/src/generated/agencies-us-ca.json";
import {
  DEFAULT_SELECTED_KINDS,
  LocationError,
  agenciesFor,
  locate,
  locationLabel,
  placeChoices,
  type AgencySeed,
} from "../src/agencies.ts";

const SEED = seed as AgencySeed;

describe("locate (port of campaign_tool.discovery.locate)", () => {
  it("resolves a county by bare name and with suffix", () => {
    const a = locate("San Luis Obispo County", "ca", SEED);
    expect(a.match_kind).toBe("county");
    expect(a.county_fips).toBe("06079");
    expect(a.place_name).toBeNull();
    expect(locate("Kern", "CA", SEED).county_name).toBe("Kern");
  });

  it("resolves a city with 'City of' and Census-style suffix", () => {
    const c = locate("City of Morro Bay", "CA", SEED);
    expect(c.match_kind).toBe("city");
    expect(c.place_name).toBe("Morro Bay");
    expect(c.county_name).toBe("San Luis Obispo");
    expect(locate("Morro Bay city", "CA", SEED).place_name).toBe("Morro Bay");
    expect(locationLabel(c)).toBe("Morro Bay (San Luis Obispo County, CA)");
  });

  it("raises on ambiguity with candidates, on unknown names and wrong state", () => {
    try {
      locate("San Luis Obispo", "CA", SEED);
      throw new Error("expected ambiguity");
    } catch (e) {
      expect(e).toBeInstanceOf(LocationError);
      expect((e as LocationError).candidates).toEqual([
        "City of San Luis Obispo (San Luis Obispo County)",
        "San Luis Obispo County",
      ]);
    }
    expect(() => locate("Nowhere", "CA", SEED)).toThrow(/No county or city/);
    expect(() => locate("Kern", "NV", SEED)).toThrow(/Seed covers CA/);
    expect(() => locate("  ", "CA", SEED)).toThrow(/non-empty/);
  });

  it("lists place choices for a datalist", () => {
    const choices = placeChoices(SEED);
    expect(choices).toContain("San Luis Obispo County");
    expect(choices).toContain("City of Morro Bay");
  });
});

describe("agenciesFor ordering (port of discovery.agencies_for)", () => {
  it("city match: city agencies first, then county-wide, include order kept", () => {
    const ids = agenciesFor(locate("Morro Bay", "CA", SEED), SEED).map((a) => a.agency_id);
    expect(ids).toEqual([
      "ca-morro-bay-police",
      "ca-morro-bay-city-council",
      "ca-san-luis-obispo-county-sheriff",
      "ca-san-luis-obispo-county-board-of-supervisors",
      "ca-san-luis-obispo-county-district-attorney",
      "ca-san-luis-obispo-county-chp",
    ]);
  });

  it("county match: county-wide then every city in seed order", () => {
    const list = agenciesFor(locate("San Luis Obispo County", "CA", SEED), SEED);
    const ids = list.map((a) => a.agency_id);
    expect(ids.slice(0, 4)).toEqual([
      "ca-san-luis-obispo-county-sheriff",
      "ca-san-luis-obispo-county-board-of-supervisors",
      "ca-san-luis-obispo-county-district-attorney",
      "ca-san-luis-obispo-county-chp",
    ]);
    expect(ids[4]).toBe("ca-arroyo-grande-police");
    expect(ids[5]).toBe("ca-arroyo-grande-city-council");
    expect(list.every((a) => a.suggested === true)).toBe(true);
  });

  it("include filters and reorders; unknown kinds rejected", () => {
    const ids = agenciesFor(locate("Morro Bay", "CA", SEED), SEED, ["chp", "police"]).map((a) => a.agency_id);
    expect(ids).toEqual(["ca-morro-bay-police", "ca-san-luis-obispo-county-chp"]);
    expect(() => agenciesFor(locate("Morro Bay", "CA", SEED), SEED, ["fbi" as never])).toThrow(/Unknown agency kind/);
  });

  it("default selection excludes district attorney and CHP", () => {
    expect(DEFAULT_SELECTED_KINDS).toEqual(["sheriff", "police", "county_board", "city_council"]);
    const selected = agenciesFor(locate("Morro Bay", "CA", SEED), SEED).filter((a) => DEFAULT_SELECTED_KINDS.includes(a.kind));
    expect(selected.map((a) => a.kind)).toEqual(["police", "city_council", "sheriff", "county_board"]);
  });
});
