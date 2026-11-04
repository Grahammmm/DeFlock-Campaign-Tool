import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';

// DF1422-14: build the fictional example, then run the generated findings/cite.js in a mocked DOM taken from the
// built finding page (button attributes and citation text). No browser, no network, fake timers.
const root=fs.mkdtempSync(path.join(os.tmpdir(),'synthetic-cite-'));
fs.cpSync('examples/fictional-campaign',root,{recursive:true,filter:p=>!p.includes(`${path.sep}public`)});
const built=spawnSync('python3',['-B','-m','campaign_tool','build','--directory',root],{encoding:'utf8'});
if(built.status!==0)throw Error(built.stderr);
const pub=name=>fs.readFileSync(path.join(root,'public',name),'utf8');
const page=pub('findings/cedar-policy-posted.html');
const unescape=s=>s.replace(/&quot;/g,'"').replace(/&#x27;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
const citation=unescape(page.match(/<p class="cite-text">([^<]*)<\/p>/)[1]);
const button=page.match(/<button type="button" class="cite-copy"( hidden)? data-copied="([^"]*)" data-fallback="([^"]*)">([^<]*)<\/button>/);
const source=pub('findings/cite.js');

function setup({clipboard}={}){
  const timers=[],selected=[],written=[];
  const p={textContent:citation,innerHTML:undefined};
  const attrs={'data-copied':button[2],'data-fallback':button[3]};
  const btn={hidden:button[1]!==undefined,textContent:button[4],handlers:{},getAttribute:k=>attrs[k],
    addEventListener(e,f){this.handlers[e]=f;},parentNode:{querySelector:s=>s==='.cite-text'?p:null}};
  const range={selectNodeContents(n){this.node=n;}};
  const selection={removeAllRanges(){selected.length=0;},addRange(r){selected.push(r.node);}};
  const context={document:{querySelectorAll:s=>s==='.cite-copy'?[btn]:[],createRange:()=>range},
    setTimeout:(f,ms)=>timers.push({f,ms}),getSelection:()=>selection};
  context.window=context;
  if(clipboard==='ok')context.navigator={clipboard:{writeText:t=>{written.push(t);return Promise.resolve();}}};
  else if(clipboard==='reject')context.navigator={clipboard:{writeText:t=>{written.push(t);return Promise.reject(new Error('denied'));}}};
  else if(clipboard==='throw')context.navigator={clipboard:{writeText:()=>{throw new Error('blocked');}}};
  else context.navigator={};
  vm.createContext(context);
  vm.runInContext(source,context,{filename:'cite.js'});
  return {btn,p,timers,selected,written};
}
const tick=()=>new Promise(r=>setImmediate(r));

test('page ships the button hidden with its labels and cite.js is first-party and never writes html',()=>{
  assert.ok(button,'button markup');
  assert.equal(button[1],' hidden');
  assert.deepEqual([button[2],button[3],button[4]],['Copied','Select and copy','Copy']);
  assert.match(page,/<script defer src="\/findings\/cite\.js"><\/script>/);
  assert.match(citation,/^Cedar County Records Project\. "Cedar Police .*" \(finding fnd_0000000000000001\)\. Published 2026-02-01, corrected 2026-02-03\. https:\/\/cedar-records\.example\.invalid\/findings\/cedar-policy-posted\.html\.$/);
  assert.doesNotMatch(source,/innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval|https?:|import|require\(/);
  assert.doesNotMatch(source,/\.(innerHTML|outerHTML)\s*=/);
  assert.doesNotMatch(pub('index.html'),/cite\.js/);
});

test('loading unhides the button and clicking copies the paragraph textContent, then shows Copied and restores the label after 2 s',async()=>{
  const {btn,timers,written,selected}=setup({clipboard:'ok'});
  assert.equal(btn.hidden,false);
  assert.equal(timers.length,0);
  btn.handlers.click();
  await tick();
  assert.deepEqual(written,[citation]);
  assert.equal(btn.textContent,'Copied');
  assert.equal(timers.length,1);
  assert.equal(timers[0].ms,2000);
  assert.equal(selected.length,0);
  timers[0].f();
  assert.equal(btn.textContent,'Copy');
});

test('a missing clipboard selects the paragraph text and shows Select and copy',()=>{
  const {btn,p,written,selected,timers}=setup({});
  btn.handlers.click();
  assert.equal(btn.textContent,'Select and copy');
  assert.deepEqual(selected,[p]);
  assert.equal(written.length,0);
  timers[0].f();
  assert.equal(btn.textContent,'Copy');
});

test('a rejected clipboard write falls back to the selection',async()=>{
  const {btn,p,selected,written}=setup({clipboard:'reject'});
  btn.handlers.click();
  await tick();
  assert.deepEqual(written,[citation]);
  assert.equal(btn.textContent,'Select and copy');
  assert.deepEqual(selected,[p]);
});

test('a clipboard that throws synchronously falls back to the selection and never shows Copied',()=>{
  const {btn,p,selected}=setup({clipboard:'throw'});
  btn.handlers.click();
  assert.equal(btn.textContent,'Select and copy');
  assert.deepEqual(selected,[p]);
});

test('the script never assigns innerHTML on the nodes it touches',async()=>{
  for(const clipboard of ['ok','reject','throw',undefined]){
    const {btn,p}=setup({clipboard});
    btn.handlers.click();
    await tick();
    assert.equal(p.innerHTML,undefined);
    assert.equal(Object.prototype.hasOwnProperty.call(btn,'innerHTML'),false);
    assert.equal(p.textContent,citation);
  }
});

test('a page without the button changes nothing',()=>{
  const context={document:{querySelectorAll:()=>[]},window:{},navigator:{}};
  vm.createContext(context);
  assert.doesNotThrow(()=>vm.runInContext(source,context,{filename:'cite.js'}));
});
