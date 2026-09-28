import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import {spawnSync} from 'node:child_process';
const config=JSON.parse(fs.readFileSync('examples/fictional-campaign/map-config.json','utf8'));
const run=spawnSync('python3',['campaign_tool/map_controller.py'],{input:JSON.stringify({config,profile_renderer:'  function setCityText(slug) { window.profileSelection=slug; }\n'}),encoding:'utf8'});
if(run.status!==0)throw Error(run.stderr);
const code=JSON.parse(run.stdout).javascript;
const square=[[0,0],[10,0],[10,10],[0,10],[0,0]];
const hole=[[3,3],[7,3],[7,7],[3,7],[3,3]];
function setup(){
  const nodes={count:{textContent:''},fallback:{innerHTML:''}};
  const context={
    URL,console,CAMERA_DATA_URL:'fictional-cameras.geojson',BOUNDARY_DATA_URL:'fictional-boundary.geojson',
    COUNTY_BOUNDS:[[0,0],[10,10]],MAP_STYLE:'https://example.invalid/style.json',
    CITIES:{cedar:{name:'Cedar',bounds:[[0,0],[10,10]]}},
    window:{matchMedia:()=>({matches:true})},
    document:{
      querySelector(selector){return selector==='.map-fallback'?nodes.fallback:selector==='.city-picker'?{offsetHeight:44}:null;},
      querySelectorAll(){return [];},
      getElementById(id){return id==='map-selection-count'?nodes.count:{};}
    }
  };
  vm.createContext(context);vm.runInContext(code,context);
  return {context,nodes};
}
test('ring membership distinguishes inside from outside',()=>{
  const {context:c}=setup();assert.equal(c.inRing([1,1],square),true);assert.equal(c.inRing([11,1],square),false);
});
test('polygon holes are not classified as inside',()=>{
  const {context:c}=setup();assert.equal(c.inPolygon([1,1],[square,hole]),true);assert.equal(c.inPolygon([5,5],[square,hole]),false);
});
test('MultiPolygon membership is recognized',()=>{
  const {context:c}=setup();c.cityBoundaries={features:[{geometry:{type:'MultiPolygon',coordinates:[[square]]},properties:{NAME:'Cedar'}}]};
  assert.equal(c.incorporatedPoint([1,1]),true);assert.equal(c.cityForPoint([1,1]),'cedar');
});
test('configured fallback bounds work without changing other points',()=>{
  const {context:c}=setup();c.cityBoundaries={features:[]};
  assert.equal(c.cityForPoint([1,1]),'cedar');assert.equal(c.cityForPoint([20,20]),'county');
});
test('candidate flag is distinct from city membership',()=>{
  const {context:c}=setup();const p={properties:{city:'cedar',ruralCandidate:true}};
  assert.equal(c.selectedPoint(p,'rural-area'),true);assert.equal(c.selectedPoint(p,'cedar'),true);assert.equal(c.selectedPoint(p,'other'),false);
});
test('popup text is escaped and source links reject unsafe origins',()=>{
  const {context:c}=setup();assert.equal(c.mapText('<img "x">&'), '&lt;img &quot;x&quot;&gt;&amp;');
  assert.equal(c.mapSource('javascript:alert(1)'),config.source_map_url);
  assert.equal(c.mapSource('https://www.openstreetmap.org.evil.invalid/node/1'),config.source_map_url);
  assert.equal(c.mapSource('https://www.openstreetmap.org/node/1'),'https://www.openstreetmap.org/node/1');
});
test('reduced motion produces zero-duration fitting and correct filters',()=>{
  const {context:c}=setup();let fitted,filter;c.mapReady=true;
  c.map={getLayer:()=>true,fitBounds:(bounds,opts)=>{fitted=opts;},setFilter:(id,value)=>{filter=value;}};
  c.fitCity('rural-area');assert.equal(fitted.duration,0);assert.equal(fitted.padding.bottom,86);
  assert.deepEqual(JSON.parse(JSON.stringify(filter)),['==',['get','ruralCandidate'],true]);
});
test('selection labels candidates as uncertain and resets to all points',()=>{
  const {context:c,nodes}=setup();c.cameraGeojson={features:[{properties:{city:'cedar',ruralCandidate:true}},{properties:{city:'cedar',ruralCandidate:false}}]};
  c.chooseCity('rural-area');assert.equal(nodes.count.textContent,'1'+config.candidate_count_suffix);
  c.chooseCity(null);assert.equal(nodes.count.textContent,'2 mapped points');
});
test('missing map library leaves a source-map fallback',()=>{
  const {context:c,nodes}=setup();c.initMap();
  assert.match(nodes.fallback.innerHTML,/could not load/);assert.ok(nodes.fallback.innerHTML.includes(config.source_map_url));
});
