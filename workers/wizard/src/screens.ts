// The seven wizard screens, server-rendered. Every interpolation is escaped by `html`.
import { locationLabel, type SuggestedAgency } from "@deflock/shared/agencies";
import { html, jsonBlock, raw, shell, type Safe } from "@deflock/shared/html";
import { redactStep, type PlanStep } from "./plan.ts";
import { CLOUDFLARE_TOKEN_SCOPES, COST_TABLE, STEP_TITLES, type WizardState } from "./state.ts";

export function frame(state: WizardState | null, step: number, body: Safe, error: string | null = null): string {
  const steps = html`<ol class="steps">${STEP_TITLES.map((t, i) => html`<li class="${i + 1 === step ? "is-active" : i + 1 < (state?.step ?? 1) ? "is-done" : ""}">${i + 1}. ${t}</li>`)}</ol>`;
  return shell({
    title: `Setup ${step}: ${STEP_TITLES[step - 1]}`,
    brand: "DeFlock Campaign Tool setup",
    body: html`${steps}${error ? html`<p class="notice error">${error}</p>` : ""}${body}`,
    footer: html`Session state is an encrypted blob that expires one hour after it was created. Nothing here sends a request, changes DNS or spends money before the Deploy screen, and account tokens are discarded at handoff.`,
  });
}

export function screenLocation(state: WizardState | null, choices: string[], candidates: string[] = []): Safe {
  return html`<h1>Where is the campaign?</h1>
<p>Type a California county or city. Agencies are suggested from the offline seed (unverified contributor knowledge); you review them on the next screens. Nothing here asserts that any agency uses ALPR.</p>
${candidates.length ? html`<p class="notice warn">That name is ambiguous. Pick one: ${candidates.map((c) => html`<a href="/setup/1?query=${encodeURIComponent(c.replace(/ \(.*\)$/, ""))}">${c}</a> `)}</p>` : ""}
<form method="post" action="/setup/1" class="stack">
<label class="field">County or city <input type="text" name="query" list="places" required value="${state?.location?.query ?? ""}" placeholder="San Luis Obispo County or City of Morro Bay"><span class="hint">"X County" resolves the county; "City of X" the city; a bare name that is both asks you to choose.</span></label>
<datalist id="places">${choices.map((c) => html`<option value="${c}"></option>`)}</datalist>
<label class="field">State <select name="state"><option value="CA">California</option></select><span class="hint">Only California has a reviewed-in-progress law package and agency seed today.</span></label>
<button class="primary" type="submit">Continue</button></form>`;
}

export function screenCampaign(state: WizardState): Safe {
  const c = state.campaign;
  const loc = state.location ? locationLabel(state.location) : "";
  return html`<h1>Campaign</h1><p class="meta">${loc}</p>
<form method="post" action="/setup/2" class="stack">
<label class="field">Campaign name <input type="text" name="name" required maxlength="120" value="${c?.name ?? (state.location ? `${state.location.place_name ?? state.location.county_name} ALPR Records` : "")}"></label>
<label class="field">Tagline <input type="text" name="tagline" maxlength="200" value="${c?.tagline ?? "Public records, reviewed findings, local decisions."}"></label>
<label class="field">Domain <input type="text" name="domain" required value="${c?.domain ?? ""}" placeholder="campaign.example.invalid"><span class="hint">Must already be a zone in the organizer's Cloudflare account. The workspace will be at workspace.&lt;domain&gt; and requests mail at requests@&lt;domain&gt;.</span></label>
<label class="field">Privacy tier <select name="privacy_tier"><option value="redacted_cloud" ${c?.privacy_tier !== "strict_local" ? raw("selected") : ""}>redacted_cloud (default): redact before any external model call</option><option value="strict_local" ${c?.privacy_tier === "strict_local" ? raw("selected") : ""}>strict_local: no external model call; loopback/Tailscale endpoint only</option></select></label>
<label class="field">Schedule (cron expressions, ';' separated, UTC) <input type="text" name="schedule_cron" required value="${c?.schedule_cron ?? "0 7,13 * * *;30 20 * * *"}"></label>
<label class="field">Timezone <input type="text" name="timezone" required value="${c?.timezone ?? "America/Los_Angeles"}"></label>
<label class="field">Organizer email (Access identity) <input type="email" name="organizer_email" required value="${c?.organizer_email ?? ""}"><span class="hint">This address is allowed into the workspace by the Access policy; add more organizers later in Cloudflare Zero Trust.</span></label>
<button class="primary" type="submit">Continue</button></form>`;
}

