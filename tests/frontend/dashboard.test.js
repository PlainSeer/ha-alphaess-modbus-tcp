import {test} from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
const dom=new JSDOM('<!doctype html><html><body></body></html>');
for(const key of ['window','document','HTMLElement','customElements','CustomEvent']) globalThis[key]=dom.window[key];
const mod=await import('../../custom_components/alphaess_modbus/frontend/alphaess-dashboard-content.js');
const {discoverDevices,groupsFor,presentation,AlphaESSDashboardStrategy:Dashboard,AlphaESSViewStrategy:View}=mod;
const entity=(key,domain='sensor',entry='first',device='one',extra={})=>({entity_id:`${domain}.renamed_${entry}_${key}`,unique_id:`${entry}_${key}`,platform:'alphaess_modbus',config_entry_id:entry,device_id:device,...extra});
const devices=[{id:'one',name:'Roof'},{id:'two',name_by_user:'Garage'}];
const entities=[entity('soc_battery'),entity('peak_shaving','switch'),entity('house_load_energy_today'),entity('soc_battery','sensor','second','two')];
function hass(list=entities){const calls=[];return {calls,states:{},locale:{language:'en'},callWS:async request=>{calls.push(request.type);if(request.type==='config/entity_registry/list')return list;if(request.type==='config/device_registry/list')return devices;throw Error('Unexpected API call');},callService:()=>{throw Error('No service calls allowed');}};}
test('discover renamed entities, isolate entries and ignore unrelated/hidden/disabled entities',()=>{
 const list=[...entities,entity('power_grid','sensor','first','one',{disabled_by:'user'}),entity('power_battery','sensor','first','one',{hidden_by:'user'}),entity('pv1_power','sensor','first','one',{platform:'template'}),entity('pv2_power','sensor','first','one',{unique_id:'other_id'})];
 const result=discoverDevices(list,devices);assert.equal(result.length,2);
 const roof=result.find(x=>x.entryId==='first');assert.equal(roof.entities.size,3);assert.equal(roof.entities.get('sensor.soc_battery').entity_id,'sensor.renamed_first_soc_battery');
 assert.equal(discoverDevices(list,devices,'two')[0].entryId,'second');
 assert.equal(discoverDevices(list,devices,'missing').length,0);
});
test('dashboard generates distinct routes and performs registry reads only',async()=>{
 const h=hass();const result=await Dashboard.generate({},h);assert.equal(result.views.length,6);assert.equal(new Set(result.views.map(v=>v.path)).size,6);
 assert.deepEqual(h.calls.sort(),['config/device_registry/list','config/entity_registry/list']);
 const view=await View.generate(result.views[0].strategy,h);assert.equal(view.type,'panel');assert.ok(view.cards[0].groups.length);
});
test('ambiguous device, empty registry and registry failure are actionable',async()=>{
 assert.match(JSON.stringify(await View.generate({},hass())),/Select one/);
 assert.match(JSON.stringify(await Dashboard.generate({},hass([]))),/No enabled/);
 assert.match(JSON.stringify(await Dashboard.generate({}, {callWS:async()=>{throw Error();}})),/Unable to read/);
});
test('controls include peak and remaining controls once; omit disabled controls',()=>{
 const d=discoverDevices([...entities,entity('reset_energy','button'),entity('some_schedule','time')],devices,'one')[0];
 const groups=groupsFor(d,'controls');assert.equal(groups[0].title,'Peak Shaving');
 const ids=groups.flatMap(g=>g.items.map(i=>i.entity));assert.equal(new Set(ids).size,ids.length);assert.ok(ids.includes('button.renamed_first_reset_energy'));assert.ok(groups.some(g=>g.title==='Schedules'));
});
test('format missing states, negative battery direction and grid export',()=>{
 for(const state of [undefined,{state:null},{state:'unknown'},{state:'unavailable'}])assert.equal(presentation(state,{}).value,'Unavailable');
 assert.equal(presentation({state:'-1450',attributes:{unit_of_measurement:'W'}},{color:'battery'}).value,'Charging 1,450');
 assert.equal(presentation({state:'1450',attributes:{unit_of_measurement:'W'}},{color:'battery'}).value,'Discharging 1,450');
 assert.deepEqual(presentation({state:'0',attributes:{unit_of_measurement:'W'}},{color:'battery'}),{value:'Idle',unit:'',color:'#9e9e9e'});
 assert.equal(presentation({state:'-1.45',attributes:{unit_of_measurement:'kW'}},{color:'battery'}).value,'Charging 1.45');
 const unavailable=presentation({state:'unavailable'},{color:'battery'});
 assert.equal(unavailable.value,'Unavailable');
 const device=discoverDevices([entity('power_battery')],devices,'one')[0];
 assert.equal(mod.diagnosticGroups(device).flatMap(g=>g.items)[0].color,'battery');
 assert.equal(presentation({state:'-500',attributes:{unit_of_measurement:'W'}},{color:'grid'}).value,'Export 500');
 assert.equal(presentation({state:'off'},{domain:'switch'}).value,'Off');
});
test('rendering and repeated hass updates are passive; click only opens more-info',()=>{
 const h=hass(),card=document.createElement('alphaess-summary');document.body.append(card);
 card.setConfig({page:'controls',title:'<img src=x onerror=alert(1)>',groups:[{title:'Peak Shaving',items:[{key:'peak_shaving',entity:'switch.test',label:'Enable',domain:'switch'}]}]});
 h.states['switch.test']={state:'off',attributes:{reason:'off'}};card.hass=h;
 assert.equal(card.shadowRoot.querySelector('img'),null);assert.match(card.shadowRoot.textContent,/<img/);
 const button=card.shadowRoot.querySelector('button');let event;card.addEventListener('hass-more-info',e=>event=e);
 button.click();assert.equal(event.detail.entityId,'switch.test');assert.equal(event.composed,true);
 h.states['switch.test']={state:'on',attributes:{reason:'limiting_import'}};card.hass=h;
 assert.equal(card.shadowRoot.querySelector('button'),button);assert.match(button.textContent,/On/);assert.match(button.textContent,/limiting import/);assert.deepEqual(h.calls,[]);
});
test('incomplete energy day is visible without hover; no stale state after unavailability',()=>{
 const card=document.createElement('alphaess-summary'),h=hass();
 card.setConfig({groups:[{title:'Energy today',items:[{entity:'sensor.today',label:'House usage'}]}]});
 h.states['sensor.today']={state:'3.7',attributes:{unit_of_measurement:'kWh',incomplete_day:true}};card.hass=h;
 assert.equal(card.shadowRoot.querySelector('.status').hidden,false);assert.match(card.shadowRoot.textContent,/Incomplete day/);
 h.states={};card.hass=h;assert.equal(card.shadowRoot.querySelector('.status').hidden,true);assert.match(card.shadowRoot.textContent,/Unavailable/);
});
test('module reload does not double-register custom elements or picker entries',async()=>{
 await import('../../custom_components/alphaess_modbus/frontend/alphaess-dashboard-content.js?reload=1');
 assert.equal(window.customStrategies.filter(s=>s.type==='alphaess-modbus').length,1);
});

