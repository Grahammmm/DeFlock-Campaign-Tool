import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
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
