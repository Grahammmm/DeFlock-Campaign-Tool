import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';

// DF1207-15: build the fictional example, then run the generated search.js in a mocked DOM whose fetch serves
// synthetic data/findings.json and data/sources.json. No browser, no network.
const root=fs.mkdtempSync(path.join(os.tmpdir(),'synthetic-search-'));
fs.cpSync('examples/fictional-campaign',root,{recursive:true,filter:p=>!p.includes(`${path.sep}public`)});
const built=spawnSync('python3',['-B','-m','campaign_tool','build','--directory',root],{encoding:'utf8'});
if(built.status!==0)throw Error(built.stderr);
const pub=name=>fs.readFileSync(path.join(root,'public',name),'utf8');
const code=pub('search.js'),html=pub('search.html');
const attr=html.match(/data-agencies="([^"]*)"/)[1].replace(/&quot;/g,'"').replace(/&amp;/g,'&');

class El{
  constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.hidden=false;this.handlers={};this._text='';this.value='';}
  get textContent(){return this._text;}
  set textContent(v){this._text=String(v);this.children=[];}
  set innerHTML(v){throw Error('innerHTML must never be used');}
  setAttribute(k,v){this.attrs[k]=String(v);}
  getAttribute(k){return k in this.attrs?this.attrs[k]:null;}
  appendChild(c){this.children.push(c);return c;}
  addEventListener(e,f){this.handlers[e]=f;}
}
const finding=(n,title,extra={})=>({id:`fnd_${n}`,slug:`f${n}`,title,summary:'',agency_id:null,sources:[],url:`/findings/f${n}.html`,...extra});
const source=(n,title,extra={})=>({sha256_prefix:`abc${n}`,title,agency_id:null,agency_name:null,url:undefined,...extra});
const FINDINGS=[
  finding(1,'ALPR contract renewal approved',{summary:'The council renewed the Flock deal.',agency_id:'ca-cedar-police',sources:[{title:'Staff report 14'}]}),
  finding(2,'ALPR audit log request'),
  finding(3,'Policy posted online',{summary:'A José Martínez memo mentions retention.'}),
];
const SOURCES=[
  source(1,'Vendor contract with Flock',{agency_id:'ca-cedar-police',agency_name:'Cedar Police Department (fictional)'}),
  source(2,'ALPR contract amendment',{agency_id:'ca-cedar-county-sheriff',agency_name:'Cedar County Sheriff'}),
];

function setup({hash='',findings=FINDINGS,sources=SOURCES,fetchImpl,labels,data=attr}={}){
  const nodes={'search-form':new El('form'),q:new El('input'),'search-results':new El('ol'),'search-count':new El('p'),
    'search-fallback':new El('p'),'search-message':new El('p')};
  nodes['search-form'].hidden=true;nodes['search-form'].attrs['data-agencies']=data;
  const winHandlers={},replaced=[],requested=[];
  const files={'/data/findings.json':{schema_version:1,findings},'/data/sources.json':{schema_version:1,sources}};
  const context={document:{getElementById:id=>nodes[id]||null,createElement:t=>new El(t)},
    location:{hash,pathname:'/search.html',search:''},history:{replaceState:(s,t,url)=>replaced.push(url)},
    encodeURIComponent,decodeURIComponent,Promise,Array,JSON,String,
    fetch:fetchImpl||(url=>{requested.push(url);return Promise.resolve({ok:true,json:()=>Promise.resolve(files[url])});}),
    addEventListener:(e,f)=>{winHandlers[e]=f;}};
  if(labels)context.LABELS=labels;
  context.window=context;
  vm.createContext(context);
  vm.runInContext(code,context,{filename:'search.js'});
  const flush=()=>new Promise(r=>setTimeout(r,5));
  const results=()=>nodes['search-results'].children.map(li=>({title:li.children[0].textContent,href:li.children[0].attrs.href,tag:li.children[1].textContent.trim(),a:li.children[0].tag}));
  const type=v=>{nodes.q.value=v;nodes.q.handlers.input();};
  const goto=h=>{context.location.hash=h;winHandlers.hashchange();};
  return {context,nodes,replaced,requested,flush,results,type,goto,message:()=>nodes['search-message'].textContent,
    count:()=>nodes['search-count'].textContent};
}

