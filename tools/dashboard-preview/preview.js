// Local documentation fixture. Never connects to Home Assistant or an inverter.
import {OVERVIEW,CONTROLS,discoverDevices,groupsFor} from '../../custom_components/alphaess_modbus/frontend/alphaess-dashboard-content.js';
// HA supplies ha-icon in production; use the same public MDI font in this fixture.
class PreviewIcon extends HTMLElement {
 connectedCallback(){const span=document.createElement('span');span.className=this.getAttribute('icon').replace('mdi:','mdi mdi-');span.style.fontSize='26px';span.setAttribute('aria-hidden','true');this.replaceChildren(span);}
}
customElements.define('ha-icon',PreviewIcon);
const values={soc_battery:[68,'%'],power_battery:[-1400,'W'],current_house_load:[1800,'W'],current_pv_production:[3200,'W'],power_grid:[0,'W'],battery_full_soc:['off',''],inverter_work_mode:['Normal',''],soh_battery:[100,'%'],battery_max_cell_temp:[25,'°C'],house_load_energy_today:[8.42,'kWh'],today_pv_generation:[12.8,'kWh'],today_energy_from_grid:[3.1,'kWh'],today_energy_feed_to_grid:[2.4,'kWh'],today_battery_charged:[6.3,'kWh'],today_battery_discharged:[1.2,'kWh'],pv1_power:[1700,'W'],pv2_power:[1500,'W'],inverter_temperature:[36,'°C'],battery_voltage:[53.2,'V'],battery_current:[26.3,'A'],battery_min_cell_temp:[23,'°C'],current_house_load_filtered:[1760,'W'],current_pv_production_filtered:[3180,'W'],excess_power:[1420,'W'],house_energy_today:[8.37,'kWh'],house_energy_since_tracking:[124.6,'kWh']};
const entities=[],states={},seen=new Set();
for(const group of [...OVERVIEW,...CONTROLS])for(const item of group.items){
 const id=`${item.domain}.example_${item.key}`;if(seen.has(id))continue;
 let v=values[item.key];
 if(!v&&item.domain==='switch')v=['off',''];
 if(!v&&item.domain==='binary_sensor')v=['off',''];
 if(!v&&item.domain==='number')v=[item.key.includes('duration')?60:item.key.includes('cutoff')?100:3,item.key.includes('duration')?'min':item.key.includes('cutoff')?'%':'kW'];
 if(['force_discharging_cutoff_soc','force_export_cutoff_soc'].includes(item.key))v=[20,'%'];
 if(item.key==='peak_shaving_power')v=[5,'kW'];
 if(item.key==='discharging_cutoff_soc')v=[10,'%'];
 if(item.key==='inverter_ac_limit')v=['5 kW',''];
 if(!v)continue;
 seen.add(id);entities.push({entity_id:id,unique_id:`example_${item.key}`,config_entry_id:'example',device_id:'example-device',platform:'alphaess_modbus'});
 states[id]={state:String(v[0]),attributes:{unit_of_measurement:v[1]}};
}
// Include realistic long names and diagnostic-only sensors in the row preview.
for(const [key,name,value,unit] of [
 ['battery_fault','Battery Fault',0,''],['inverter_warning_2','Inverter Warning 2',131072,''],
 ['charging_period_1_start_minute','Charging Period 1 Start Minute',30,'min'],
 ['total_energy_from_pv','Total Energy from PV',8.58,'MWh'],
]) {
 const id=`sensor.example_${key}`;
 entities.push({entity_id:id,unique_id:`example_${key}`,config_entry_id:'example',device_id:'example-device',platform:'alphaess_modbus',original_name:name});
 states[id]={state:String(value),attributes:{unit_of_measurement:unit}};
}
for(const entity of entities)entity.original_name ||= entity.unique_id.replace(/^example_/,'').replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase());
entities.find(e=>e.unique_id==='example_battery_max_cell_temp').original_name='Battery Maximum Cell Temperature';
entities.find(e=>e.unique_id==='example_house_load_energy_today').original_name='House Load Energy Today Accumulated';
states['sensor.example_house_load_energy_today'].attributes.incomplete_day=true;
const device=discoverDevices(entities,[{id:'example-device',name:'AlphaESS inverter'}])[0];
const card=document.querySelector('alphaess-summary');
const fontStyle='/node_modules/@mdi/font/css/materialdesignicons.min.css';
const headFont=document.createElement('link');headFont.rel='stylesheet';headFont.href=fontStyle;document.head.append(headFont);
function show(page){card.setConfig({title:'AlphaESS inverter',page,groups:groupsFor(device,page)});card.hass={states,locale:{language:'en'}};const link=document.createElement('link');link.rel='stylesheet';link.href=fontStyle;card.shadowRoot.append(link);for(const b of document.querySelectorAll('nav button'))b.setAttribute('aria-selected',String(b.id===page));}
for(const b of document.querySelectorAll('nav button'))b.onclick=()=>show(b.id);
show('overview');
window.preview={show,card,states};
