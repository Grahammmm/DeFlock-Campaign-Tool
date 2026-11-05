import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';

// DF1622-08: build the fictional example with synthetic asked items, then run the generated requests/filter.js in a mocked
// DOM parsed from the built requests.html. No browser, no network, nothing private.
const POLICE='ca-cedar-police',SHERIFF='ca-cedar-county-sheriff',COUNCIL='ca-cedar-city-council';
const it=(topic,asked_on,state='awaiting_response')=>({topic,asked_on,state});
const root=fs.mkdtempSync(path.join(os.tmpdir(),'synthetic-requests-filter-'));
fs.cpSync('examples/fictional-campaign',root,{recursive:true,filter:p=>!p.includes(`${path.sep}public`)});
const file=path.join(root,'content','agencies.json');
const agencies=JSON.parse(fs.readFileSync(file,'utf8'));
const asked={[SHERIFF]:[it('Zeta','2026-09-01','answered'),it('Dup','2026-10-01')],
  [POLICE]:[it('Dup','2026-10-01','acknowledged'),it('Dup','2026-10-01','acknowledged'),it('Newest','2026-10-20','withdrawn')],
  [COUNCIL]:[it('Mid','2026-09-15','partly_answered')]};
for(const a of agencies){delete a.asked;if(asked[a.agency_id])a.asked=asked[a.agency_id];
  if(a.agency_id===COUNCIL)a.slug='all';}  // a real agency whose slug equals the old wildcard word
fs.writeFileSync(file,JSON.stringify(agencies));
const built=spawnSync('python3',['-B','-m','campaign_tool','build','--directory',root],{encoding:'utf8'});
if(built.status!==0)throw Error(built.stderr);
const pub=name=>fs.readFileSync(path.join(root,'public',name),'utf8');
const html=pub('requests.html'),SCRIPT=pub('requests/filter.js');

const strict=obj=>new Proxy(obj,{get(t,k){
  if(['innerHTML','outerHTML','insertAdjacentHTML','insertAdjacentElement'].includes(k))throw Error(`markup access: ${String(k)}`);return t[k];},
  set(t,k,v){if(['innerHTML','outerHTML'].includes(k))throw Error(`markup write: ${String(k)}`);t[k]=v;return true;}});

