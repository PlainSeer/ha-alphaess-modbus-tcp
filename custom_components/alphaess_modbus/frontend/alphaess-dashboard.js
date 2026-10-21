/** Diagnostic beta loader. No service calls, storage writes or telemetry. */
const VERSION = "1.19.3b3";
const TYPE = "alphaess-modbus";
const moduleUrl = new URL("./alphaess-dashboard-content.js", import.meta.url);
moduleUrl.search = new URL(import.meta.url).search;

// Export the loader factory so failures can be tested without a real HA server.
export function createLoader(importModule, timeoutMs = 8000) {
  const started = performance.now();
  const events = ["0 ms: loader started"];
  let modulePromise;
  const record = stage => {
    if (events.length < 20) events.push(`${Math.round(performance.now() - started)} ms: ${stage}`);
  };
  const onStage = event => {
    if (["module-started", "module-registered"].includes(event.detail)) record(event.detail);
  };
  function load() {
    if (!modulePromise) {
      window.addEventListener("alphaess-dashboard-load", onStage);
      record("module import requested");
      modulePromise = Promise.resolve().then(importModule).then(module => {
        record("module import completed");
        return module;
      }, error => {
        record("module import failed");
        throw error;
      }).finally(() => window.removeEventListener("alphaess-dashboard-load", onStage));
    }
    let timer;
    return Promise.race([
      modulePromise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("Module import still pending after 8 seconds")), timeoutMs);
      }),
    ]).finally(() => clearTimeout(timer));
  }
  // Use plain escaped text. Avoid exposing server addresses, queries or stacks.
  const clean = text => String(text).replace(/https?:\/\/[^\s)]+/g, "[URL]")
    .replace(/[<>`]/g, "").slice(0, 400);
  function report(error) {
    return {type:"markdown", content:[
      "## AlphaESS loading diagnostics",
      `Diagnostic beta: ${VERSION}`,
      error ? "The loader ran, but the dashboard did not finish loading." : "The loader and dashboard module ran successfully.",
      error ? `Error: ${clean(error.name || "Error")}: ${clean(error.message || error)}` : "",
      ...events,
      "Dashboard strategy: " + (customElements.get(`ll-strategy-dashboard-${TYPE}`) ? "registered" : "missing"),
      "View strategy: " + (customElements.get(`ll-strategy-view-${TYPE}`) ? "registered" : "missing"),
      "Dashboard card: " + (customElements.get("alphaess-summary") ? "registered" : "missing"),
      "Share a screenshot of this report. It contains no entity values. Reload the frontend to start a fresh attempt.",
    ].filter(Boolean).join("\n\n")};
  }
  async function generate(kind, config, hass) {
    try {
      const module = await load();
      const Strategy = kind === "dashboard" ? module.AlphaESSDashboardStrategy : module.AlphaESSViewStrategy;
      const result = await Strategy.generate(config, hass);
      if (kind === "dashboard") return {...result, views:[...result.views,
        {title:"Loader diagnostics", path:"alphaess-loader-diagnostics", icon:"mdi:bug-outline", cards:[report()]}]};
      return result;
    } catch (error) {
      const view = {title:"Loading diagnostics", cards:[report(error)]};
      return kind === "dashboard" ? {title:"AlphaESS", views:[view]} : view;
    }
  }
  return {generate};
}

const loader = createLoader(() => import(moduleUrl.href));
class DashboardLoader extends HTMLElement {
  static noEditor = true;
  static getCreateSuggestions() { return {title:"AlphaESS", icon:"mdi:solar-panel"}; }
  static generate(config, hass) { return loader.generate("dashboard", config, hass); }
}
class ViewLoader extends HTMLElement {
  static noEditor = true;
  static generate(config, hass) { return loader.generate("view", config, hass); }
}
for (const [name, definition] of [
  [`ll-strategy-dashboard-${TYPE}`, DashboardLoader],
  [`ll-strategy-view-${TYPE}`, ViewLoader],
]) {
  if (!customElements.get(name)) customElements.define(name, definition);
}
window.customStrategies = window.customStrategies || [];
if (!window.customStrategies.some(s => s.type === TYPE && s.strategyType === "dashboard")) {
  window.customStrategies.push({type:TYPE, strategyType:"dashboard", name:"AlphaESS",
    description:"Battery, solar and inverter controls. Diagnostic beta loader.",
    documentationURL:"https://github.com/PlainSeer/ha-alphaess-modbus/blob/main/docs/managed-dashboard.md"});
}
