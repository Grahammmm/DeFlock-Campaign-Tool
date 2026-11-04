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
function agencyLink(label='Agency page'){
  const node={dataset:label===null?{}:{label},removeAttribute(name){delete this[name];}};
  return node;
}
test('agency page link shows label and finding count from data-label and CITIES',()=>{
  const {context:c,nodes}=setup();nodes['city-agency-link']=agencyLink();
  c.CITIES.paged={...c.CITIES.cedar,agencyPage:'/agencies/paged.html',findingCount:2};
  c.setCityText('paged');const a=nodes['city-agency-link'];
  assert.equal(a.href,'/agencies/paged.html');assert.equal(a.textContent,'Agency page \u00b7 2 published findings');assert.equal(a.hidden,false);
  c.CITIES.paged.findingCount=1;c.setCityText('paged');assert.equal(a.textContent,'Agency page \u00b7 1 published finding');
  c.CITIES.paged.findingCount=0;c.setCityText('paged');assert.equal(a.textContent,'Agency page \u00b7 no published findings yet');
  for(const bad of ['3',-1,1.5,undefined]){c.CITIES.paged.findingCount=bad;c.setCityText('paged');
    assert.equal(a.textContent,'Agency page',String(bad));assert.equal(a.hidden,false,String(bad));}
});
test('agency page link hides for no page, county view and the sequence back',()=>{
  const {context:c,nodes}=setup();nodes['city-agency-link']=agencyLink();const a=nodes['city-agency-link'];
  c.CITIES.paged={...c.CITIES.cedar,agencyPage:'/agencies/paged.html',findingCount:2};
  c.CITIES.other={...c.CITIES.cedar,agencyPage:'/agencies/other.html',findingCount:1};
  c.setCityText('paged');assert.equal(a.hidden,false);
  c.setCityText('cedar');assert.equal(a.hidden,true);assert.equal('href' in a,false);assert.equal(a.textContent,'');
  c.setCityText('paged');c.setCityText(null);assert.equal(a.hidden,true);assert.equal('href' in a,false);
  c.setCityText('other');assert.equal(a.href,'/agencies/other.html');assert.equal(a.textContent,'Agency page \u00b7 1 published finding');
});
test('agency page link rejects unsafe paths and needs the markup label',()=>{
  const {context:c,nodes}=setup();nodes['city-agency-link']=agencyLink();const a=nodes['city-agency-link'];
  c.CITIES.paged={...c.CITIES.cedar,findingCount:2};
  for(const bad of ['javascript:alert(1)','https://evil.example/x','/agencies/../x.html','/agencies/X.html',7]){
    c.CITIES.paged.agencyPage=bad;c.setCityText('paged');assert.equal(a.hidden,true,String(bad));assert.equal('href' in a,false,String(bad));
  }
  const {context:d,nodes:n}=setup();n['city-agency-link']=agencyLink(null);
  d.CITIES.cedar.agencyPage='/agencies/cedar.html';d.setCityText('cedar');assert.equal(n['city-agency-link'].hidden,true);
  const {context:e}=setup(false);e.CITIES.cedar.agencyPage='/agencies/cedar.html';assert.doesNotThrow(()=>e.setCityText('cedar'));
  assert.equal(code.includes('Agency page'),false);assert.equal(code.split('fetch(').length,1);
});