test('#q=alpr%20contract: every term must match, results are linked, findings first, count line right',async()=>{
  const s=setup({hash:'#q=alpr%20contract'});
  assert.equal(s.nodes['search-form'].hidden,true);   // not revealed until the data is in
  assert.equal(s.nodes['search-fallback'].hidden,false);
  await s.flush();
  assert.deepEqual(s.requested,['/data/findings.json','/data/sources.json']);
  assert.equal(s.nodes['search-form'].hidden,false);
  assert.equal(s.nodes['search-fallback'].hidden,true);
  assert.equal(s.nodes.q.value,'alpr contract');
  assert.deepEqual(s.results(),[
    {title:'ALPR contract renewal approved',href:'/findings/f1.html',tag:'Finding',a:'a'},
    {title:'ALPR contract amendment',href:'/sources.html',tag:'Source',a:'a'}]);
  assert.equal(s.count(),'2 results');
  assert.equal(s.nodes['search-count'].hidden,false);
  assert.equal(s.nodes['search-message'].hidden,true);
});

test('matches summary, agency name from the page map, cited source titles; a source links its agency section',async()=>{
  const s=setup({hash:'#q=flock%20deal'});await s.flush();
  assert.deepEqual(s.results().map(r=>r.title),['ALPR contract renewal approved']);
  s.goto('#q=staff%20report');assert.deepEqual(s.results().map(r=>r.title),['ALPR contract renewal approved']);
  const police=JSON.parse(attr)['ca-cedar-police'];
  s.goto('#q='+encodeURIComponent(police[0].toLowerCase()));
  assert.deepEqual(s.results().map(r=>r.title),['ALPR contract renewal approved','Vendor contract with Flock']);
  assert.equal(s.results()[1].href,'/sources.html'+(police[1]?'#'+police[1]:''));
  assert.ok(police[1],'the example agency has a source section anchor');
  s.goto('#q=sheriff');assert.deepEqual(s.results().map(r=>r.title),['ALPR contract amendment']);  // agency_name on a source
  s.goto('#q=zzzz');assert.equal(s.results().length,0);assert.equal(s.message(),'No published finding or source matches.');
  assert.equal(s.count(),'');
});

test('a title holding markup renders as literal text; the script never builds HTML',async()=>{
  const evil='<img src=x onerror=alert(1)>';
  const s=setup({hash:'#q=onerror',findings:[finding(9,evil)],sources:[source(9,'<b>bold</b> onerror')]});
  await s.flush();
  assert.deepEqual(s.results().map(r=>r.title),[evil,'<b>bold</b> onerror']);
  for(const li of s.nodes['search-results'].children)assert.deepEqual(li.children.map(c=>c.tag),['a','span']);
  assert.equal(s.nodes['search-message'].textContent,'');
  for(const word of ['innerHTML','outerHTML','insertAdjacentHTML','eval','document.write'])assert.ok(!code.includes(word),word);
  assert.doesNotMatch(code,/https?:\/\/[A-Za-z0-9]/);
});

test('a link that is not a path or an http(s) URL is dropped, the title still shows',async()=>{
  const s=setup({hash:'#q=bad',findings:[finding(1,'bad one',{url:'javascript:alert(1)'}),finding(2,'bad two',{url:'//evil.example/x'}),
    finding(3,'bad three',{url:'https://cedar-records.example.invalid/findings/f3.html'})],sources:[]});
  await s.flush();
  assert.deepEqual(s.results().map(r=>[r.title,r.href,r.a]),[['bad one',undefined,'span'],['bad two',undefined,'span'],
    ['bad three','https://cedar-records.example.invalid/findings/f3.html','a']]);
});