test('diagnostics uses shared panels and retains every enabled sensor exactly once',async()=>{
 const list=[entity('battery_voltage'),entity('battery_fault'),entity('inverter_grid_frequency'),entity('pv1_voltage'),entity('total_energy_from_pv'),entity('dispatch_mode'),entity('inverter_temperature'),entity('future_metric'),entity('battery_full_soc','binary_sensor'),entity('peak_shaving','switch'),entity('hidden','sensor','first','one',{hidden_by:'user'}),entity('disabled','sensor','first','one',{disabled_by:'integration'})];
 const view=await View.generate({page:'diagnostics',device_id:'one'},hass(list));
 assert.equal(view.type,'panel');assert.equal(view.title,'Diagnostics');
 const card=view.cards[0];assert.equal(card.type,'custom:alphaess-summary');assert.equal(card.page,'diagnostics');
 const shown=card.groups.flatMap(g=>g.items.map(i=>i.entity));
 const expected=list.filter(e=>!e.hidden_by&&!e.disabled_by&&!e.entity_id.startsWith('switch.')).map(e=>e.entity_id);
 assert.deepEqual(shown.sort(),expected.sort());assert.equal(new Set(shown).size,shown.length);
 assert.deepEqual(new Set(card.groups.map(g=>g.title)),new Set(['Battery','Faults & warnings','Grid','Solar','Energy','Dispatch & operating status','Inverter & system','Other sensors']));
});

test('diagnostic labels preserve full names and open only more-info',()=>{
 const full='Battery Maximum Cell Temperature';
 const device=discoverDevices([entity('battery_max_cell_temp','sensor','first','one',{original_name:full})],devices,'one')[0];
 const groups=groupsFor(device,'diagnostics');assert.equal(groups[0].items[0].label,'max Cell temp');
 const card=document.createElement('alphaess-summary'),h=hass();card.setConfig({page:'diagnostics',groups});card.hass=h;
 const button=card.shadowRoot.querySelector('button');assert.match(button.getAttribute('aria-label'),/Battery Maximum Cell Temperature/);
 assert.ok(card.shadowRoot.querySelector('.panels'));assert.equal(card.shadowRoot.querySelector('.controls'),null);
 assert.ok(card.shadowRoot.querySelector('.diagnostics'));assert.equal(card.shadowRoot.querySelectorAll('.column').length,2);
 assert.equal(button.querySelector('.label').textContent,full);assert.equal(button.querySelector('ha-icon'),null);
 h.states['sensor.renamed_first_battery_max_cell_temp']={state:'22',attributes:{unit_of_measurement:'°C'}};card.hass=h;
 assert.equal(button.querySelector('.value').textContent,'22°C');
 h.states={};card.hass=h;assert.equal(button.querySelector('.value').textContent,'Unavailable');
 let id;card.addEventListener('hass-more-info',e=>id=e.detail.entityId);button.click();assert.equal(id,'sensor.renamed_first_battery_max_cell_temp');assert.deepEqual(h.calls,[]);
});
