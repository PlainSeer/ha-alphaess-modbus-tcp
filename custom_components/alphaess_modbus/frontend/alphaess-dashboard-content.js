window.dispatchEvent(new CustomEvent("alphaess-dashboard-load", {detail:"module-started"}));
/** Optional managed dashboard. Registry reads only; actions open HA more-info. */
const DOMAIN = "alphaess_modbus";
const TYPE = "alphaess-modbus";
const CARD = "alphaess-summary";
const DOCS = "https://github.com/PlainSeer/ha-alphaess-modbus/blob/main/docs/managed-dashboard.md";
const spec = (key, label, icon, color = "blue", domain = "sensor") =>
  ({ key, label, icon, color, domain });

export const OVERVIEW = [
  { title: "Battery & Grid", items: [
    spec("soc_battery", "Battery SoC", "mdi:battery", "soc"),
    spec("power_battery", "Battery", "mdi:battery-medium", "battery"),
    spec("current_house_load", "House load", "mdi:home-lightning-bolt", "amber"),
    spec("power_grid", "Grid", "mdi:transmission-tower", "grid"),
    spec("battery_full_soc", "Battery full", "mdi:battery-check", "green", "binary_sensor"),
    spec("inverter_work_mode", "Inverter mode", "mdi:information"),
    spec("soh_battery", "Battery health", "mdi:battery-heart", "green"),
    spec("battery_max_cell_temp", "Battery temp", "mdi:thermometer"),
  ] },
  { title: "Energy today", note: "House load is accumulated from power; missing readings can leave gaps.", items: [
    spec("house_load_energy_today", "House load", "mdi:home-lightning-bolt", "amber"),
    spec("today_pv_generation", "Solar", "mdi:solar-power", "green"),
    spec("today_energy_from_grid", "Imported", "mdi:transmission-tower-import", "red"),
    spec("today_energy_feed_to_grid", "Exported", "mdi:transmission-tower-export", "teal"),
    spec("today_battery_charged", "Charged", "mdi:battery-plus", "green"),
    spec("today_battery_discharged", "Discharged", "mdi:battery-minus", "orange"),
  ] },
  { title: "Solar", items: [
    spec("current_pv_production", "PV production", "mdi:solar-power", "green"),
    spec("pv1_power", "String 1", "mdi:solar-panel", "green"),
    spec("pv2_power", "String 2", "mdi:solar-panel", "green"),
    spec("pv3_power", "String 3", "mdi:solar-panel", "green"),
    spec("pv4_power", "String 4", "mdi:solar-panel", "green"),
    spec("active_power_pv_meter", "AC PV meter", "mdi:meter-electric", "green"),
    spec("inverter_temperature", "Inverter temp", "mdi:thermometer", "orange"),
    spec("total_energy_from_pv", "PV total", "mdi:counter", "amber"),
  ] },
  { title: "Battery health", items: [
    spec("soh_battery", "Health", "mdi:battery-heart", "green"),
    spec("battery_status", "Status", "mdi:battery"),
    spec("battery_voltage", "Voltage", "mdi:flash"),
    spec("battery_current", "Current", "mdi:current-dc"),
    spec("battery_min_cell_temp", "Min cell temp", "mdi:thermometer"),
    spec("battery_max_cell_temp", "Max cell temp", "mdi:thermometer", "orange"),
    spec("battery_min_cell_voltage", "Min cell voltage", "mdi:flash"),
    spec("battery_max_cell_voltage", "Max cell voltage", "mdi:flash"),
  ] },
  { title: "Filtered power", items: [
    spec("current_house_load_filtered", "Filtered load", "mdi:home-lightning-bolt", "amber"),
    spec("current_pv_production_filtered", "Filtered solar", "mdi:solar-power", "green"),
    spec("excess_power", "Excess power", "mdi:solar-power", "teal"),
  ] },
  { title: "House energy estimates", note: "Daily balance can decrease. Tracking total is not lifetime consumption.", items: [
    spec("house_energy_today", "Daily balance", "mdi:home-lightning-bolt", "amber"),
    spec("house_energy_since_tracking", "Since tracking", "mdi:counter"),
  ] },
];