test('one character and 101 characters show the hint and no results list; 2 and 100 search',async()=>{
  const s=setup({hash:'#q=a'});await s.flush();
  assert.equal(s.message(),'Type 2 to 100 characters to search.');
  assert.equal(s.nodes['search-results'].children.length,0);assert.equal(s.count(),'');
  s.goto('#q='+'a'.repeat(101));
  assert.equal(s.message(),'Type 2 to 100 characters to search.');assert.equal(s.nodes['search-results'].children.length,0);
  s.goto('#q=%20%20a%20%20');assert.equal(s.message(),'Type 2 to 100 characters to search.');  // trimmed first
  s.goto('#q=al');assert.ok(s.results().length>0);assert.equal(s.message(),'');
  s.goto('#q='+'a'.repeat(100));assert.equal(s.nodes['search-results'].children.length,0);assert.equal(s.message(),'No published finding or source matches.');
  s.type('a');assert.equal(s.message(),'Type 2 to 100 characters to search.');assert.equal(s.nodes['search-results'].children.length,0);
  s.goto('');assert.equal(s.message(),'Type 2 to 100 characters to search.');  // no query: the hint, no list
});

test('bad percent-encoding in the hash is ignored without an exception',async()=>{
  const s=setup({hash:'#q=%E0%A4'});await s.flush();
  assert.equal(s.nodes.q.value,'');assert.equal(s.nodes['search-results'].children.length,0);
  assert.equal(s.message(),'Type 2 to 100 characters to search.');
  assert.doesNotThrow(()=>s.goto('#q=%E0%A4'));
  assert.doesNotThrow(()=>s.goto('#content'));assert.equal(s.nodes['search-results'].children.length,0);
  s.goto('#x=1&q=alpr');assert.equal(s.results().length,3);   // other keys are skipped, q is found
});

test('José matches jose and the reverse; case does not matter',async()=>{
  const s=setup({hash:'#q=jose'});await s.flush();
  assert.deepEqual(s.results().map(r=>r.title),['Policy posted online']);
  s.goto('#q=JOS%C3%89');assert.deepEqual(s.results().map(r=>r.title),['Policy posted online']);
  s.goto('#q=jose%CC%81');assert.deepEqual(s.results().map(r=>r.title),['Policy posted online']);   // decomposed é
  s.goto('#q=MARTINEZ');assert.deepEqual(s.results().map(r=>r.title),['Policy posted online']);
  s.goto('#q=POLICY%20POSTED');assert.deepEqual(s.results().map(r=>r.title),['Policy posted online']);
});

test('fetch rejects, a bad status, or JSON without the arrays: fallback stays visible, one message, no exception',async()=>{
  const bad={
    rejects:()=>Promise.reject(new Error('offline')),
    status:()=>Promise.resolve({ok:false,json:()=>Promise.resolve({})}),
    empty:()=>Promise.resolve({ok:true,json:()=>Promise.resolve({})}),
    notJson:()=>Promise.resolve({ok:true,json:()=>Promise.reject(new SyntaxError('bad'))}),
    nullDoc:()=>Promise.resolve({ok:true,json:()=>Promise.resolve(null)}),
    throws:()=>{throw new Error('no fetch');}};
  for(const [name,impl] of Object.entries(bad)){
    const s=setup({hash:'#q=alpr',fetchImpl:impl});await s.flush();
    assert.equal(s.nodes['search-fallback'].hidden,false,name);
    assert.equal(s.nodes['search-form'].hidden,true,name);
    assert.equal(s.message(),'Search is unavailable.',name);
    assert.equal(s.nodes['search-results'].children.length,0,name);
    assert.doesNotThrow(()=>s.goto('#q=alpr'),name);
  }
  // only one of the two files bad is still unavailable
  const half=setup({hash:'#q=alpr',fetchImpl:url=>url==='/data/sources.json'?Promise.resolve({ok:true,json:()=>Promise.resolve({})}):
    Promise.resolve({ok:true,json:()=>Promise.resolve({findings:FINDINGS})})});
  await half.flush();assert.equal(half.message(),'Search is unavailable.');assert.equal(half.nodes['search-fallback'].hidden,false);
});

