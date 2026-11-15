import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {spawnSync} from 'node:child_process';
import {isDeepStrictEqual} from 'node:util';
const result = spawnSync('python3', ['-B','-m','tests.test_site_suspected','--fixture-js'], {encoding:'utf8'});
assert.equal(result.status, 0, result.stderr);
const script = result.stdout;
const sentence = 'A suspected location is predicted by a document. It is not a confirmed camera.';
const row = {id:'pole-a',place_label:'<img onerror=attack>',latitude:35.1235,longitude:-120.1235,
  source_kind:'permit',source:'https://records.example.test/permit',as_of:'2026-11-15'};
const documentData = {schema_version:1,generated_from:'content/suspected-locations.json',notice:sentence,entries:[row]};
function harness(options={}) {
  const events=[], calls=[], popups=[], requests=[];
  const nodes={};
  function element(tag='div') { return {tag,textContent:'',hidden:false,checked:true,children:[],listeners:{},
    appendChild(child){this.children.push(child);}, addEventListener(name,fn){this.listeners[name]=fn;},
    getContext(){if(options.iconFailure) throw Error('icon'); return {fillRect(){},fillText(t){calls.push(['text',t]);},getImageData(){return {};}};}}; }
  nodes['suspected-toggle']=element('input'); nodes['suspected-unavailable']=element(); nodes['map-selection-count']=element();
  const sources={}, layers={}, images={};
  const map={on(...args){events.push(args);}, loaded(){return !!options.loaded;},
    getSource(id){return sources[id];},getLayer(id){return layers[id];},hasImage(id){return !!images[id];},
    addImage(id,data){images[id]=data;calls.push(['image',id]);},addSource(id,data){sources[id]=data;},
    addLayer(layer){if(options.layerFailure) throw Error('layer'); layers[layer.id]=layer;},
    setLayoutProperty(id,key,value){layers[id].layout[key]=value;calls.push(['layout',id,value]);},
    setFilter(id,value){calls.push(['filter',id,value]);},fitBounds(bounds){calls.push(['fit',bounds]);}};
  class Popup {constructor(){popups.push(this);} setLngLat(v){this.lng=v;return this;}setDOMContent(v){this.dom=v;return this;}
    addTo(v){this.map=v;return this;}remove(){this.removed=true;}}
  const context={console,URL,Number,Promise,CITIES:{},COUNTY_BOUNDS:[[0,0],[2,2]],
    setCityText(){},window:{maplibregl:options.noLibrary?undefined:{Popup},matchMedia(){return {matches:true};}},
    maplibregl:{Popup},document:{getElementById(id){return nodes[id]||element();},createElement:element,
      querySelector(){return null;},querySelectorAll(){return [];},addEventListener(){}},
    fetch(url){requests.push(url); if(options.fetchThrows) throw Error('fetch');
      return options.reject?Promise.reject(Error('fetch')):Promise.resolve({ok:!options.nonOK,json(){return options.badJSON?Promise.reject(Error('JSON')):Promise.resolve(options.doc??documentData);}});}};
  vm.createContext(context);vm.runInContext(script,context);
  context.map=options.noMap?null:map;
  context.cameraGeojson={type:'FeatureCollection',features:[{geometry:{coordinates:[1,1]},properties:{city:'cedar',ruralCandidate:false}}]};
  context.mapReady=true; layers['camera-points']={id:'camera-points'};
  const camera = JSON.stringify(context.cameraGeojson);
  function emit(name,...args){for(const e of events.filter(e=>e[0]===name)) e.at(-1)(...args);}
  async function init(){context.initSuspectedLayer();if(!options.loaded) emit('load');await new Promise(resolve=>setImmediate(resolve));}
  return {context,map,nodes,sources,layers,events,calls,popups,requests,camera,emit,init};
}

test('integrated app installs its own question icon/source/layer once, correct axes, text-only popup', async()=>{
  const h=harness();await h.init();h.context.initSuspectedLayer();h.emit('load');await new Promise(resolve=>setImmediate(resolve));
  assert.ok(script.includes('initMap();initSuspectedLayer();'));
  assert.equal(h.requests.length,1);assert.equal(h.requests[0],'/data/suspected-locations.json');
  assert.equal(h.layers['suspected-location-points'].type,'symbol');
  assert.equal(h.layers['suspected-location-points'].layout['icon-image'],'suspected-question');
  assert.ok(h.calls.some(c=>c[0]==='text'&&c[1]==='?'));
  assert.ok(isDeepStrictEqual(Array.from(h.sources['suspected-locations'].data.features[0].geometry.coordinates),[-120.1235,35.1235]));
  const click=h.events.find(e=>e[0]==='click'&&e[1]==='suspected-location-points').at(-1);
  click({features:[{properties:row}],lngLat:[-120,35]});
  assert.equal(h.popups[0].dom.children[0].textContent,row.place_label);
  assert.equal(h.popups[0].dom.children[1].textContent,sentence);
  assert.equal(h.context.activePopup,null);
  h.nodes['suspected-toggle'].checked=false;h.nodes['suspected-toggle'].listeners.change();
  assert.equal(h.layers['suspected-location-points'].layout.visibility,'none');assert.ok(h.popups[0].removed);
  h.nodes['suspected-toggle'].checked=true;h.nodes['suspected-toggle'].listeners.change();
  assert.equal(h.layers['suspected-location-points'].layout.visibility,'visible');
  assert.equal(h.events.filter(e=>e[0]==='click').length,1);
});

test('toggle before map load and already-loaded install are idempotent',async()=>{
  const h=harness();h.context.initSuspectedLayer();h.nodes['suspected-toggle'].checked=false;h.nodes['suspected-toggle'].listeners.change();
  h.emit('load');await new Promise(resolve=>setImmediate(resolve));assert.equal(h.layers['suspected-location-points'].layout.visibility,'none');
  const loaded=harness({loaded:true});await loaded.init();assert.equal(loaded.requests.length,1);
});

test('city/group/reset retain camera-only count/filter/fit and county-wide suspected layer',async()=>{
  const h=harness();await h.init();
  for(const slug of ['cedar','rural-area',null]) h.context.chooseCity(slug);
  assert.equal(JSON.stringify(h.context.cameraGeojson),h.camera);
  assert.equal(h.nodes['map-selection-count'].textContent,'1 mapped point');
  assert.ok(h.calls.filter(c=>c[0]==='filter').every(c=>c[1]==='camera-points'));
  assert.equal(h.layers['suspected-location-points'].layout.visibility,'visible');
});

for(const options of [{noMap:true},{noLibrary:true},{reject:true},{fetchThrows:true},{nonOK:true},{badJSON:true},
  {doc:{}},{doc:{...documentData,entries:[{}]}},{doc:{...documentData,entries:[row,row]}},{iconFailure:true},{layerFailure:true}]) {
  test('overlay failure isolated: '+JSON.stringify(options),async()=>{
    const h=harness(options);await h.init();assert.equal(h.nodes['suspected-unavailable'].hidden,false);
    assert.match(h.nodes['suspected-unavailable'].textContent,/unavailable/);
    assert.equal(JSON.stringify(h.context.cameraGeojson),h.camera);
    assert.ok(h.layers['camera-points']);assert.equal(h.context.activePopup,null);
  });
}
test('map error hides only overlay and retains list fallback',async()=>{
  const h=harness();await h.init();h.emit('error');
  assert.equal(h.layers['suspected-location-points'].layout.visibility,'none');
  assert.equal(h.nodes['suspected-unavailable'].hidden,false);
  assert.ok(h.layers['camera-points']);
});
