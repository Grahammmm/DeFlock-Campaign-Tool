// Installed into the operator's site PR by stage_packet_changes.py. No network,
// download links, record bytes or signer identities. Plain text is HTML-escaped.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const e = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fields = ['schema','packet','revision','approved_at','kind','agency_key','request_id','title','text','documents','citations'].sort();
export function renderPacketUpdates(updates) {
  const seen = new Set();
  for (const u of updates) {
    if (JSON.stringify(Object.keys(u).sort()) !== JSON.stringify(fields) || u.schema !== 'records-public-update-v1'
      || !/^[a-f0-9]{16}$/.test(u.packet) || seen.has(u.packet) || !Number.isSafeInteger(u.revision) || u.revision < 0
      || !['document','summary','finding','site_update'].includes(u.kind) || typeof u.title !== 'string' || !u.title || u.title.length > 160
      || typeof u.text !== 'string' || !u.text || u.text.length > 4000 || !Array.isArray(u.documents) || !Array.isArray(u.citations)
      || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(u.agency_key) || !/^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,99})?$/.test(u.request_id)
      || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(u.approved_at) || Number.isNaN(Date.parse(u.approved_at))) throw Error('Invalid public update');
    seen.add(u.packet);
    if (/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|token\s*=|https?:\/\/[^\s<>"']*[?#]|\b\d[A-Z]{3}\d{3}\b|(?:\(\d{3}\)\s?|\d{3}[-.])\d{3}[-.]\d{4}/i.test(JSON.stringify(u))) throw Error('Private update metadata');
    if (u.documents.some(d => !/^[a-f0-9]{64}$/.test(d.sha256) || !/^\d{4}-\d{2}-\d{2}$/.test(d.date_received)
      || !Array.isArray(d.redaction_ids) || u.kind === 'document' && !d.redaction_ids.length)
      || u.citations.some(c => c.verified !== true || typeof c.ref !== 'string')
      || ['document','summary','finding'].includes(u.kind) && !u.documents.length
      || u.kind === 'finding' && !u.citations.length) throw Error('Invalid update evidence');
  }
  if (!updates.length) return '';
  return '<section id="packet-updates"><h2>Reviewed records updates</h2><p>Packet approval covers this text only. Document hashes identify sources, not downloadable copies or legal conclusions. Full-text, redaction, hosting and legal acceptance remain separate gates.</p>'
    + [...updates].sort((a,b) => a.packet.localeCompare(b.packet)).map(u => `<article id="packet-${e(u.packet)}"><h3>${e(u.title)}</h3><p>${e(u.agency_key)} · ${e(u.request_id)} · ${e(u.approved_at.slice(0,10))} · revision ${u.revision}</p><p>${e(u.text)}</p><ul>${u.citations.map(c => `<li>${e(c.ref)}</li>`).join('')}${u.documents.map(d => `<li>Source SHA-256: <span class="hash">${e(d.sha256)}</span>; received ${e(d.date_received)}. No download offered by this update.</li>`).join('')}</ul></article>`).join('') + '</section>';
}
export async function writePacketUpdates() {
  const dir = path.join(root,'content/updates');
  const updates = [];
  for (const name of (await fs.readdir(dir)).sort()) {
    if (!/^[a-f0-9]{16}\.json$/.test(name)) throw Error('Unexpected update file');
    const file = path.join(dir,name);
    if ((await fs.realpath(file)) !== file || !(await fs.lstat(file)).isFile()) throw Error('Unsafe update file');
    const u = JSON.parse(await fs.readFile(file,'utf8'));
    if (name !== u.packet+'.json') throw Error('Update identity mismatch');
    updates.push(u);
  }
  const section = renderPacketUpdates(updates);
  const page = path.join(root,'records.html');
  const html = await fs.readFile(page,'utf8');
  if (!html.includes('</main>') || html.includes('id="packet-updates"')) throw Error('Library build order changed');
  await fs.writeFile(page,html.replace('</main>',section+'</main>'));
}
