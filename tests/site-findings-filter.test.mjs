import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';

// RF1510-3: build the fictional example plus five synthetic findings, then run the generated
// findings/filter.js in a mocked DOM parsed from the built findings/index.html. No browser, no network.
const root=fs.mkdtempSync(path.join(os.tmpdir(),'synthetic-filter-'));
fs.cpSync('examples/fictional-campaign',root,{recursive:true,filter:p=>!p.includes(`${path.sep}public`)});
const dir=path.join(root,'content','findings');
const base=JSON.parse(fs.readFileSync(path.join(dir,'cedar-policy-posted.json'),'utf8'));
const POLICE='ca-cedar-police',SHERIFF='ca-cedar-county-sheriff';
[['two',POLICE,'apparent_conflict','2025-06-01'],['three',SHERIFF,'information_gap','2026-03-02'],
 ['four',SHERIFF,'documented_fact','2025-11-20'],['five',null,'apparent_conflict','2026-04-01'],
 ['six',SHERIFF,'information_gap','2025-02-02']].forEach(([slug,agency,code,day],n)=>{
  const doc={...base,slug:`synthetic-${slug}`,id:`fnd_${String(n+2).padStart(16,'0')}`,title:`Synthetic ${slug}`,
    corrections:[],agency_id:agency,classification:code,event_date:day};
  if(code==='apparent_conflict')Object.assign(doc,{rule_version:'synthetic-v1',duty:'Synthetic duty.',exceptions:['None.']});
  fs.writeFileSync(path.join(dir,`synthetic-${slug}.json`),JSON.stringify(doc));
});
const built=spawnSync('python3',['-B','-m','campaign_tool','build','--directory',root],{encoding:'utf8'});
if(built.status!==0)throw Error(built.stderr);
const pub=name=>fs.readFileSync(path.join(root,'public',name),'utf8');
const html=pub('findings/index.html');

function setup(hash=''){
  const items=[...html.matchAll(/<li data-agency="([^"]*)" data-classification="([^"]*)" data-year="([^"]*)">/g)].map(m=>({
    attrs:{'data-agency':m[1],'data-classification':m[2],'data-year':m[3]},hidden:false,getAttribute(k){return this.attrs[k];}}));
  const nodes={'finding-filter':{hidden:true,handlers:{},addEventListener(e,f){this.handlers[e]=f;}},
    'filter-count':{textContent:''},'filter-empty':{hidden:true}};
  for(const m of html.matchAll(/<select id="(filter-[a-z]+)"[^>]*>(.*?)<\/select>/g))
    nodes[m[1]]={value:'all',options:[...m[2].matchAll(/<option value="([^"]*)">/g)].map(o=>({value:o[1]}))};
  const replaced=[];
  const context={document:{getElementById:id=>nodes[id]||null,querySelectorAll:s=>s==='.finding-list li'?items:[]},
    location:{hash,pathname:'/findings/',search:''},history:{replaceState:(s,t,url)=>replaced.push(url)},
    encodeURIComponent,decodeURIComponent};
  context.window=context;
  const before=new Set(Object.keys(context));
  vm.createContext(context);
  vm.runInContext(pub('findings/filter.js'),context,{filename:'filter.js'});
  const added=Object.keys(context).filter(k=>!before.has(k));
  const visible=()=>items.filter(i=>!i.hidden).map(i=>i.attrs);
  return {context,nodes,items,replaced,added,visible};
}

test('filter.js is first-party, small and loaded only by the findings index',()=>{
  assert.ok(Buffer.byteLength(pub('findings/filter.js'))<2048);
  assert.match(html,/<script defer src="\/findings\/filter\.js"><\/script>/);
  assert.doesNotMatch(pub('index.html'),/filter\.js|data-agency/);
  assert.doesNotMatch(pub('findings/filter.js'),/https?:|import|require\(|eval|innerHTML/);
});

test('reveals the form, shows all, exposes only findingFilter, leaves an unrelated hash alone',()=>{
  const {nodes,items,replaced,added,visible}=setup('#content');
  assert.equal(items.length,6);
  assert.equal(nodes['finding-filter'].hidden,false);
  assert.equal(visible().length,6);
  assert.equal(nodes['filter-count'].textContent,'Showing 6 of 6');
  assert.equal(nodes['filter-empty'].hidden,true);
  assert.equal(replaced.length,0);
  assert.deepEqual(added,['findingFilter']);
  assert.deepEqual(Object.keys(setup().context.findingFilter),['apply']);
});

test('agency plus classification narrows; no match shows the empty message; reset shows all',()=>{
  const {nodes,replaced,visible}=setup();
  nodes['filter-agency'].value=SHERIFF;nodes['filter-classification'].value='information_gap';
  nodes['finding-filter'].handlers.change();
  const shown=visible();
  assert.equal(shown.length,2);
  assert.ok(shown.every(a=>a['data-agency']===SHERIFF&&a['data-classification']==='information_gap'));
  assert.equal(nodes['filter-count'].textContent,'Showing 2 of 6');
  assert.equal(nodes['filter-empty'].hidden,true);
  assert.equal(replaced.at(-1),`#agency=${SHERIFF}&classification=information_gap`);
  nodes['filter-agency'].value='none';nodes['filter-classification'].value='documented_fact';
  nodes['finding-filter'].handlers.change();
  assert.equal(visible().length,0);
  assert.equal(nodes['filter-count'].textContent,'Showing 0 of 6');
  assert.equal(nodes['filter-empty'].hidden,false);
  nodes['filter-agency'].value='all';nodes['filter-classification'].value='all';
  nodes['finding-filter'].handlers.change();
  assert.equal(visible().length,6);
  assert.equal(nodes['filter-count'].textContent,'Showing 6 of 6');
  assert.equal(nodes['filter-empty'].hidden,true);
  assert.equal(replaced.at(-1),'/findings/');
});

test('hash sets the initial state, unknown values are ignored and the hash is normalized',()=>{
  const {nodes,replaced,visible}=setup(`#agency=${POLICE}&year=1999`);
  assert.equal(nodes['filter-agency'].value,POLICE);
  assert.equal(nodes['filter-year'].value,'all');
  assert.equal(visible().length,2);
  assert.ok(visible().every(a=>a['data-agency']===POLICE));
  assert.deepEqual(replaced,[`#agency=${POLICE}`]);
  const year=setup('#year=2025&classification=no-such&agency=%E0%A4%A&bogus=1');  // malformed escape ignored
  assert.equal(year.nodes['filter-agency'].value,'all');
  assert.equal(year.nodes['filter-classification'].value,'all');
  assert.equal(year.visible().length,3);
  assert.deepEqual(year.replaced,['#year=2025']);
  const none=setup('#agency=all&classification=nope');
  assert.equal(none.visible().length,6);
  assert.deepEqual(none.replaced,['/findings/']);
});

test('without the form (a page with too few findings) the script does nothing',()=>{
  const context={document:{getElementById:()=>null,querySelectorAll:()=>{throw Error('must not query');}},
    location:{hash:'#agency=x'},history:{replaceState:()=>{throw Error('must not write');}}};
  context.window=context;vm.createContext(context);
  vm.runInContext(pub('findings/filter.js'),context);
  assert.equal(context.findingFilter,undefined);
});
