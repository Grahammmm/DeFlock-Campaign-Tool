import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';

// DF1622-09: build the fictional example with synthetic meetings from two bodies, then run the generated
// meetings/filter.js in a mock DOM parsed from the built meetings.html. No browser, no network, nothing private.
const root=fs.mkdtempSync(path.join(os.tmpdir(),'synthetic-meetings-filter-'));
fs.cpSync('examples/fictional-campaign',root,{recursive:true,filter:p=>!p.includes(`${path.sep}public`)});
const dir=path.join(root,'content','meetings');
const add=(id,body,day,extra={})=>fs.writeFileSync(path.join(dir,`${id}.json`),JSON.stringify({id,body,starts_at:`${day}T18:00:00-08:00`,agency_id:'ca-cedar-city-council',...extra}));
add('alpha-up','Council A','2099-11-10');add('beta-up','Council B','2099-11-11');
add('alpha-old','Council A','2000-10-01');add('beta-cancel','Council B','2099-11-12',{status:'cancelled'});
const built=spawnSync('python3',['-B','-m','campaign_tool','build','--directory',root],{encoding:'utf8'});
if(built.status!==0)throw Error(built.stderr);
const pub=name=>fs.readFileSync(path.join(root,'public',name),'utf8');
const html=pub('meetings.html'),script=pub('meetings/filter.js');

const strict=obj=>new Proxy(obj,{get(t,k){
  if(['innerHTML','outerHTML','insertAdjacentHTML','insertAdjacentElement'].includes(k))throw Error(`markup access: ${String(k)}`);
  return t[k];},
  set(t,k,v){if(['innerHTML','outerHTML'].includes(k))throw Error(`markup write: ${String(k)}`);if(t.onSet)t.onSet(k,v);t[k]=v;return true;}});

