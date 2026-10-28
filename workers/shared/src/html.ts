// Server-rendered HTML helpers. Every interpolation goes through `esc` unless it is
// already a `Safe` fragment produced by `html`/`raw`.
import { STYLES_CSS_TEMPLATE } from "./generated/styles.ts";

export class Safe {
  constructor(public readonly value: string) {}
  toString() {
    return this.value;
  }
}

export function esc(value: unknown): string {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function raw(value: string): Safe {
  return new Safe(value);
}

type Piece = string | number | boolean | null | undefined | Safe | Piece[];

function render(piece: Piece): string {
  if (piece === null || piece === undefined || piece === false) return "";
  if (piece instanceof Safe) return piece.value;
  if (Array.isArray(piece)) return piece.map(render).join("");
  return esc(piece);
}

/** Tagged template: `html\`<b>${userText}</b>\`` escapes interpolations. */
export function html(strings: TemplateStringsArray, ...values: Piece[]): Safe {
  let out = "";
  for (let i = 0; i < strings.length; i++) {
    out += strings[i];
    if (i < values.length) out += render(values[i]);
  }
  return new Safe(out);
}

export const FONT_FAMILY =
  'system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif';

/** The engine stylesheet with template placeholders resolved. */
export function siteCss(): string {
  return STYLES_CSS_TEMPLATE.replace("{{FONT_FAMILY}}", FONT_FAMILY).replace("{{FONT_FACES}}", "");
}

/** Small additions for app screens (forms, tables, cards) on top of the site shell. */
export const APP_CSS = `
.app-main{max-width:var(--max);margin:auto;padding:2rem 4vw}
.app-main h1{font-size:clamp(1.8rem,3vw,2.6rem);letter-spacing:-.04em;margin:0 0 1rem}
.app-main h2{font-size:1.35rem;margin:2rem 0 .8rem}
.app-nav{display:flex;flex-wrap:wrap;gap:.4rem 1.2rem;padding:.8rem 4vw;background:var(--paper);border-bottom:1px solid var(--line);font-size:.9rem}
.app-nav a{color:var(--ink);text-decoration:none;font-weight:600}.app-nav a.is-active{border-bottom:2px solid var(--coral)}
.steps{display:flex;flex-wrap:wrap;gap:.5rem;list-style:none;padding:0;margin:0 0 1.5rem;font-size:.85rem}
.steps li{padding:.3rem .6rem;background:var(--paper);border-radius:2px;color:var(--muted)}.steps li.is-active{background:var(--ink);color:#fff}.steps li.is-done{background:var(--mint)}
form.stack{display:grid;gap:1rem;max-width:44rem}
label.field{display:grid;gap:.3rem;font-weight:600;font-size:.9rem}
label.field span.hint{font-weight:400;color:var(--muted);font-size:.8rem}
input[type=text],input[type=email],input[type=url],input[type=password],input[type=number],select,textarea{font:inherit;padding:.6rem .7rem;border:1px solid var(--line);background:#fff;color:var(--ink);width:100%}
textarea{min-height:14rem;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85rem}
.button,button.primary{display:inline-block;border:0;background:var(--coral);color:#fff;padding:.75rem 1.2rem;font:inherit;font-weight:700;cursor:pointer;text-decoration:none}
button.secondary{border:1px solid var(--ink);background:#fff;color:var(--ink);padding:.7rem 1.1rem;font:inherit;font-weight:600;cursor:pointer}
button.danger{border:0;background:#7a1f12;color:#fff;padding:.7rem 1.1rem;font:inherit;font-weight:600;cursor:pointer}
.notice{border-left:4px solid var(--mint);background:#fff;padding:.8rem 1rem;margin:0 0 1rem}
.notice.error{border-color:var(--coral);background:#fbeae6}
.notice.warn{border-color:#c98a1a;background:#fff4dd}
table.data{width:100%;border-collapse:collapse;background:#fff;font-size:.9rem}
table.data th,table.data td{text-align:left;padding:.55rem .6rem;border-bottom:1px solid var(--line);vertical-align:top}
table.data th{background:var(--paper);font-size:.78rem;text-transform:uppercase;letter-spacing:.06em}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(18rem,1fr));gap:1rem}
.card{background:#fff;border-top:3px solid var(--coral);padding:1.2rem;box-shadow:0 8px 24px #092b2d0d}
.card h3{margin:0 0 .5rem;font-size:1.1rem}.card .meta{color:var(--muted);font-size:.8rem}
.stat{display:grid;gap:.2rem}.stat strong{font-size:2rem;letter-spacing:-.04em;color:var(--ink)}
pre.json{background:var(--ink);color:#e6f2ee;padding:1rem;overflow:auto;font-size:.8rem;max-height:30rem}
.pill{display:inline-block;padding:.15rem .5rem;font-size:.75rem;font-weight:700;background:var(--paper);border-radius:2px}
.pill.ok{background:var(--mint)}.pill.bad{background:#f7ded6;color:#7a1f12}.pill.warn{background:#fff4dd;color:#6b4a06}
.agency-list{list-style:none;padding:0;margin:0;display:grid;gap:.5rem}.agency-list li{background:#fff;padding:.7rem .9rem;border:1px solid var(--line);display:flex;gap:.8rem;align-items:flex-start}
.agency-list .kind{color:var(--muted);font-size:.8rem}
.cost-table td:last-child{white-space:nowrap}
.timeline{list-style:none;padding:0;margin:0;display:grid;gap:.6rem}.timeline li{background:#fff;border-left:3px solid var(--mint);padding:.7rem .9rem}
.timeline li.outbound{border-color:var(--coral)}
.checklist{display:grid;gap:.4rem}
@media(max-width:850px){.app-main{padding:1.2rem 1rem}}
`;

export interface ShellOptions {
  title: string;
  brand: string;
  nav?: { href: string; label: string; active?: boolean }[];
  body: Safe;
  headExtra?: Safe;
  footer?: Safe;
}

/** Page frame reusing the engine shell (topbar, main, footer) and stylesheet. */
export function shell(opts: ShellOptions): string {
  const nav = (opts.nav ?? [])
    .map((n) => html`<a href="${n.href}"${n.active ? raw(' class="is-active" aria-current="page"') : ""}>${n.label}</a>`)
    .map((s) => s.value)
    .join("");
  return (
    "<!doctype html>\n" +
    html`<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>${opts.title} - ${opts.brand}</title><style>${raw(siteCss())}${raw(APP_CSS)}</style>${opts.headExtra ?? ""}</head><body><a class="skip-link" href="#top">Skip to content</a><header class="topbar"><a class="brand" href="/"><span class="brand-mark" aria-hidden="true"></span>${opts.brand}</a></header>${nav ? raw('<nav class="app-nav" aria-label="Sections">' + nav + "</nav>") : ""}<main id="top" class="app-main">${opts.body}</main><footer class="site-footer"><p>${opts.footer ?? "Nothing on this screen sends a request, publishes a finding or spends money without an explicit approval."}</p></footer></body></html>`.value
  );
}

export function fmtDate(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 10) : "";
}

export function jsonBlock(value: unknown): Safe {
  return html`<pre class="json">${JSON.stringify(value, null, 2)}</pre>`;
}
