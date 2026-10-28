import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';

// Build the fictional example into a temporary directory and run the three
// generated scripts together in a mocked DOM: no browser, no network, no MapLibre.
const root=fs.mkdtempSync(path.join(os.tmpdir(),'synthetic-site-'));
fs.cpSync('examples/fictional-campaign',root,{recursive:true,filter:p=>!p.includes(`${path.sep}public`)});
const built=spawnSync('python3',['-B','-m','campaign_tool','build','--directory',root],{encoding:'utf8'});
if(built.status!==0)throw Error(built.stderr);
const pub=name=>fs.readFileSync(path.join(root,'public',name),'utf8');

function setup(){
  const ids=['city-name','city-vendor','city-status','city-summary','city-camera-count','city-open','city-facts','city-flags','city-records-link','map-selection-count','county-map'];
  const nodes=Object.fromEntries(ids.map(id=>[id,{textContent:'',innerHTML:'',classList:{toggle(){}},setAttribute(){}}]));
  const html=pub('index.html');
  const buttons=[...html.matchAll(/<button type="button" class="city-choice[^"]*"(?: id="([^"]+)")? data-city="([^"]*)"/g)].map(m=>({
    id:m[1]||'',attrs:{'data-city':m[2]},handlers:{},classes:{},
    getAttribute(k){return this.attrs[k];},addEventListener(e,f){this.handlers[e]=f;},
    classList:{toggle:(c,v)=>{}},setAttribute(k,v){this.attrs[k]=v;}
  }));
  const listeners={};let fallback={innerHTML:'present'};
  const context={URL,console,window:{matchMedia:()=>({matches:true})},document:{
    addEventListener(e,f){listeners[e]=f;},
    querySelectorAll(s){return s==='.city-choice'?buttons:[];},
    querySelector(s){return s==='.map-fallback'?fallback:s==='.city-picker'?{offsetHeight:40}:null;},
    getElementById(id){return nodes[id]||null;}
  }};
  vm.createContext(context);
  for(const name of ['site-data.js','agency-cards.js','app.js'])vm.runInContext(pub(name),context,{filename:name});
  listeners.DOMContentLoaded();
  return {context,nodes,buttons,fallback};
}

test('page bootstrap selects the county profile and degrades without MapLibre',()=>{
  const {nodes,fallback}=setup();
  assert.equal(nodes['city-name'].textContent,'Cedar County (fictional)');
  assert.equal(nodes['map-selection-count'].textContent,'3 mapped points');
  assert.equal(nodes['city-records-link'].href,'sources.html#library');
  assert.match(fallback.innerHTML,/could not load/);
});

test('city buttons route to the agency profile and its source fragment',()=>{
  const {nodes,buttons}=setup();
  buttons.find(b=>b.attrs['data-city']==='cedar').handlers.click();
  assert.equal(nodes['city-name'].textContent,'Cedar Police Department (fictional)');
  assert.equal(nodes['city-records-link'].href,'sources.html#cedar-police-records');
  assert.equal(nodes['city-facts'].innerHTML,'<li>Cedar Police produced a written ALPR usage policy (fictional)</li>');
  assert.match(pub('sources.html'),/id="cedar-police-records"/);
});

test('group slug uses the unconfirmed-ownership label',()=>{
  const {nodes,buttons}=setup();
  buttons.find(b=>b.attrs['data-city']==='rural-area').handlers.click();
  assert.equal(nodes['map-selection-count'].textContent,'3 possible rural-area points; ownership unconfirmed');
  assert.equal(nodes['city-status'].textContent,'Ownership unconfirmed');
});

test('site data globals are plain JSON values',()=>{
  const context={};vm.createContext(context);vm.runInContext(pub('site-data.js'),context);
  assert.equal(JSON.stringify(context.COUNTY_BOUNDS),'[[0,0],[2,2]]');
  assert.equal(context.CAMERA_DATA_URL,'/data/cameras.geojson');
  assert.equal(Object.keys(context.CITIES).sort().join(','),'cedar,rural-area');
});
