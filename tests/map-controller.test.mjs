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
function setup(js=code){
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
  vm.createContext(context);vm.runInContext(js,context);
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

test('authored context renders on a clicked point and escapes source-controlled text',async()=>{
  const hook='function(p){return "<br>Source review: "+mapText(p.reviewLabel||"Not checked");}';
  const rendered=spawnSync('python3',['campaign_tool/map_controller.py'],{input:JSON.stringify({config,profile_renderer:'function setCityText(){}',point_context_renderer:hook}),encoding:'utf8'});
  assert.equal(rendered.status,0,rendered.stderr);
  const {context:c}=setup(JSON.parse(rendered.stdout).javascript);
  const events={};let html='';
  class MapStub {
    on(name,...args){events[name]=args.at(-1);}
    addControl(){} addSource(){} addLayer(){} getLayer(){return true;}
    setFilter(){} fitBounds(){} resize(){}
  }
  class PopupStub {on(){return this;}remove(){return this;}setLngLat(){return this;}setHTML(value){html=value;return this;}addTo(){return this;}}
  c.window.maplibregl=c.maplibregl={Map:MapStub,Popup:PopupStub,NavigationControl:class{},AttributionControl:class{}};
  c.ResizeObserver=class{observe(){}};
  c.document.querySelector=()=>null;
  c.fetch=async()=>({json:async()=>({features:[]})});
  c.initMap();events.load();await new Promise(resolve=>setImmediate(resolve));
  events.click({lngLat:[1,1],features:[{properties:{reviewLabel:'<img src=x onerror=bad>',url:'https://www.openstreetmap.org/node/1'}}]});
  assert.match(html,/Source review: &lt;img src=x onerror=bad&gt;/);
  assert.ok(html.includes('View source record'));assert.equal(html.includes('<img'),false);
});


// Offline fixtures exercise only the documented Popup on/remove lifecycle.
const lifecycleRender=spawnSync('python3',['-B','campaign_tool/map_controller.py'],{
  input:JSON.stringify({
    config,
    profile_renderer:'function setCityText(slug){window.profileSelection=slug;}',
    point_context_renderer:'function(p){return "<br>Source review: "+mapText(p.reviewLabel||"Not checked")+"<br>Reviewed: "+mapText(p.reviewDate||"Unavailable");}'
  }),
  encoding:'utf8'
});
if(lifecycleRender.status!==0)throw Error(lifecycleRender.stderr);
const lifecycleCode=JSON.parse(lifecycleRender.stdout).javascript;

async function popupHarness(){
  const {context:c}=setup(lifecycleCode);
  const events={},popups=[],filters=[];
  class MapStub {
    on(name,...args){events[name]=args.at(-1);}
    addControl(){} addSource(){} addLayer(){} getLayer(){return true;}
    setFilter(layer,filter){filters.push({layer,filter});}
    fitBounds(){} resize(){}
  }
  class PopupStub {
    constructor(){this.handlers={};this.removeCalls=0;this.attached=false;popups.push(this);}
    on(name,handler){this.handlers[name]=handler;return this;}
    setLngLat(value){this.lngLat=value;return this;}
    setHTML(value){this.html=value;return this;}
    addTo(map){this.map=map;this.attached=true;return this;}
    remove(){this.removeCalls++;this.attached=false;this.handlers.close?.();return this;}
  }
  c.window.maplibregl=c.maplibregl={
    Map:MapStub,Popup:PopupStub,NavigationControl:class{},AttributionControl:class{}
  };
  c.ResizeObserver=class{observe(){}};
  c.document.querySelector=()=>null;
  c.fetch=async()=>({json:async()=>({features:[]})});
  c.CITIES.harbor={name:'Harbor',bounds:[[11,11],[20,20]]};
  c.initMap();events.load();await new Promise(resolve=>setImmediate(resolve));
  function click(overrides={}){
    events.click({lngLat:[1,1],features:[{properties:{
      manufacturer:'Fictional Maker',operator:'Fictional Operator',
      url:'https://www.openstreetmap.org/node/1',
      reviewLabel:'Synthetic source review',reviewDate:'2030-01-02',...overrides
    }}]});
    return popups.at(-1);
  }
  return {c,popups,filters,click};
}

test('city change removes the selected popup through its public API',async()=>{
  const {c,click,filters}=await popupHarness();
  c.chooseCity('cedar');
  const popup=click();
  c.chooseCity('harbor');
  assert.equal(popup.removeCalls,1);
  assert.equal(popup.attached,false);
  assert.equal(c.activePopup,null);
  assert.equal(c.selectedCity,'harbor');
  assert.equal(c.window.profileSelection,'harbor');
  assert.equal(JSON.stringify(filters.at(-1).filter),'["==",["get","city"],"harbor"]');
});

test('county reset removes the selected popup without changing reset behavior',async()=>{
  const {c,click,filters}=await popupHarness();
  c.chooseCity('cedar');
  const popup=click();
  c.chooseCity(null);
  assert.equal(popup.removeCalls,1);
  assert.equal(c.activePopup,null);
  assert.equal(c.selectedCity,null);
  assert.equal(c.window.profileSelection,null);
  assert.equal(filters.at(-1).filter,null);
});

test('opening a second popup removes the first and retains the replacement',async()=>{
  const {c,click}=await popupHarness();
  const first=click(),second=click({reviewDate:'2030-01-03'});
  assert.equal(first.removeCalls,1);
  assert.equal(first.attached,false);
  assert.equal(second.removeCalls,0);
  assert.equal(second.attached,true);
  assert.equal(c.activePopup,second);
});

test('manual public close clears the handle and avoids duplicate removal',async()=>{
  const {c,click}=await popupHarness();
  const popup=click();
  popup.remove();
  assert.equal(c.activePopup,null);
  c.chooseCity('harbor');
  assert.equal(popup.removeCalls,1);
  const replacement=click();
  assert.equal(c.activePopup,replacement);
});

test('an old popup close notification cannot clear the current popup',async()=>{
  const {c,click}=await popupHarness();
  const first=click(),second=click();
  first.handlers.close();
  assert.equal(c.activePopup,second);
  c.chooseCity('harbor');
  assert.equal(second.removeCalls,1);
  assert.equal(c.activePopup,null);
});

test('popup replacement preserves safe source and individual review-date markup',async()=>{
  const {click}=await popupHarness();
  const first=click({manufacturer:'<img src=x onerror=bad>',reviewLabel:'<b>synthetic</b>'});
  assert.ok(first.html.includes('&lt;img src=x onerror=bad&gt;'));
  assert.ok(first.html.includes('Source review: &lt;b&gt;synthetic&lt;/b&gt;'));
  assert.ok(first.html.includes('Reviewed: 2030-01-02'));
  assert.ok(first.html.includes('href="https://www.openstreetmap.org/node/1"'));
  assert.ok(first.html.includes('rel="noopener noreferrer"'));
  assert.equal(first.html.includes('<img'),false);
  const second=click({url:'javascript:alert(1)',reviewDate:'2030-01-03'});
  assert.ok(second.html.includes('View source record'));
  assert.ok(second.html.includes('Reviewed: 2030-01-03'));
  assert.equal(second.html.includes('javascript:'),false);
  assert.equal(second.html.includes('2030-01-02'),false);
});
