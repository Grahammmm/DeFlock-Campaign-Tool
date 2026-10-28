import { html, shell, type Safe } from "@deflock/shared/html";
import type { AccessIdentity } from "../auth.ts";

export const NAV = [
  ["/", "Dashboard"],
  ["/requests", "Requests"],
  ["/inbox", "Inbox"],
  ["/records", "Records"],
  ["/findings", "Findings"],
  ["/approvals", "Approvals"],
  ["/publish", "Publish"],
  ["/subscribers", "Subscribers"],
  ["/meetings", "Meetings"],
  ["/settings", "Settings"],
] as const;

export function page(title: string, brand: string, current: string, identity: AccessIdentity, body: Safe): string {
  return shell({
    title,
    brand,
    nav: NAV.map(([href, label]) => ({ href, label, active: href === "/" ? current === "/" : current.startsWith(href) })),
    body: html`${body}<p class="app-identity" style="margin-top:3rem;color:var(--muted);font-size:.8rem">Signed in as ${identity.email} (Cloudflare Access)</p>`,
  });
}

export function statePill(state: string): Safe {
  const cls = ["executed", "fulfilled", "done", "ready", "published", "approved"].includes(state)
    ? "ok"
    : ["failed", "denied", "rejected", "blocked"].includes(state)
      ? "bad"
      : ["proposed", "queued", "leased", "draft", "in_review"].includes(state)
        ? "warn"
        : "";
  return html`<span class="pill ${cls}">${state}</span>`;
}