function setup({hash='',history=true,drop=[],page=html}={}){
  const listOf=key=>{
    const m=page.match(new RegExp(`<div class="meeting-list" id="meetings-filter-${key}">(.*?)</div></section>`,'s'));
    if(!m)return null;
    const rows=[...m[1].matchAll(/<div class="meeting-filter-item" data-body="([^"]*)">/g)].map(r=>strict({className:'meeting-filter-item',hidden:false,attrs:{'data-body':r[1]},getAttribute(k){return this.attrs[k];}}));
    const decoy=strict({className:'other',hidden:false,getAttribute(){return null;}});  // a non-wrapper child is never a row
    return {rows,decoy};
  };
  const nodes={},lists={};
  const order=[];
  for(const key of ['upcoming','past']){
    const l=listOf(key);if(!l)continue;lists[key]=l;
    nodes[`meetings-filter-${key}`]=strict({children:[l.decoy,...l.rows]});
    nodes[`meetings-filter-${key}-count`]=strict({hidden:true,textContent:'',onSet(k){if(k==='textContent')order.push(`${key}:form-hidden=${nodes['meetings-filter'].hidden}`);}});
    nodes[`meetings-filter-${key}-empty`]=strict({hidden:true});
  }
  const select=page.match(/<select id="meetings-filter-body"[^>]*>(.*?)<\/select>/s);
  nodes['meetings-filter-body']=strict({value:'all',options:[...select[1].matchAll(/<option value="([^"]*)">/g)].map(o=>({value:o[1]}))});
  const template=page.match(/data-count-template="([^"]*)"/)[1];
  nodes['meetings-filter']=strict({hidden:true,handlers:{},addEventListener(e,f){this.handlers[e]=f;},getAttribute(k){return k==='data-count-template'?template:null;}});
  for(const id of drop)delete nodes[id];
  const replaced=[],windowHandlers={};
  const location={hash,pathname:'/meetings.html',search:'?lang=en'};
  const context={document:{getElementById:id=>nodes[id]||null,get cookie(){throw Error('cookie access');}},
    location,encodeURIComponent,decodeURIComponent,String,addEventListener:(e,f)=>{windowHandlers[e]=f;}};
  if(history)context.history={replaceState:(s,t,url)=>{replaced.push(url);location.hash=url.startsWith('#')?url:'';}};
  context.window=context;
  vm.createContext(context);
  const before=new Set(Object.keys(context));
  let error=null;
  try{vm.runInContext(script,context,{filename:'filter.js'});}catch(e){error=e;}
  const added=Object.keys(context).filter(k=>!before.has(k));
  const shown=key=>lists[key].rows.filter(r=>!r.hidden).map(r=>r.attrs['data-body']);
  return {nodes,lists,replaced,added,error,order,shown,location,windowHandlers,
    pick(v){nodes['meetings-filter-body'].value=v;nodes['meetings-filter'].handlers.change();},
    hashchange(h){location.hash=h;windowHandlers.hashchange();},
    text:key=>(nodes[`meetings-filter-${key}-count`]||{}).textContent,
    note:key=>nodes[`meetings-filter-${key}-empty`].hidden};
}

test('script is small, ES5, text-only; the page loads it once, after nothing else, with lists complete and the form hidden',()=>{
  assert.ok(Buffer.byteLength(script)<4096);
  assert.doesNotMatch(script,/https?:|\bimport\b|require\(|eval|Function\(|innerHTML|outerHTML|insertAdjacent|fetch|XMLHttpRequest|cookie|localStorage|sessionStorage|=>|\blet\b|\bconst\b|\bon[a-z]+=/);
  assert.match(script,/textContent/);
  assert.doesNotMatch(script,/Showing|LABELS/);
  assert.equal((html.match(/<script defer src="\/meetings\/filter\.js"><\/script>/g)||[]).length,1);
  assert.doesNotMatch(html,/site-data\.js/);
  assert.match(html.match(/<form class="sources-filter" id="meetings-filter"[^>]*>/)[0],/ hidden>$/);
  assert.match(html,/id="meetings-filter-upcoming-count"[^>]* hidden>Showing 2 of 2</);  // alpha-up and beta-up; the cancelled one is listed in the past section
  assert.equal((html.match(/<article id="meeting-/g)||[]).length,(html.match(/class="meeting-filter-item"/g)||[]).length);
  assert.match(html,/Cancelled|cancelled/i);
});

test('first application precedes the reveal; each body narrows both lists with separate counts; cancelled stays counted',()=>{
  const s=setup();
  assert.equal(s.error,null);
  assert.deepEqual(s.order.every(o=>o.endsWith('form-hidden=true')),true);
  assert.equal(s.nodes['meetings-filter'].hidden,false);
  assert.equal(s.nodes['meetings-filter-upcoming-count'].hidden,false);
  assert.deepEqual(s.added,[]);
  const values=s.nodes['meetings-filter-body'].options.map(o=>o.value);
  assert.ok(values[0]==='all'&&values.includes('council-a')&&values.includes('council-b'));
  assert.equal(s.text('upcoming'),'Showing 2 of 2');assert.equal(s.text('past'),'Showing 3 of 3');
  s.pick('council-b');
  assert.deepEqual(s.shown('upcoming'),['council-b']);
  assert.deepEqual(s.shown('past'),['council-b']);  // the cancelled meeting stays listed (its marking is inside the unchanged article)
  assert.equal(s.text('upcoming'),'Showing 1 of 2');assert.equal(s.text('past'),'Showing 1 of 3');
  assert.equal(s.note('past'),true);assert.equal(s.note('upcoming'),true);
  assert.equal(s.replaced.at(-1),'#body=council-b');
  s.pick('cedar-city-council-fictional');  // zero upcoming, one past: separate denominators and notes
  assert.equal(s.text('upcoming'),'Showing 0 of 2');assert.equal(s.note('upcoming'),false);
  assert.equal(s.text('past'),'Showing 1 of 3');assert.equal(s.note('past'),true);
  s.pick('council-a');
  assert.equal(s.text('upcoming'),'Showing 1 of 2');
  assert.deepEqual(s.shown('past'),['council-a']);
  s.pick('all');
  assert.equal(s.text('upcoming'),'Showing 2 of 2');
  assert.equal(s.replaced.at(-1),'/meetings.html?lang=en');
  assert.equal(s.lists.upcoming.decoy.hidden,false);
});

test('hash restore: valid, duplicates, unknown, malformed, prototype names, unrelated anchors and hashchange',()=>{
  let s=setup({hash:'#body=council-b'});
  assert.equal(s.text('upcoming'),'Showing 1 of 2');assert.equal(s.nodes['meetings-filter-body'].value,'council-b');
  assert.equal(s.replaced.at(-1),'#body=council-b');
  s=setup({hash:'#body=nope&body=council-a&body=zzz'});assert.equal(s.nodes['meetings-filter-body'].value,'council-a');  // last valid wins
  for(const h of ['#body=nope','#body=%E0%A4%A','#body=constructor','#body=__proto__','#body=all']){
    s=setup({hash:h});assert.equal(s.error,null,h);
    assert.equal(s.nodes['meetings-filter-body'].value,'all',h);assert.equal(s.text('upcoming'),'Showing 2 of 2',h);
    assert.equal(s.replaced.at(-1),'/meetings.html?lang=en',h);  // normalized, query kept
  }
  for(const h of ['#past-meetings','#meeting-alpha-up','#other=1','#body','']){
    s=setup({hash:h});assert.equal(s.replaced.length,0,h);assert.equal(s.location.hash,h);
  }
  s=setup({hash:'#body=council-a'});s.hashchange('#past-meetings');
  assert.equal(s.nodes['meetings-filter-body'].value,'all');assert.equal(s.text('upcoming'),'Showing 2 of 2');
  s.hashchange('#body=council-b');assert.equal(s.text('upcoming'),'Showing 1 of 2');
  s=setup({history:false,hash:'#body=council-a'});s.pick('council-b');
  assert.equal(s.replaced.length,0);assert.equal(s.text('upcoming'),'Showing 1 of 2');
});

test('missing or incomplete nodes make the script a no-op with no partial mutation; submit is prevented',()=>{
  for(const drop of [['meetings-filter'],['meetings-filter-body'],['meetings-filter-upcoming'],['meetings-filter-upcoming-count'],['meetings-filter-upcoming-empty'],
      ['meetings-filter-past'],['meetings-filter-past-count'],['meetings-filter-past-empty']]){
    const s=setup({drop});
    assert.equal(s.error,null,drop.join());
    const rows=[...s.lists.upcoming.rows,...s.lists.past.rows];
    assert.ok(rows.every(r=>r.hidden===false),drop.join());
    assert.equal(s.nodes['meetings-filter']?.hidden??true,true,drop.join());
    assert.ok(!s.text('upcoming'),drop.join());
  }
  const s=setup();let prevented=false;
  s.nodes['meetings-filter'].handlers.submit({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);
});

test('a page with no past list still filters the upcoming one; zero in one list shows its note; label template keeps $ and quotes',()=>{
  const noPast=html.replace(/<section class="section" id="past-meetings">.*?<\/section>\n/s,'');
  assert.doesNotMatch(noPast,/meetings-filter-past/);
  const s=setup({page:noPast});assert.equal(s.error,null);
  s.pick('council-a');assert.equal(s.text('upcoming'),'Showing 1 of 2');
  const tricky=html.replace(/data-count-template="[^"]*"/,()=>'data-count-template="$& {total} \\ \' {shown} {shown}"');
  const t=setup({page:tricky});t.pick('council-a');
  assert.equal(t.text('upcoming'),"$& 2 \\ ' 1 1");
});