function setup({hash='',history=true,drop=null}={}){
  const body=html.split('<tbody>')[1].split('</tbody>')[0];
  const rows=[...body.matchAll(/<tr data-agency="([^"]*)" data-state="([^"]*)">/g)].map((m,i)=>strict({
    n:i,hidden:false,attrs:{'data-agency':m[1],'data-state':m[2]},getAttribute(k){return this.attrs[k];}}));
  const head=strict({hidden:false}),other=strict({hidden:false});  // a header row and an unrelated table row: never touched
  const revealOrder=[];
  const nodes={'requests-filter':strict({hidden:true,handlers:{},tpl:html.match(/data-count-template="([^"]*)"/)[1],
      getAttribute(k){return k==='data-count-template'?this.tpl:null;},addEventListener(e,f){this.handlers[e]=f;}}),
    'requests-filter-count':strict({textContent:'',set textContent(v){revealOrder.push(`count:form-hidden=${nodes['requests-filter'].hidden}`);this._t=v;},get textContent(){return this._t;}}),
    'requests-filter-empty':strict({hidden:true}),
    'requests-table':strict({querySelectorAll(sel){if(sel!=='tbody > tr')throw Error(`unexpected query: ${sel}`);return rows;}})};
  for(const m of html.matchAll(/<select id="(requests-filter-[a-z]+)"[^>]*>(.*?)<\/select>/gs))
    nodes[m[1]]=strict({value:'*',options:[...m[2].matchAll(/<option value="([^"]*)"/g)].map(o=>({value:o[1]}))});
  if(drop)delete nodes[drop];
  const replaced=[],windowHandlers={},location={hash,pathname:'/requests.html',search:'?lang=en'};
  const context={document:{getElementById:id=>nodes[id]||null,get cookie(){throw Error('cookie access');}},
    location,encodeURIComponent,decodeURIComponent,String,addEventListener:(e,f)=>{windowHandlers[e]=f;}};
  if(history)context.history={replaceState:(s,t,url)=>{replaced.push(url);location.hash=url.startsWith('#')?url:'';}};
  context.window=context;
  vm.createContext(context);
  const before=new Set(Object.keys(context));
  let error=null;
  try{vm.runInContext(SCRIPT,context,{filename:'filter.js'});}catch(e){error=e;}
  const added=Object.keys(context).filter(k=>!before.has(k));
  const visible=()=>rows.filter(r=>!r.hidden).map(r=>r.n);
  const set=(a,s)=>{nodes['requests-filter-agency'].value=a;nodes['requests-filter-state'].value=s;nodes['requests-filter'].handlers.change();};
  const hashchange=h=>{location.hash=h;windowHandlers.hashchange();};
  const sel=()=>[nodes['requests-filter-agency'].value,nodes['requests-filter-state'].value];
  return {nodes,rows,head,other,replaced,added,visible,set,hashchange,sel,error,revealOrder,location,context};
}
const count=s=>s.nodes['requests-filter-count'].textContent;
const slugs=[...new Set([...html.matchAll(/<tr data-agency="([^"]*)"/g)].map(m=>m[1]))];

test('script is first-party, small, text-only; the page loads it with a hidden form and the full table',()=>{
  assert.ok(Buffer.byteLength(SCRIPT)<4096);
  assert.doesNotMatch(SCRIPT,/https?:|\bimport\b|require\(|eval|Function\(|innerHTML|outerHTML|insertAdjacentHTML|fetch|XMLHttpRequest|cookie|localStorage|sessionStorage|\bon[a-z]+=/);
  assert.match(html,/<script defer src="\/requests\/filter\.js"><\/script>/);
  assert.match(html.match(/<form class="sources-filter" id="requests-filter"[^>]*>/)[0],/ hidden>$/);
  assert.equal(slugs.length,3);
  assert.equal((html.match(/<tr data-agency/g)||[]).length,6);
});

test('combined_filters_count_empty_reset',()=>{
  const s=setup();
  assert.equal(s.error,null);
  assert.deepEqual(s.revealOrder.at(-1),'count:form-hidden=true');  // applied before the form is revealed
  assert.equal(s.nodes['requests-filter'].hidden,false);
  assert.equal(count(s),'Showing 6 of 6');
  assert.equal(s.nodes['requests-filter-empty'].hidden,true);
  const police=slugs.find(x=>s.rows.some(r=>r.attrs['data-agency']===x&&r.attrs['data-state']==='withdrawn'));
  s.set(police,'*');
  assert.equal(s.visible().length,3);assert.equal(count(s),'Showing 3 of 6');  // duplicates counted separately
  s.set('*','acknowledged');
  assert.equal(s.visible().length,2);assert.equal(count(s),'Showing 2 of 6');
  s.set(police,'answered');  // conjunction with no row
  assert.equal(s.visible().length,0);assert.equal(count(s),'Showing 0 of 6');assert.equal(s.nodes['requests-filter-empty'].hidden,false);
  s.set(police,'*');  // clearing one dimension keeps the other
  assert.equal(s.visible().length,3);assert.equal(s.nodes['requests-filter-empty'].hidden,true);
  s.set('*','*');
  assert.equal(s.visible().length,6);assert.equal(count(s),'Showing 6 of 6');
  assert.equal(s.replaced.at(-1),'/requests.html?lang=en');  // reset keeps the query, drops the hash
  assert.equal(s.head.hidden,false);assert.equal(s.other.hidden,false);
  assert.deepEqual(s.added,[]);  // no leaked globals
});

test('hash_roundtrip_validation_and_back_forward',()=>{
  const s=setup();
  const a=slugs[0];
  s.set(a,'answered');
  assert.equal(s.replaced.at(-1),`#agency=${encodeURIComponent(a)}&state=answered`);  // canonical order
  const roundtrip=setup({hash:`#state=withdrawn&agency=${a}`});
  assert.deepEqual(roundtrip.sel(),[a,'withdrawn']);
  assert.equal(roundtrip.replaced.at(-1),`#agency=${encodeURIComponent(a)}&state=withdrawn`);  // normalised
  for(const h of ['#bogus=1','#agency','#state','#agency=nope','#state=nope&agency=%E0%A4%A','#content']){
    const t=setup({hash:h});assert.deepEqual(t.sel(),['*','*'],h);assert.equal(count(t),'Showing 6 of 6',h);
    if(h==='#bogus=1'||h==='#content')assert.equal(t.replaced.length,0,h);  // unknown keys/anchors are left alone
  }
  assert.equal(setup({hash:'#state=nope'}).replaced.at(-1),'/requests.html?lang=en');  // recognised key, invalid value
  assert.deepEqual(setup({hash:`#state=nope&state=answered&state=zzz`}).sel(),['*','answered']);  // last valid wins
  assert.deepEqual(setup({hash:'#state=all&agency=none'}).sel(),['*','*']);  // 'all' is no longer a wildcard: state=all is invalid; agency=all would select the real agency
  const h=setup({hash:`#agency=${a}`});
  h.hashchange('#state=withdrawn');  // fresh defaults on every hash load
  assert.deepEqual(h.sel(),['*','withdrawn']);
  h.hashchange('');
  assert.deepEqual(h.sel(),['*','*']);assert.equal(count(h),'Showing 6 of 6');
  const noHistory=setup({history:false});
  noHistory.set('*','answered');
  assert.equal(noHistory.visible().length,1);assert.equal(noHistory.replaced.length,0);
  for(const drop of ['requests-filter','requests-filter-count','requests-filter-empty','requests-table','requests-filter-state']){
    const t=setup({drop});assert.equal(t.error,null,drop);assert.equal(t.visible().length,6,drop);  // missing node: no-op
  }
});

test('agency_slug_all_is_selectable_and_roundtrips_in_the_hash',()=>{
  const s=setup();
  assert.ok(slugs.includes('all'));
  s.set('all','*');
  assert.deepEqual(s.visible().length,1);assert.equal(count(s),'Showing 1 of 6');  // only the council row, not everything
  assert.equal(s.replaced.at(-1),'#agency=all');
  const back=setup({hash:'#agency=all'});
  assert.deepEqual(back.sel(),['all','*']);assert.equal(count(back),'Showing 1 of 6');
  assert.equal(back.replaced.at(-1),'#agency=all');
  s.set('*','*');
  assert.equal(count(s),'Showing 6 of 6');assert.equal(s.replaced.at(-1),'/requests.html?lang=en');
  assert.deepEqual(setup({hash:'#agency=*'}).sel(),['*','*']);
});
