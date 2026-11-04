import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';

// DF1103-12: build the fictional example with a synthetic five-record library, then run the generated sources/filter.js
// in a mocked DOM parsed from the built sources.html. No browser, no network, nothing private.
const POLICE='ca-cedar-police',SHERIFF='ca-cedar-county-sheriff',UNLISTED='ca-zz-unlisted';
const root=fs.mkdtempSync(path.join(os.tmpdir(),'synthetic-sources-filter-'));
fs.cpSync('examples/fictional-campaign',root,{recursive:true,filter:p=>!p.includes(`${path.sep}public`)});
const content=path.join(root,'content');
const library=JSON.parse(fs.readFileSync(path.join(content,'sources.json'),'utf8'));
const entry=(sha,fragment,agency,received,title)=>({sha256:sha,title,agency_id:agency,received_at:received,public_url:null,fragment});
library.push(entry('1a2b'.repeat(16),'cedar-sheriff-records',SHERIFF,'2025-12-31T23:30:00-08:00','Synthetic sheriff edge record'),
  entry('3c4d'.repeat(16),'cedar-sheriff-records',SHERIFF,'2024-06-01T10:00:00+00:00','Synthetic sheriff older record'),
  entry('5e6f'.repeat(16),'unlisted-records',UNLISTED,'2024-02-02T08:00:00+00:00','Synthetic unlisted record'));
fs.writeFileSync(path.join(content,'sources.json'),JSON.stringify(library));
const base=JSON.parse(fs.readFileSync(path.join(content,'findings','cedar-policy-posted.json'),'utf8'));
fs.writeFileSync(path.join(content,'findings','synthetic-sheriff-cites.json'),JSON.stringify({...base,id:'fnd_0000000000000002',
  slug:'synthetic-sheriff-cites',title:'Synthetic sheriff cites',corrections:[],
  sources:[{sha256:'3c4d'.repeat(16),locator:'page:1',title:'Synthetic sheriff older record'}]}));

function build(dir){
  const built=spawnSync('python3',['-B','-m','campaign_tool','build','--directory',dir],{encoding:'utf8'});
  if(built.status!==0)throw Error(built.stderr);
  return name=>fs.readFileSync(path.join(dir,'public',name),'utf8');
}
const pub=build(root);
const withLabels=labels=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'synthetic-sources-filter-labels-'));
  fs.cpSync(root,dir,{recursive:true,filter:p=>!p.includes(`${path.sep}public`)});
  fs.writeFileSync(path.join(dir,'content','labels.json'),JSON.stringify({schema_version:1,labels}));
  return build(dir);
};
const html=pub('sources.html');
const FIVE=5,SELECTOR='.source-table > li.source-entry';

// A mock element that refuses any markup-writing property: the script may only use textContent and hidden.
const strict=obj=>new Proxy(obj,{get(target,key){
  if(['innerHTML','outerHTML','insertAdjacentHTML','insertAdjacentElement'].includes(key))throw Error(`markup access: ${String(key)}`);
  return target[key];},
  set(target,key,value){
    if(['innerHTML','outerHTML'].includes(key))throw Error(`markup write: ${String(key)}`);
    if(target.onSet)target.onSet(key,value);
    target[key]=value;return true;}});

