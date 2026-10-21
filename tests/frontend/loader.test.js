import {test} from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
const dom = new JSDOM('<!doctype html><html><body></body></html>');
for (const key of ['window','document','HTMLElement','customElements','CustomEvent']) globalThis[key] = dom.window[key];
const {createLoader} = await import('../../custom_components/alphaess_modbus/frontend/alphaess-dashboard.js');
const Dashboard = customElements.get('ll-strategy-dashboard-alphaess-modbus');
const View = customElements.get('ll-strategy-view-alphaess-modbus');

test('strategies register before the dashboard module is imported', () => {
  assert.equal(typeof Dashboard.generate, 'function');
  assert.equal(typeof View.generate, 'function');
  assert.equal(customElements.get('alphaess-summary'), undefined);
});

test('module rejection shows a built-in diagnostic card with redacted errors', async () => {
  let calls = 0;
  const loader = createLoader(() => {calls++; throw new TypeError('Failed https://example.org/file?token=secret <script>`');});
  const result = await loader.generate('dashboard', {}, {});
  const card = result.views[0].cards[0];
  assert.equal(card.type, 'markdown');
  assert.match(card.content, /TypeError: Failed \[URL\]/);
  assert.match(card.content, /module import failed/);
  assert.doesNotMatch(card.content, /example.org|token|secret|<script>/);
  await loader.generate('view', {}, {});
  assert.equal(calls, 1); // Failed imports are not retried in a loop.
});

test('pending import gives diagnostics, and a later navigation can use its result', async () => {
  let finish;
  const loader = createLoader(() => new Promise(resolve => {finish = resolve;}), 5);
  const failed = await loader.generate('view', {}, {});
  assert.match(failed.cards[0].content, /still pending/);
  assert.match(failed.cards[0].content, /module import requested/);
  assert.doesNotMatch(failed.cards[0].content, /module import completed/);
  finish({AlphaESSViewStrategy:{generate:async () => ({cards:[{type:'markdown',content:'Loaded'}]})}});
  const result = await loader.generate('view', {}, {});
  assert.equal(result.cards[0].content, 'Loaded');
});

test('delegate preserves config and hass, shares import and handles generation errors', async () => {
  const config = {device_id:'selected',page:'controls'}, hass = {callService:()=>{throw Error('must not call services');}};
  let calls = 0;
  const module = {AlphaESSDashboardStrategy:{generate:async (c,h) => {
    assert.equal(c,config); assert.equal(h,hass); return {title:'Original',views:[{title:'Original',path:'original',cards:[]}]};
  }},AlphaESSViewStrategy:{generate:async () => {throw Error('generation failed');}}};
  const loader = createLoader(() => {calls++;return module;});
  const [a,b] = await Promise.all([loader.generate('dashboard',config,hass),loader.generate('dashboard',config,hass)]);
  assert.equal(calls,1); assert.equal(a.title,'Original'); assert.equal(a.views[0].path,'original');
  assert.equal(a.views.at(-1).title,'Loader diagnostics'); assert.deepEqual(a,b);
  assert.match((await loader.generate('view',config,hass)).cards[0].content,/generation failed/);
});

test('actual module loads through wrapper, renders views and reports execution stages', async () => {
  const calls = [];
  const hass = {callWS:async request => {
    calls.push(request.type);
    if(request.type === 'config/entity_registry/list') return [{entity_id:'sensor.test_soc',unique_id:'entry_soc_battery',platform:'alphaess_modbus',config_entry_id:'entry',device_id:'device'}];
    if(request.type === 'config/device_registry/list') return [{id:'device',name:'AlphaESS'}];
    throw Error('Unexpected call');
  },callService:()=>{throw Error('No service calls allowed');}};
  const result = await Dashboard.generate({},hass);
  assert.equal(result.views.length,4);
  assert.match(result.views.at(-1).cards[0].content,/module-started/);
  assert.match(result.views.at(-1).cards[0].content,/module-registered/);
  assert.match(result.views.at(-1).cards[0].content,/Dashboard card: registered/);
  const view = await View.generate(result.views[0].strategy,hass);
  assert.equal(view.cards[0].type,'custom:alphaess-summary');
  assert.equal(customElements.get('ll-strategy-dashboard-alphaess-modbus'),Dashboard);
  assert.ok(calls.every(type => ['config/entity_registry/list','config/device_registry/list'].includes(type)));
});

test('duplicate loader URLs do not replace strategies or duplicate picker entries', async () => {
  await import('../../custom_components/alphaess_modbus/frontend/alphaess-dashboard.js?duplicate');
  assert.equal(customElements.get('ll-strategy-dashboard-alphaess-modbus'),Dashboard);
  assert.equal(window.customStrategies.filter(x => x.type === 'alphaess-modbus').length,1);
});