export function screenAgencies(state: WizardState): Safe {
  const byId = new Map(state.suggested.map((s) => [s.agency_id, s]));
  return html`<h1>Agencies</h1>
<p>Suggested from the seed in the same order as <code>campaign_tool kit</code>: ${state.location?.match_kind === "city" ? "this city's agencies first, then county-wide" : "county-wide agencies first, then each city"}. Sheriff, police, county board and city council are checked by default; the district attorney and CHP are not. Contacts are unverified until you confirm them on an official page.</p>
<form method="post" action="/setup/3" class="stack">
<ul class="agency-list">${state.agencies.map((a) => { const s = byId.get(a.agency_id) as SuggestedAgency | undefined; return html`<li><input type="checkbox" id="${a.agency_id}" name="agency" value="${a.agency_id}" ${a.selected ? raw("checked") : ""}><label for="${a.agency_id}"><strong>${a.name}</strong><br><span class="kind">${a.kind} - ${s?.jurisdiction_name ?? ""}${s?.records_email ? " - " + s.records_email : " - no records email in seed"}${s?.portal.url ? " - portal " + s.portal.vendor : ""}${s?.verified ? "" : " - unverified"}</span></label></li>`; })}</ul>
<button class="primary" type="submit">Continue</button></form>`;
}

export function screenRequests(state: WizardState): Safe {
  const drafts = Object.values(state.requests);
  const byId = new Map(state.agencies.map((a) => [a.agency_id, a]));
  return html`<h1>Records requests</h1>
<p>One combined draft per selected law-enforcement agency covering every request scope in the California law package. Edit freely; nothing is sent from the wizard. Sending later requires an approval card in the workspace.</p>
${drafts.length ? "" : html`<p class="notice warn">No law-enforcement agency (sheriff, police, CHP) is selected, so there are no drafts. Go back if that is unintended.</p>`}
<form method="post" action="/setup/4" class="stack">
${drafts.map((d) => html`<fieldset style="border:1px solid var(--line);padding:1rem"><legend><strong>${byId.get(d.agency_id)?.name ?? d.agency_id}</strong></legend>
<label class="field">Subject <input type="text" name="subject:${d.agency_id}" value="${d.subject}" required></label>
<label class="field">Channel <select name="channel:${d.agency_id}"><option value="email" ${d.channel === "email" ? raw("selected") : ""}>email</option><option value="muckrock" ${d.channel === "muckrock" ? raw("selected") : ""}>muckrock</option><option value="portal_manual" ${d.channel === "portal_manual" ? raw("selected") : ""}>portal (manual)</option></select></label>
<label class="field">Fee cap (USD) <input type="number" name="fee_cap:${d.agency_id}" min="0" step="1" value="${(d.fee_cap_cents / 100).toFixed(0)}"></label>
<label class="field">Body (Markdown) <textarea name="body:${d.agency_id}">${d.body_md}</textarea></label></fieldset>`)}
<button class="primary" type="submit">Continue</button></form>`;
}