const mode = (key, title, powerLabel, color) => ({ title, items: [
  spec(key, "Enable", "mdi:power", color, "switch"),
  spec(`${key}_hold`, "Hold", "mdi:pause-circle", "amber", "switch"),
  spec(`${key}_countdown`, "Countdown", "mdi:timer-outline"),
  spec(`${key}_power`, powerLabel, "mdi:flash", color, "number"),
  spec(`${key}_duration`, "Duration", "mdi:timer-outline", "blue", "number"),
  spec(`${key}_cutoff_soc`, "Stop at SoC", "mdi:battery", "blue", "number"),
  ...(key === "force_import" ? [spec("force_import_pause", "Paused", "mdi:pause-circle", "amber", "binary_sensor")] : []),
] });
export const CONTROLS = [
  { title: "Peak Shaving", note: "Off by default. Tap a setting to open its controls. A gradual import target, not a hard cap.", items: [
    spec("peak_shaving", "Enable", "mdi:chart-bell-curve", "blue", "switch"),
    spec("peak_shaving_paused", "Paused", "mdi:pause-circle", "amber", "binary_sensor"),
    spec("peak_shaving_power", "Import target", "mdi:transmission-tower-import", "blue", "number"),
    spec("peak_shaving_cutoff_soc", "Below SoC", "mdi:battery", "green", "number"),
    spec("discharging_cutoff_soc", "Lower cutoff", "mdi:battery-low", "orange"),
  ] },
  mode("force_charging", "Force Charging", "Charge power", "green"),
  mode("force_discharging", "Force Discharging", "Discharge power", "orange"),
  mode("force_export", "Force Export", "Export target", "teal"),
  mode("force_import", "Force Import", "Import target", "red"),
  { title: "Excess Export", items: [
    spec("excess_export", "Enable", "mdi:solar-power", "green", "switch"),
    spec("excess_export_pause", "Paused", "mdi:pause-circle", "amber", "binary_sensor"),
    spec("inverter_ac_limit", "AC limit", "mdi:transmission-tower", "blue", "select"),
  ] },
];

/** Discover by platform, entry and unique ID, never by an assumed entity name. */
export function discoverDevices(entities, devices, selectedDevice) {
  const groups = new Map();
  const deviceMap = new Map(devices.map(d => [d.id, d]));
  for (const e of entities) {
    if (e.platform !== DOMAIN || !e.config_entry_id || !e.device_id || e.disabled_by || e.hidden_by) continue;
    if (selectedDevice && e.device_id !== selectedDevice) continue;
    const prefix = `${e.config_entry_id}_`;
    if (typeof e.unique_id !== "string" || !e.unique_id.startsWith(prefix)) continue;
    const domain = e.entity_id.split(".")[0];
    if (!groups.has(e.config_entry_id)) {
      const d = deviceMap.get(e.device_id);
      groups.set(e.config_entry_id, { entryId: e.config_entry_id, deviceId: e.device_id,
        title: d?.name_by_user || d?.name || "AlphaESS", entities: new Map() });
    }
    const group = groups.get(e.config_entry_id);
    if (group.deviceId !== e.device_id) continue;
    group.entities.set(`${domain}.${e.unique_id.slice(prefix.length)}`, e);
  }
  return [...groups.values()].sort((a,b) => a.title.localeCompare(b.title) || a.entryId.localeCompare(b.entryId));
}

