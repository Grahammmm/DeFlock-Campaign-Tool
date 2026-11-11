import { html, raw, shell, type Safe } from "@deflock/shared/html";
import type { AccessIdentity } from "../auth.ts";

export const NAV = [
  ["/", "Waiting on you"],
  ["/status", "Dashboard"],
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

/** Console-only CSS, injected after the shared stylesheet so the wizard pages that share APP_CSS are unchanged. */
export const CONSOLE_CSS = `
.app-nav a{display:inline-flex;align-items:center;min-height:24px}
.table-wrap{overflow-x:auto;max-width:100%}.table-wrap:focus-visible,pre.json:focus-visible{outline:3px solid var(--coral);outline-offset:2px}
@media(max-width:700px){.app-nav a{min-height:44px}}
`;

export function page(title: string, brand: string, current: string, identity: AccessIdentity, body: Safe): string {
  return shell({
    title,
    brand,
    nav: NAV.map(([href, label]) => ({ href, label, active: href === "/" ? current === "/" : current.startsWith(href) })),
    headExtra: html`<style>${raw(CONSOLE_CSS)}</style>`,
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

/** A data table in its own labelled, keyboard-scrollable box so a wide table scrolls inside it on a phone. */
export function tableWrap(label: string, table: Safe): Safe {
  return html`<div class="table-wrap" role="region" aria-label="${label}" tabindex="0">${table}</div>`;
}

/** Shared-layout page for an unknown path. */
export function notFoundBody(): Safe {
  return html`<h1>Page not found</h1><p>That page does not exist in this workspace.</p><p><a href="/">Back to Waiting on you</a></p>`;
}

/** A workspace JSON block: a labelled, keyboard-scrollable pre (the shared jsonBlock is unchanged for the wizard). */
export function consoleJson(label: string, value: unknown): Safe {
  return html`<pre class="json" tabindex="0" role="region" aria-label="${label}">${JSON.stringify(value, null, 2)}</pre>`;
}