test('zero published findings: sources are still searched; zero of both finds nothing',async()=>{
  const s=setup({hash:'#q=contract',findings:[]});await s.flush();
  assert.deepEqual(s.results().map(r=>r.tag),['Source','Source']);
  assert.equal(s.nodes['search-form'].hidden,false);
  const none=setup({hash:'#q=contract',findings:[],sources:[]});await none.flush();
  assert.equal(none.message(),'No published finding or source matches.');assert.equal(none.nodes['search-fallback'].hidden,true);
});

test('malformed records are skipped, not fatal; at most 50 results with findings first',async()=>{
  const many=Array.from({length:40},(_,n)=>finding(100+n,`needle finding ${n}`));
  const more=Array.from({length:40},(_,n)=>source(200+n,`needle source ${n}`));
  const s=setup({hash:'#q=needle',findings:[null,5,...many],sources:[null,'x',...more]});await s.flush();
  const rows=s.results();
  assert.equal(rows.length,50);assert.equal(s.count(),'50 results');
  assert.deepEqual([...new Set(rows.map(r=>r.tag))],['Finding','Source']);
  assert.equal(rows.filter(r=>r.tag==='Finding').length,40);
  const one=setup({hash:'#q=audit'});await one.flush();assert.equal(one.count(),'1 result');
});

test('typing updates the hash with replaceState (no navigation), clears it when empty, and hashchange re-reads',async()=>{
  const s=setup({hash:''});await s.flush();
  s.type('alpr contract');
  assert.equal(s.replaced.at(-1),'#q=alpr%20contract');assert.equal(s.results().length,2);
  s.type('');assert.equal(s.replaced.at(-1),'/search.html');
  s.type('é&=#');assert.equal(s.replaced.at(-1),'#q='+encodeURIComponent('é&=#'));
  s.nodes['search-form'].handlers.submit({preventDefault(){s.prevented=true;}});assert.equal(s.prevented,true);
  const writes=s.replaced.length;
  s.goto('#q=audit');assert.equal(s.nodes.q.value,'audit');assert.equal(s.results().length,1);
  assert.equal(s.replaced.length,writes);   // reading the hash never rewrites it
});

test('labels: a relabelled site reads LABELS; keys it lacks keep the English default',async()=>{
  const s=setup({hash:'#q=a',labels:{'js.search_hint':'Escriba de 2 a 100 caracteres.','js.search_count_many':'{count} resultados','js.search_finding':'Hallazgo'}});
  await s.flush();
  assert.equal(s.message(),'Escriba de 2 a 100 caracteres.');
  s.goto('#q=alpr');assert.equal(s.count(),'3 resultados');assert.equal(s.results()[0].tag,'Hallazgo');
  assert.equal(s.results().at(-1).tag,'Source');
  const odd=setup({hash:'#q=alpr',labels:{'js.search_count_many':'${count} $& {count}'}});await odd.flush();
  assert.equal(odd.count(),'$3 $& 3');
});

test('without the form (any other page) the script does nothing; the script only fetches the two published files',()=>{
  const fetched=[];
  const ctx={document:{getElementById:()=>null,createElement:()=>{throw Error('no');}},fetch:u=>{fetched.push(u);},window:{}};
  ctx.window=ctx;vm.createContext(ctx);vm.runInContext(code,ctx);
  assert.deepEqual(fetched,[]);
  assert.deepEqual([...code.matchAll(/load\("([^"]*)"/g)].map(m=>m[1]),['/data/findings.json','/data/sources.json']);
  assert.doesNotMatch(code,/XMLHttpRequest|sendBeacon|WebSocket|localStorage|sessionStorage|document\.cookie/);
});