/** Preserve every enabled sensor from the old list, grouped by its stable key. */
export function diagnosticGroups(device) {
  const sections = [
    ["Faults & warnings", /fault|warning/, "mdi:alert-circle-outline"],
    ["Energy", /^(total_energy_|today_|house_energy_|house_load_energy_)/, "mdi:counter"],
    ["Dispatch & operating status", /dispatch|^force_|^peak_shaving|^excess_export|^(charging|discharging)_/, "mdi:information-outline"],
    ["Battery", /battery|^bms_|^lmu_/, "mdi:battery"],
    ["Grid", /grid|^(ovp|uvp|ofp|ufp)/, "mdi:transmission-tower"],
    ["Solar", /^pv|_pv_|^active_power_pv/, "mdi:solar-power"],
    ["Inverter & system", /inverter|^system_|^ems_|^iso_|^modbus_|^ip_|^local_ip$|^subnet_mask$|^gateway$/, "mdi:chip"],
    ["Other sensors", /.*/, "mdi:information-outline"],
  ].map(([title, match, icon]) => ({title, match, icon, items:[]}));
  for (const [id, entity] of device.entities) {
    const [domain, key] = id.split(".");
    if (!["sensor", "binary_sensor"].includes(domain)) continue;
    const section = sections.find(s => s.match.test(key));
    const fullLabel = entity.name || entity.original_name || key.replaceAll("_", " ");
    const shortLabel = section.title === "Battery" ? fullLabel.replace(/^Battery /i, "")
      : section.title === "Inverter & system" ? fullLabel.replace(/^Inverter /i, "") : fullLabel;
    const label = shortLabel
      .replace(/temperature/ig, "temp").replace(/minimum/ig, "min").replace(/maximum/ig, "max");
    section.items.push({key, domain, entity:entity.entity_id, label, fullLabel,
      icon:entity.icon || section.icon, color:key === "power_battery" ? "battery" : "blue"});
  }
  return sections.filter(s => s.items.length).map(({title, items}) => ({title,
    items:items.sort((a,b) => a.label.localeCompare(b.label) || a.entity.localeCompare(b.entity))}));
}

export function groupsFor(device, page) {
  if (page === "diagnostics") return diagnosticGroups(device);
  const specs = page === "overview" ? OVERVIEW : CONTROLS;
  const used = new Set();
  const groups = specs.map(group => ({ ...group, items: group.items.flatMap(item => {
    const e = device.entities.get(`${item.domain}.${item.key}`);
    if (!e) return [];
    used.add(e.entity_id);
    return [{ ...item, entity: e.entity_id }];
  }) })).filter(g => g.items.length);
  if (page === "controls") {
    // Include all remaining enabled settings, including scheduling/dispatch.
    const remaining = [...device.entities.values()].filter(e =>
      /^(switch|number|select|time|button)\./.test(e.entity_id) && !used.has(e.entity_id));
    const byDomain = [
      ["switch", "Other switches"], ["number", "Other settings"],
      ["select", "Operating settings"], ["time", "Schedules"], ["button", "Maintenance"],
    ];
    for (const [domain, title] of byDomain) {
      const items = remaining.filter(e => e.entity_id.startsWith(domain + "."))
        .sort((a,b) => a.entity_id.localeCompare(b.entity_id))
        .map(e => ({ entity: e.entity_id, label: e.name || e.original_name || e.entity_id,
          domain, icon: domain === "button" ? "mdi:tools" : "mdi:tune", color: "blue" }));
      if (items.length) groups.push({title, items, note: domain === "button" ? "Maintenance actions can restart the inverter or reset totals. Open details before acting." : undefined});
    }
  }
  return groups;
}