export function screenAccounts(state: WizardState): Safe {
  const a = state.accounts;
  const strict = state.campaign?.privacy_tier === "strict_local";
  return html`<h1>Accounts</h1>
<p>Credentials entered here live only in the encrypted session blob until handoff, then in the Worker's secrets. They are never written to the campaign repository.</p>
<form method="post" action="/setup/5" class="stack">
<h2>Cloudflare</h2>
<p>Create an API token with exactly these permissions, scoped to the one account and the one zone:</p>
<table class="data"><tr><th>Permission</th><th>Why</th></tr>${CLOUDFLARE_TOKEN_SCOPES.map((s) => html`<tr><td>${s.scope}</td><td>${s.why}</td></tr>`)}</table>
<label class="field">API token <input type="password" name="cf_token" required autocomplete="off"></label>
<label class="field">Account id <input type="text" name="cf_account_id" required pattern="[0-9a-f]{32}" value="${a?.cloudflare.account_id ?? ""}"></label>
<label class="field">Zone id for ${state.campaign?.domain ?? "the domain"} <input type="text" name="cf_zone_id" required pattern="[0-9a-f]{32}" value="${a?.cloudflare.zone_id ?? ""}"></label>
<label class="field">Zero Trust team name <input type="text" name="cf_access_team" required pattern="[a-z0-9-]+" value="${a?.cloudflare.access_team ?? ""}"><span class="hint">The &lt;team&gt; in &lt;team&gt;.cloudflareaccess.com; the workspace validates Access tokens against it.</span></label>
<h2>Mailbox</h2>
<label class="field">Inbound mode <select name="mail_mode"><option value="email_routing" ${a?.mailbox.mode !== "imap_smtp" ? raw("selected") : ""}>Cloudflare Email Routing -> workspace Worker (inbound only)</option><option value="imap_smtp" ${a?.mailbox.mode === "imap_smtp" ? raw("selected") : ""}>IMAP + SMTP mailbox</option></select></label>
<label class="field">IMAP host <input type="text" name="imap_host" value="${a?.mailbox.imap_host ?? ""}"></label>
<label class="field">IMAP user <input type="text" name="imap_user" value="${a?.mailbox.imap_user ?? ""}"></label>
<label class="field">IMAP password <input type="password" name="imap_password" autocomplete="off"></label>
<label class="field">SMTP host <input type="text" name="smtp_host" value="${a?.mailbox.smtp_host ?? ""}"></label>
<label class="field">SMTP user <input type="text" name="smtp_user" value="${a?.mailbox.smtp_user ?? ""}"></label>
<label class="field">SMTP password <input type="password" name="smtp_password" autocomplete="off"></label>
<h2>Newsletter (Brevo, optional)</h2>
<label class="field">API key <input type="password" name="brevo_api_key" autocomplete="off"></label>
<label class="field">List id <input type="text" name="brevo_list_id" value="${a?.brevo?.list_id ?? ""}"></label>
<h2>Model provider (${strict ? "strict_local: loopback or Tailscale URL only" : "redacted_cloud: any OpenAI-compatible endpoint; inputs are redacted first"})</h2>
<label class="field">Base URL <input type="url" name="model_base_url" value="${a?.model?.base_url ?? (strict ? "http://127.0.0.1:11434/v1" : "")}"></label>
<label class="field">API key <input type="password" name="model_api_key" autocomplete="off"></label>
<label class="field">Model id <input type="text" name="model_id" value="${a?.model?.model_id ?? ""}"></label>
<h2>What this will cost</h2>
<table class="data cost-table"><tr><th>Item</th><th>Billing basis</th><th>Note</th></tr>${COST_TABLE.map((c) => html`<tr><td>${c.item}</td><td>${c.basis}</td><td>${c.note}</td></tr>`)}</table>
<p class="meta">No price or capacity guarantee is made; check the providers' current pages before you authorize anything.</p>
<button class="primary" type="submit">Continue to the plan</button></form>`;
}