function setup({hash='',code=pub('sources/filter.js'),data=pub('site-data.js'),page=html,history=true,loadData=true}={}){
  const groups=[...page.matchAll(/<section class="agency-document-group" id="([^"]*)">(.*?)<\/section>/gs)].map(g=>{
    const rows=[...g[2].matchAll(/<li class="source-entry" data-agency="([^"]*)" data-year="([^"]*)" data-cited="([^"]*)" data-record="([^"]*)">/g)].map(m=>strict({
      attrs:{'data-agency':m[1],'data-year':m[2],'data-cited':m[3],'data-record':m[4]},hidden:false,getAttribute(k){return this.attrs[k];}}));
    const decoy=strict({hidden:false,getAttribute(){return null;}});  // a nested citation item: never a record
    return strict({id:g[1],hidden:false,rows,decoy,querySelectorAll(sel){
      if(sel!==SELECTOR)throw Error(`unexpected group query: ${sel}`);return this.rows;}});
  });
  const revealOrder=[];
  const nodes={'sources-filter':strict({hidden:true,handlers:{},addEventListener(e,f){this.handlers[e]=f;}}),
    'sources-filter-count':strict({textContent:'',onSet(k){if(k==='textContent')revealOrder.push(`count:form-hidden=${nodes['sources-filter'].hidden}`);}}),
    'sources-filter-empty':strict({hidden:true}),'sources-filter-cited':strict({checked:false})};
  for(const m of page.matchAll(/<select id="(sources-filter-[a-z]+)"[^>]*>(.*?)<\/select>/gs))
    nodes[m[1]]=strict({value:'all',options:[...m[2].matchAll(/<option value="([^"]*)">/g)].map(o=>({value:o[1]}))});
  const replaced=[],windowHandlers={};
  const location={hash,pathname:'/sources.html',search:'?lang=en'};
  const context={document:{getElementById:id=>nodes[id]||null,
      querySelectorAll:s=>{if(s!=='.agency-document-group')throw Error(`unexpected document query: ${s}`);return groups;},
      get cookie(){throw Error('cookie access');}},
    location,encodeURIComponent,decodeURIComponent,String,
    addEventListener:(e,f)=>{windowHandlers[e]=f;}};
  if(history)context.history={replaceState:(s,t,url)=>{replaced.push(url);location.hash=url.startsWith('#')?url:'';}};
  context.window=context;
  const before=new Set(Object.keys(context));
  vm.createContext(context);
  if(loadData)vm.runInContext(data,context,{filename:'site-data.js'});
  const loaded=new Set(Object.keys(context));
  let error=null;
  try{vm.runInContext(code,context,{filename:'filter.js'});}catch(e){error=e;}
  const added=Object.keys(context).filter(k=>!loaded.has(k));
  const rows=groups.flatMap(g=>g.rows);
  const visible=()=>rows.filter(r=>!r.hidden).map(r=>r.attrs['data-record']);
  const hiddenGroups=()=>groups.filter(g=>g.hidden).map(g=>g.id);
  const change=()=>nodes['sources-filter'].handlers.change();
  const hashchange=h=>{location.hash=h;windowHandlers.hashchange();};
  const state=()=>({agency:nodes['sources-filter-agency'].value,year:nodes['sources-filter-year'].value,cited:nodes['sources-filter-cited'].checked});
  return {context,nodes,groups,rows,replaced,added,visible,hiddenGroups,change,hashchange,state,error,revealOrder,windowHandlers,location,before};
}
const ALL=['cafecafecafe','beefbeefbeef','1a2b1a2b1a2b','3c4d3c4d3c4d','5e6f5e6f5e6f'];

test('filter.js is first-party, small, text-only and loaded only by sources.html after site-data.js',()=>{
  const script=pub('sources/filter.js');
  assert.ok(Buffer.byteLength(script)<4096);
  assert.match(html,/<script defer src="\/site-data\.js"><\/script>\n {2}<script defer src="\/sources\/filter\.js"><\/script>/);
  for(const page of ['index.html','findings/index.html','agencies.html','sources/cafecafecafe.html','search.html'])
    assert.doesNotMatch(pub(page),/sources\/filter\.js|sources-filter/,page);
  assert.doesNotMatch(script,/https?:|\bimport\b|require\(|eval|innerHTML|outerHTML|fetch|XMLHttpRequest|cookie|localStorage|sessionStorage|\bon[a-z]+=/);
  assert.match(script,/textContent/);
  assert.doesNotMatch(script,/Showing/);  // the count always comes from LABELS
  const form=html.match(/<form class="sources-filter"[^>]*>/)[0];
  assert.match(form,/ hidden>$/);  // hidden until the script has applied the filter once
});

test('first application happens before the form is revealed; only the form changes; an unrelated anchor is left alone',()=>{
  const s=setup({hash:'#cedar-police-records'});
  assert.equal(s.error,null);
  assert.equal(s.rows.length,FIVE);
  assert.deepEqual(s.revealOrder,['count:form-hidden=true']);
  assert.equal(s.nodes['sources-filter'].hidden,false);
  assert.deepEqual(s.visible(),ALL);
  assert.equal(s.nodes['sources-filter-count'].textContent,'Showing 5 of 5');
  assert.equal(s.nodes['sources-filter-empty'].hidden,true);
  assert.equal(s.replaced.length,0);  // group anchors are preserved, not normalized
  assert.equal(s.location.hash,'#cedar-police-records');
  assert.deepEqual(s.added,[]);  // nothing is exposed on window
  assert.equal(s.groups.every(g=>g.hidden===false&&g.decoy.hidden===false),true);
  assert.equal(setup().nodes['sources-filter-agency'].options.map(o=>o.value).join(),`all,${UNLISTED},${SHERIFF},${POLICE}`);
  assert.equal(setup().nodes['sources-filter-year'].options.map(o=>o.value).join(),'all,2026,2025,2024');
});

test('each dimension alone, the conjunction, the cited toggle both ways, and the reset',()=>{
  const s=setup();
  s.nodes['sources-filter-agency'].value=SHERIFF;s.change();
  assert.deepEqual(s.visible(),['1a2b1a2b1a2b','3c4d3c4d3c4d']);
  assert.equal(s.nodes['sources-filter-count'].textContent,'Showing 2 of 5');
  assert.deepEqual(s.hiddenGroups(),['cedar-police-records','unlisted-records']);  // groups with no visible record vanish
  assert.equal(s.replaced.at(-1),`#agency=${SHERIFF}`);
  s.nodes['sources-filter-agency'].value='all';s.nodes['sources-filter-year'].value='2024';s.change();
  assert.deepEqual(s.visible(),['3c4d3c4d3c4d','5e6f5e6f5e6f']);
  assert.equal(s.nodes['sources-filter-count'].textContent,'Showing 2 of 5');
  assert.equal(s.replaced.at(-1),'#year=2024');
  s.nodes['sources-filter-year'].value='2025';s.change();  // the stored calendar year, not the UTC one
  assert.deepEqual(s.visible(),['1a2b1a2b1a2b']);
  s.nodes['sources-filter-year'].value='all';s.nodes['sources-filter-cited'].checked=true;s.change();
  assert.deepEqual(s.visible(),['cafecafecafe','3c4d3c4d3c4d']);
  assert.equal(s.replaced.at(-1),'#cited=1');
  s.nodes['sources-filter-agency'].value=SHERIFF;s.nodes['sources-filter-year'].value='2024';s.change();
  assert.deepEqual(s.visible(),['3c4d3c4d3c4d']);
  assert.equal(s.nodes['sources-filter-count'].textContent,'Showing 1 of 5');  // records, not groups
  assert.deepEqual(s.hiddenGroups(),['cedar-police-records','unlisted-records']);
  assert.equal(s.replaced.at(-1),`#agency=${SHERIFF}&year=2024&cited=1`);
  s.nodes['sources-filter-cited'].checked=false;s.nodes['sources-filter-year'].value='all';s.change();  // the uncited record comes back
  assert.deepEqual(s.visible(),['1a2b1a2b1a2b','3c4d3c4d3c4d']);
  assert.equal(s.groups.every(g=>g.decoy.hidden===false),true);  // nested citation items are never filtered
  s.nodes['sources-filter-agency'].value='all';s.change();
  assert.deepEqual(s.visible(),ALL);
  assert.deepEqual(s.hiddenGroups(),[]);
  assert.equal(s.replaced.at(-1),'/sources.html?lang=en');  // all-default user change keeps the query string
});

test('no match: zero records, every group hidden, the empty message appears and goes away again',()=>{
  const s=setup();
  s.nodes['sources-filter-agency'].value=SHERIFF;s.nodes['sources-filter-year'].value='2026';s.change();
  assert.deepEqual(s.visible(),[]);
  assert.equal(s.nodes['sources-filter-count'].textContent,'Showing 0 of 5');
  assert.equal(s.nodes['sources-filter-empty'].hidden,false);
  assert.equal(s.hiddenGroups().length,3);
  s.nodes['sources-filter-agency'].value=UNLISTED;s.nodes['sources-filter-year'].value='all';s.nodes['sources-filter-cited'].checked=true;s.change();
  assert.equal(s.nodes['sources-filter-empty'].hidden,false);  // the unlisted record is not cited
  s.nodes['sources-filter-cited'].checked=false;s.change();
  assert.deepEqual(s.visible(),['5e6f5e6f5e6f']);
  assert.equal(s.nodes['sources-filter-empty'].hidden,true);
});

test('initial hash: ordered, normalized, last valid duplicate wins, invalid and unknown parts ignored',()=>{
  let s=setup({hash:`#cited=1&year=2024&agency=${SHERIFF}`});
  assert.deepEqual(s.state(),{agency:SHERIFF,year:'2024',cited:true});
  assert.deepEqual(s.visible(),['3c4d3c4d3c4d']);
  assert.deepEqual(s.replaced,[`#agency=${SHERIFF}&year=2024&cited=1`]);
  s=setup({hash:`#agency=nope&agency=${POLICE}&agency=also-nope&agency=${SHERIFF}&agency=%E0%A4%A`});
  assert.equal(s.state().agency,SHERIFF);  // the last VALID value
  assert.deepEqual(s.replaced,[`#agency=${SHERIFF}`]);
  s=setup({hash:'#agency=bogus&year=1999&cited=yes'});  // recognized keys with invalid values normalize to the defaults
  assert.deepEqual(s.state(),{agency:'all',year:'all',cited:false});
  assert.deepEqual(s.visible(),ALL);
  assert.deepEqual(s.replaced,['/sources.html?lang=en']);
  s=setup({hash:'#year=2025&bogus=1&agency=%E0%A4%A&cited=0&=x&agency'});  // malformed escape, unknown key, cited=0, bare key
  assert.deepEqual(s.state(),{agency:'all',year:'2025',cited:false});
  assert.deepEqual(s.replaced,['#year=2025']);
  s=setup({hash:'#bogus=1&other'});  // no recognized key: full list, hash untouched
  assert.deepEqual(s.visible(),ALL);
  assert.equal(s.replaced.length,0);
  s=setup({hash:`#agency=${encodeURIComponent(POLICE)}&year=all`});
  assert.equal(s.state().agency,POLICE);
  assert.deepEqual(s.replaced,[`#agency=${POLICE}`]);
});

test('hashchange (back/forward) reads fresh defaults; an anchor restores the full list without normalization',()=>{
  const s=setup({hash:`#agency=${POLICE}`});
  assert.deepEqual(s.visible(),['cafecafecafe','beefbeefbeef']);
  s.hashchange('#year=2024');  // the agency selection from before is NOT carried over
  assert.deepEqual(s.state(),{agency:'all',year:'2024',cited:false});
  assert.deepEqual(s.visible(),['3c4d3c4d3c4d','5e6f5e6f5e6f']);
  const calls=s.replaced.length;
  s.hashchange('#cedar-police-records');  // a group anchor
  assert.deepEqual(s.state(),{agency:'all',year:'all',cited:false});
  assert.deepEqual(s.visible(),ALL);
  assert.equal(s.nodes['sources-filter-count'].textContent,'Showing 5 of 5');
  assert.equal(s.replaced.length,calls);
  assert.equal(s.location.hash,'#cedar-police-records');
  s.hashchange('#cited=1&agency=gone');
  assert.deepEqual(s.visible(),['cafecafecafe','3c4d3c4d3c4d']);
  assert.equal(s.replaced.at(-1),'#cited=1');  // normalization uses replaceState, so it cannot loop
  s.hashchange('');
  assert.deepEqual(s.visible(),ALL);
  assert.equal(s.replaced.at(-1),'#cited=1');
  s.hashchange(`#agency=${SHERIFF}`);
  s.nodes['sources-filter-cited'].checked=true;s.change();
  assert.equal(s.replaced.at(-1),`#agency=${SHERIFF}&cited=1`);
});

test('a user change drops a group anchor from the hash; without the History API filtering still works and nothing navigates',()=>{
  const s=setup({hash:'#cedar-police-records'});
  s.nodes['sources-filter-cited'].checked=true;s.change();
  assert.deepEqual(s.replaced,['#cited=1']);
  s.nodes['sources-filter-cited'].checked=false;s.change();
  assert.equal(s.replaced.at(-1),'/sources.html?lang=en');
  const bare=setup({hash:'#year=2025',history:false});
  assert.equal(bare.error,null);
  assert.deepEqual(bare.visible(),['1a2b1a2b1a2b']);
  bare.nodes['sources-filter-year'].value='all';bare.change();
  assert.deepEqual(bare.visible(),ALL);
  assert.equal(bare.location.hash,'#year=2025');  // the script never assigns location.hash itself
});

test('without the form the script does nothing: no row queries, no history writes',()=>{
  const context={document:{getElementById:()=>null,querySelectorAll:()=>{throw Error('must not query');}},
    location:{get hash(){throw Error('must not read');}},history:{replaceState:()=>{throw Error('must not write');}},
    addEventListener:()=>{throw Error('must not listen');}};
  context.window=context;vm.createContext(context);
  vm.runInContext(pub('sources/filter.js'),context);
  assert.deepEqual(Object.keys(context).sort(),['addEventListener','document','history','location','window']);
});

test('the count comes from LABELS: default, custom, omitted placeholder, quotes and backslashes; LABELS loads first',()=>{
  assert.match(pub('site-data.js'),/^var LABELS=\{"js\.filter_count":"Showing \{shown\} of \{total\}"\};$/m);
  const s=setup();
  s.nodes['sources-filter-cited'].checked=true;s.change();
  assert.equal(s.nodes['sources-filter-count'].textContent,'Showing 2 of 5');
  const es=withLabels({'js.filter_count':'Mostrando {shown} de {total}','sources.filter_cited':'Citado por un hallazgo'});
  assert.match(es('sources.html'),/<script defer src="\/site-data\.js"><\/script>\n {2}<script defer src="\/sources\/filter\.js"><\/script>/);
  assert.match(es('sources.html'),/Citado por un hallazgo/);
  const spanish=setup({page:es('sources.html'),data:es('site-data.js'),code:es('sources/filter.js')});
  assert.equal(spanish.nodes['sources-filter-count'].textContent,'Mostrando 5 de 5');
  spanish.nodes['sources-filter-agency'].value=SHERIFF;spanish.change();
  assert.equal(spanish.nodes['sources-filter-count'].textContent,'Mostrando 2 de 5');
  assert.doesNotMatch(es('sources/filter.js'),/Mostrando|Showing/);
  const partial=withLabels({'js.filter_count':'{total} en total'});
  assert.equal(setup({page:partial('sources.html'),data:partial('site-data.js')}).nodes['sources-filter-count'].textContent,'5 en total');
  const hostile=withLabels({'js.filter_count':'Mostrando "{shown}" \\ de {total} /script/'});
  assert.equal(setup({page:hostile('sources.html'),data:hostile('site-data.js')}).nodes['sources-filter-count'].textContent,'Mostrando "5" \\ de 5 /script/');
  const nodata=setup({loadData:false});  // LABELS missing: the first application fails, so the form stays hidden
  assert.ok(nodata.error);
  assert.equal(nodata.nodes['sources-filter'].hidden,true);
  assert.deepEqual(nodata.visible(),ALL);
});

test('a hostile agency name stays text: the filter never writes markup and the form markup is escaped',()=>{
  const names=fs.mkdtempSync(path.join(os.tmpdir(),'synthetic-sources-filter-names-'));
  fs.cpSync(root,names,{recursive:true,filter:p=>!p.includes(`${path.sep}public`)});
  const file=path.join(names,'content','agencies.json');
  const agencies=JSON.parse(fs.readFileSync(file,'utf8'));
  agencies.find(a=>a.agency_id===SHERIFF).name='Zeta "Sheriff" & Co';
  fs.writeFileSync(file,JSON.stringify(agencies));
  const rebuilt=build(names);
  const page=rebuilt('sources.html');
  assert.match(page,/<option value="ca-cedar-county-sheriff">Zeta &quot;Sheriff&quot; &amp; Co<\/option>/);
  const s=setup({page,data:rebuilt('site-data.js'),code:rebuilt('sources/filter.js'),hash:`#agency=${SHERIFF}`});
  assert.equal(s.error,null);  // the strict mock throws on any innerHTML access
  assert.deepEqual(s.visible(),['1a2b1a2b1a2b','3c4d3c4d3c4d']);
});