const notice = text => ({type: "markdown", content: text});
async function discover(hass, config) {
  const [entities, devices] = await Promise.all([
    hass.callWS({type:"config/entity_registry/list"}),
    hass.callWS({type:"config/device_registry/list"}),
  ]);
  return discoverDevices(entities, devices, config.device_id);
}
function viewFor(device, page, config) {
  return { title: config.title || (page === "controls" ? "Controls" : page === "diagnostics" ? "Diagnostics" : "Overview"),
    path: config.path || page, type:"panel", cards:[{type:`custom:${CARD}`,
      title:device.title, page, groups:groupsFor(device,page)}] };
}
export class AlphaESSDashboardStrategy extends HTMLElement {
  static noEditor = true;
  static getCreateSuggestions() { return {title:"AlphaESS", icon:"mdi:solar-panel"}; }
  static async generate(config, hass) {
    try {
      const devices = await discover(hass, config);
      if (!devices.length) return {title:"AlphaESS", views:[{title:"Setup", cards:[notice("No enabled AlphaESS Modbus entities found. Check the integration and any device_id selection.")]}]};
      return {title:config.title || "AlphaESS", views:devices.flatMap(d =>
        ["overview","controls","diagnostics"].map(page => ({
          title: `${devices.length > 1 ? d.title + " · " : ""}${page[0].toUpperCase()+page.slice(1)}`,
          path:`${d.entryId}-${page}`, icon:page === "overview" ? "mdi:solar-panel" : page === "controls" ? "mdi:tune" : "mdi:information-outline",
          strategy:{type:`custom:${TYPE}`, device_id:d.deviceId, entry_id:d.entryId, page},
        }))) };
    } catch (_) {
      return {title:"AlphaESS", views:[{title:"Unavailable", cards:[notice("Unable to read the Home Assistant registries. Check your connection and permissions, then reload this dashboard.")]}]};
    }
  }
}
export class AlphaESSViewStrategy extends HTMLElement {
  static noEditor = true;
  static async generate(config, hass) {
    try {
      const devices = await discover(hass, config);
      const device = config.entry_id ? devices.find(d => d.entryId === config.entry_id) : devices.length === 1 ? devices[0] : null;
      if (!device) return {cards:[notice("Select one AlphaESS device using device_id in the view strategy. The selected device must have enabled integration entities.")]};
      return viewFor(device, ["overview","controls","diagnostics"].includes(config.page) ? config.page : "overview", config);
    } catch (_) { return {cards:[notice("Unable to load AlphaESS entities. Check your connection and permissions, then reload.")]}; }
  }
}

const COLORS = {green:"#4caf50",amber:"#ffc107",orange:"#ff9800",red:"#f44336",teal:"#009688",blue:"#2196f3",grey:"#9e9e9e"};
export function presentation(state, item, locale = "en") {
  if (!state || typeof state.state !== "string" || ["unknown","unavailable",""].includes(state.state)) return {value:"Unavailable", color:COLORS.grey, unit:""};
  const n = Number(state.state);
  let color = item.color || "blue";
  if (color === "grid") color = n > 0 ? "red" : n < 0 ? "teal" : "grey";
  if (color === "battery") color = n > 0 ? "orange" : n < 0 ? "green" : "grey";
  if (color === "soc") color = n >= 85 ? "green" : n >= 40 ? "amber" : n >= 20 ? "orange" : "red";
  if (state.state === "off") color = "grey";
  let value = state.state;
  let unit = state.attributes?.unit_of_measurement || "";
  if (item.domain === "button") { value = "Open"; unit = ""; }
  else if (Number.isFinite(n) && state.state.trim() !== "") {
    // Presentation only: retain signed sensor states for history and controls.
    if (item.color === "grid") value = `${n < 0 ? "Export" : "Import"} ${new Intl.NumberFormat(locale,{maximumFractionDigits:0}).format(Math.abs(n))}`;
    else if (item.color === "battery") value = n === 0 ? "Idle" : `${n < 0 ? "Charging" : "Discharging"} ${new Intl.NumberFormat(locale,{maximumFractionDigits:unit === "W" ? 0 : 2}).format(Math.abs(n))}`;
    else value = new Intl.NumberFormat(locale,{maximumFractionDigits:unit === "W" || unit === "%" ? 0 : 2}).format(n);
    if (item.color === "battery" && n === 0) unit = "";
  } else if (value === "on" || value === "off") value = value === "on" ? "On" : "Off";
  return {value, unit, color:COLORS[color] || COLORS.blue};
}