export function screenDeploy(state: WizardState, plan: PlanStep[], dryRun: boolean): Safe {
  return html`<h1>Deploy plan</h1>
<p>${plan.length} API calls, in order. Nothing has been created yet. ${dryRun ? html`<span class="pill warn">DRY_RUN is on: apply records every call without performing it.</span>` : html`<span class="pill bad">Apply will create billable resources in the organizer's Cloudflare account.</span>`}</p>
<table class="data"><tr><th>#</th><th>Step</th><th>Call</th><th>Rollback</th></tr>${plan.map((s, i) => { const r = redactStep(s); return html`<tr><td>${i + 1}</td><td>${s.title}<br><span class="meta">${s.id}</span></td><td><code>${s.method} ${s.path}</code>${r.body ? html`<details><summary>body</summary>${jsonBlock(r.kind === "d1_query" ? { sql: "(" + String((r.body as { sql: string }).sql).length + " chars)" } : r.kind === "worker_upload" ? { bundle: (r.body as { bundle: string }).bundle, metadata: (r.body as { metadata: unknown }).metadata } : r.body)}</details>` : ""}</td><td>${s.rollback ? html`<code>${s.rollback.method} ${s.rollback.path}</code>` : html`<span class="meta">none</span>`}</td></tr>`; })}</table>
${state.deploy ? html`<h2>Last apply: ${state.deploy.status}</h2>${state.deploy.error ? html`<p class="notice error">Stopped at ${state.deploy.failed_step}: ${state.deploy.error}</p>` : ""}<h3>Receipts</h3>${jsonBlock(state.deploy.receipts)}<h3>Rollback list (newest first)</h3>${jsonBlock(state.deploy.rollback)}` : ""}
<form method="post" action="/setup/6" class="stack"><label class="field"><input type="checkbox" name="confirm" value="yes" required> I authorize these ${plan.length} calls against account ${state.accounts?.cloudflare.account_id ?? "?"} and zone ${state.accounts?.cloudflare.zone_id ?? "?"}.</label><button class="primary" type="submit">${dryRun ? "Apply (dry run)" : "Apply"}</button></form>`;
}

export function screenHandoff(state: WizardState, workspaceUrl: string, publicUrl: string, runnerToken: string | null): Safe {
  const d = state.deploy;
  return html`<h1>Handoff</h1>
${d ? html`<p>${d.status === "applied" ? html`<span class="pill ok">applied</span>` : html`<span class="pill bad">${d.status}</span>`} ${d.dry_run ? html`<span class="pill warn">dry run: no resources were created</span>` : ""} ${d.receipts.length} receipts.</p>` : html`<p class="notice error">No deploy has run.</p>`}
<div class="cards">
<div class="card"><h3>Workspace</h3><p><a href="${workspaceUrl}">${workspaceUrl}</a></p><p class="meta">Protected by Cloudflare Access; sign in as ${state.campaign?.organizer_email ?? "the organizer"}.</p></div>
<div class="card"><h3>Public site</h3><p><a href="${publicUrl}">${publicUrl}</a></p><p class="meta">Serves 503 until a site version is approved on the Publish screen.</p></div>
<div class="card"><h3>Requests inbox</h3><p>${state.campaign ? "requests@" + state.campaign.domain : ""}</p><p class="meta">Inbound mail is preserved by hash and shown in the workspace Inbox.</p></div>
</div>
${runnerToken ? html`<h2>Runner token (shown once)</h2><p class="notice warn">Copy this now; it is not stored anywhere after this page: <code>${runnerToken}</code></p>` : ""}
<h2>Next steps</h2>
<ol class="checklist">
<li>Open the workspace and confirm Access denies an anonymous visitor and admits the organizer.</li>
<li>Verify every agency's records contact on an official page before approving any send.</li>
<li>Start the runner with the token above, pointed at ${workspaceUrl}/api/runner.</li>
<li>Review the request drafts; sending creates approval cards, nothing sends by itself.</li>
<li>Keep this receipt with the campaign's private records. It contains resource ids, no secrets.</li>
</ol>
<p><a class="button" href="/setup/7/launch-receipt.json" download="launch-receipt.json">Download launch-receipt.json</a></p>
<form method="post" action="/setup/reset"><button class="secondary" type="submit">Finish and clear this session</button></form>`;
}
