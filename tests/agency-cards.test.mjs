import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
const config=JSON.parse(fs.readFileSync('examples/fictional-campaign/agency-cards.json','utf8'));
const result=spawnSync('python3',['campaign_tool/agency_cards.py'],{input:JSON.stringify(config),encoding:'utf8'});
if(result.status!==0)throw Error(result.stderr);
const code=JSON.parse(result.stdout).javascript;
function setup(optional=true){
  const nodes=Object.fromEntries(['city-name','city-vendor','city-status','city-summary','city-camera-count','city-open','city-facts','city-flags',...(optional?['city-method-link','city-records-link']:[])].map(id=>[id,{}]));
  const profile={name:'Cedar agency',displayName:'Cedar fictional department',vendor:'Example vendor',status:'Synthetic',summary:'No real claims',cameraCount:'3 synthetic points',facts:['First synthetic note','Second synthetic note'],open:'Review before publication',flags:['Unreviewed example']};
  const context={document:{getElementById:id=>nodes[id]||null},CITIES:{cedar:profile,'rural-area':profile}};
  vm.createContext(context);vm.runInContext(code,context);
  return {context,nodes};
}
test('county profile populates facts, flags and default source links',()=>{
  const {context:c,nodes}=setup();c.setCityText(null);
  assert.equal(nodes['city-name'].textContent,config.county_profile.name);
  assert.equal(nodes['city-facts'].innerHTML,'<li>Synthetic example; no reviewed agency findings.</li>');
  assert.equal(nodes['city-flags'].innerHTML,'<span>Fictional example</span>');
  assert.equal(nodes['city-records-link'].href,'sources.html#library');
});
test('agency selection uses display name and source fragment',()=>{
  const {context:c,nodes}=setup();c.setCityText('cedar');
  assert.equal(nodes['city-name'].textContent,'Cedar fictional department');
  assert.equal(nodes['city-records-link'].href,'sources.html#cedar');
  assert.equal(nodes['city-records-link'].textContent,config.source_link_label);
  assert.equal(nodes['city-facts'].innerHTML,'<li>First synthetic note</li><li>Second synthetic note</li>');
});
test('configured alias sends both evidence links to matching notes',()=>{
  const {context:c,nodes}=setup();c.setCityText('rural-area');
  assert.equal(nodes['city-records-link'].href,'sources.html#cedar-region');
  assert.equal(nodes['city-method-link'].href,'sources.html#cedar-region');
});
test('absent camera counts are hidden and present counts shown',()=>{
  const {context:c,nodes}=setup();c.setCityText(null);assert.equal(nodes['city-camera-count'].hidden,true);
  c.setCityText('cedar');assert.equal(nodes['city-camera-count'].hidden,false);
});
test('missing optional source-link elements do not break the profile',()=>{
  const {context:c,nodes}=setup(false);assert.doesNotThrow(()=>c.setCityText('cedar'));
  assert.equal(nodes['city-summary'].textContent,'No real claims');
});
test('Flock transparency link shows only for a payload URL on the transparency host',()=>{
  const {context:c,nodes}=setup();nodes['city-flock-link']={};
  c.CITIES.example={...c.CITIES.cedar,flockTransparencyUrl:'https://transparency.flocksafety.com/example-pd'};
  c.setCityText('example');
  assert.equal(nodes['city-flock-link'].href,'https://transparency.flocksafety.com/example-pd');
  assert.equal(nodes['city-flock-link'].hidden,false);
  assert.equal(nodes['city-records-link'].href,'sources.html#example');
  c.setCityText('cedar');
  assert.equal(nodes['city-flock-link'].hidden,true);
  assert.equal(nodes['city-flock-link'].href,'');
  assert.equal(nodes['city-records-link'].href,'sources.html#cedar');
  assert.equal(nodes['city-method-link'].href,'sources.html#cedar');
  for(const bad of ['javascript:alert(1)','https://evil.example/x','https://transparency.flocksafety.com/a/../b',7]){
    c.CITIES.example.flockTransparencyUrl=bad;c.setCityText('example');
    assert.equal(nodes['city-flock-link'].hidden,true,String(bad));
  }
  const {context:d}=setup(false);d.CITIES.cedar.flockTransparencyUrl='https://transparency.flocksafety.com/example-pd';
  assert.doesNotThrow(()=>d.setCityText('cedar'));
});

// DF0137-14: a relabelled site without a map (the app.js picker path) shows organizer js.* labels, never the English.
test('no-map relabelled site: county and city fixed facts come from js.* labels through site-data.js',()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'relabel-cards-'));
  fs.cpSync('examples/fictional-campaign',root,{recursive:true,filter:p=>!p.includes(`${path.sep}public`)&&!p.includes(`${path.sep}content${path.sep}map`)});
  const sitePath=path.join(root,'content','site.json'),site=JSON.parse(fs.readFileSync(sitePath,'utf8'));
  site.county_profile.facts=[];fs.writeFileSync(sitePath,JSON.stringify(site));
  const labels={'js.county_findings':'{count} hallazgo(s) publicado(s) con fuentes.','js.city_no_findings':'Sin hallazgos publicados.'};
  fs.writeFileSync(path.join(root,'content','labels.json'),JSON.stringify({schema_version:1,labels}));
  const built=spawnSync('python3',['-B','-m','campaign_tool','build','--directory',root],{encoding:'utf8'});
  assert.equal(built.status,0,built.stderr);
  const pub=name=>fs.readFileSync(path.join(root,'public',name),'utf8');
  assert.match(pub('app.js'),/Agency picker without a map/);
  const scripts=['site-data.js','agency-cards.js','app.js'].map(pub).join('\n');
  for(const english of ['published finding(s) with hashed sources','No published findings for this agency yet'])
    assert.equal(scripts.includes(english),false,english);
  const nodes={};const node=id=>nodes[id]||(nodes[id]={classList:{toggle(){}},setAttribute(){}});
  const context={document:{getElementById:node,querySelectorAll:()=>[],addEventListener(){}}};
  vm.createContext(context);vm.runInContext(scripts,context);
  context.chooseCity(null);
  assert.match(nodes['city-facts'].innerHTML,/^<li>\d+ hallazgo\(s\) publicado\(s\) con fuentes\.<\/li>$/);
  const bare=Object.keys(context.CITIES).filter(slug=>context.CITIES[slug].facts.join()==='Sin hallazgos publicados.');
  assert.ok(bare.length>0);
  context.chooseCity(bare[0]);
  assert.equal(nodes['city-facts'].innerHTML,'<li>Sin hallazgos publicados.</li>');
});