const CSS = `
:host{display:block;color:var(--primary-text-color);font-family:var(--paper-font-body1_-_font-family, sans-serif)}
.wrap{width:95%;margin:16px auto 32px}.intro{padding:4px 2px 16px}h1{font-size:22px;margin:0 0 6px}p{font-size:12px;color:var(--secondary-text-color);line-height:1.5;margin:6px 0}
.panels{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start}.column{display:grid;gap:12px;min-width:0}
ha-card{display:block;background:color-mix(in srgb,var(--primary-text-color) 3%,var(--card-background-color));border:1px solid var(--divider-color);border-radius:14px;box-shadow:none;overflow:hidden}
h2{font-size:17px;font-weight:700;margin:0;padding:14px 18px 10px;border-bottom:1px solid var(--divider-color)}
.note{padding:0 14px}.items{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:2px;padding:4px 8px 8px}
button{font:inherit;color:inherit;background:none;border:0;border-radius:10px;min-width:0;padding:8px 2px 6px;cursor:pointer;text-align:center}button:hover{background:var(--secondary-background-color)}button:focus-visible{outline:2px solid var(--primary-color);outline-offset:-2px}
ha-icon{display:block;--mdc-icon-size:26px;width:26px;height:28px;margin:auto}.value{font-size:14px;font-weight:700;line-height:1.3;margin-top:4px;overflow-wrap:anywhere}.unit{font-size:10.5px;font-weight:600;color:var(--secondary-text-color);margin-left:2px}.label{font-size:12px;color:var(--secondary-text-color);line-height:1.3;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.detail{font-size:11px;color:var(--secondary-text-color);padding:0 12px 8px}
.controls .items{display:block;padding:4px 18px}.controls button{display:grid;grid-template-columns:26px minmax(0,1fr) auto;align-items:center;gap:12px;width:100%;border-radius:0;border-bottom:1px solid var(--divider-color);padding:13px 0;text-align:left}.controls button:last-child{border-bottom:0}.controls .label{grid-column:2;grid-row:1;font-size:14px;color:var(--primary-text-color)}.controls .value{grid-column:3;grid-row:1;margin:0;font-weight:500}.controls .status{grid-column:2 / 4}.status{font-size:11px;color:var(--secondary-text-color);margin-top:4px}
.diagnostics .items{display:block;padding:0 18px}.diagnostics button{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,auto);align-items:center;column-gap:20px;width:100%;min-height:48px;padding:11px 0;border-radius:0;border-bottom:1px solid var(--divider-color);text-align:left}.diagnostics button:last-child{border-bottom:0}.diagnostics .label{grid-column:1;grid-row:1;font-size:14px;color:var(--primary-text-color);white-space:normal;overflow:visible;overflow-wrap:anywhere;margin:0}.diagnostics .value{grid-column:2;grid-row:1;margin:0;text-align:right;font-weight:500;font-variant-numeric:tabular-nums}.diagnostics .unit{font-size:12px;font-weight:400;margin-left:4px}.diagnostics .status{grid-column:1 / -1;grid-row:2}
@media(max-width:650px){.wrap{width:95%;margin:10px auto 24px}.panels{grid-template-columns:1fr;gap:10px}}
@media(max-width:540px){.items{grid-template-columns:repeat(2,minmax(0,1fr))}}
`;
const el = (tag, text, cls) => {const e=document.createElement(tag); if(text!==undefined)e.textContent=text; if(cls)e.className=cls; return e;};
export class AlphaESSSummary extends HTMLElement {
  constructor() {super();this.attachShadow({mode:"open"});this._rows=[];}
  setConfig(config) {
    this._config=config;this._rows=[];this.shadowRoot.replaceChildren();
    this.shadowRoot.append(el("style",CSS));
    const wrap=el("div",undefined,config.page === "controls" ? "wrap controls" : config.page === "diagnostics" ? "wrap diagnostics" : "wrap"), intro=el("header",undefined,"intro");
    intro.append(el("h1",config.title || "AlphaESS"));
    intro.append(el("p",config.page === "controls" ? "Tap a tile to open Home Assistant controls. Opening this page does not change inverter settings." : config.page === "diagnostics" ? "Enabled sensors grouped by function. Tap a value for its full name, history and details." : "Live power, battery and energy. Tap a value for history and details."));
    const panels=el("div",undefined,"panels");
    const columns=[el("div",undefined,"column"),el("div",undefined,"column")];
    panels.append(...columns);wrap.append(intro,panels);this.shadowRoot.append(wrap);
    let groupIndex=0;
    for (const group of config.groups || []) {
      const panel=el("ha-card"), grid=el("div",undefined,"items");panel.append(el("h2",group.title));
      if(group.note)panel.append(el("p",group.note,"note"));
      for(const item of group.items) {
        const button=el("button"), icon=el("ha-icon"), value=el("span"), unit=el("span",undefined,"unit"), line=el("div",undefined,"value");
        button.type="button"; icon.setAttribute("icon",item.icon || "mdi:information-outline");
        const status=el("div",undefined,"status");status.hidden=true;
        line.append(value,unit);
        if(config.page === "diagnostics") button.append(el("div",item.fullLabel || item.label,"label"),line,status);
        else button.append(icon,line,el("div",item.label,"label"),status);
        button.addEventListener("click",()=>this.dispatchEvent(new CustomEvent("hass-more-info",{detail:{entityId:item.entity},bubbles:true,composed:true})));
        grid.append(button);this._rows.push({item,button,icon,value,unit,status});
      }
      panel.append(grid);
      // Overview keeps power/energy on the left and solar/estimates on the right.
      const column=config.page === "overview" ? (["Battery & Grid","Energy today","Battery health"].includes(group.title) ? 0 : 1) : groupIndex++ % 2;
      columns[column].append(panel);
    }
    this._renderStates();
  }
  set hass(hass) {this._hass=hass;this._renderStates();}
  _renderStates() {
    if(!this._hass)return;
    for(const row of this._rows) {
      const state=this._hass.states[row.item.entity];
      const p=presentation(state,row.item,this._hass.locale?.language || "en");
      row.value.textContent=p.value;row.unit.textContent=p.unit;row.icon.style.color=p.color;
      const incomplete=state?.attributes?.incomplete_day ? " · Incomplete day" : "";
      const reason=row.item.key === "peak_shaving" && state?.attributes?.reason ? ` · ${String(state.attributes.reason).replaceAll("_"," ")}` : "";
      row.button.title=`${row.item.fullLabel || row.item.label}: ${p.value} ${p.unit}${incomplete}${reason}`;
      row.button.setAttribute("aria-label",row.button.title);
      row.status.textContent=(incomplete+reason).replace(/^ · /, "");
      row.status.hidden=!row.status.textContent;
    }
  }
  getCardSize() {return 6;}
}
for(const [name,definition] of [[`ll-strategy-dashboard-${TYPE}`,AlphaESSDashboardStrategy],[`ll-strategy-view-${TYPE}`,AlphaESSViewStrategy],[CARD,AlphaESSSummary]]) {
  if(!customElements.get(name))customElements.define(name,definition);
}
window.customStrategies=window.customStrategies || [];
if(!window.customStrategies.some(s=>s.type===TYPE && s.strategyType==="dashboard"))window.customStrategies.push({type:TYPE,strategyType:"dashboard",name:"AlphaESS",description:"Battery, solar and inverter controls. Discovers your AlphaESS devices automatically.",documentationURL:DOCS});

window.dispatchEvent(new CustomEvent("alphaess-dashboard-load", {detail:"module-registered"}));
