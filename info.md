# AlphaESS Modbus TCP — PlainSeer fork

![AlphaESS icon](https://raw.githubusercontent.com/PlainSeer/ha-alphaess-modbus/main/custom_components/alphaess_modbus/brand/icon.png)

Local Home Assistant integration for AlphaESS inverters via Modbus TCP, without a cloud dependency.

## USE AT YOUR OWN RISK

Unofficial community-maintained software, supplied without warranty. The integration includes controls that write to your inverter. Incorrect settings can cause unexpected operation, financial loss or equipment damage. Keep a restorable backup. Passing tests does not certify hardware safety.

## About this fork

Continues Senalse's v1.14.0 integration, with passive sensors and optional controls inspired by Axel Koegler's Hillview package. Original authors retain full credit. This is not an official AlphaESS integration or full Hillview parity.

## Sensor overview

- **Current House Load Filtered / Current PV Production Filtered:** smooth the existing power calculations. These displayed values do not drive inverter controls.
- **Excess Power:** filtered PV minus filtered house load, clamped at zero; an estimate, not an export command.
- **Battery Full:** on when existing raw battery status is `1` (charging not permitted, discharging permitted), matching Hillview. Invalid, missing or unrecognised status makes it unavailable.
- **Today's House Energy:** a daily balance of import, export, PV and battery energy, including AC PV once. This estimate can decrease during the day.
- **House Energy Since Tracking:** accumulates from zero when first enabled; not lifetime consumption. Downtime can leave gaps.
- **House Load Energy Today (Accumulated):** integrates existing house-load power into kWh. Only increases within each day, resets at HA-local midnight and restores same-day totals after restart. Missing readings and downtime leave gaps.

These passive sensors use data already read by the integration, without adding register reads or writes or changing polling. They do not automatically replace existing helpers or automations. See the energy calculation notes below before using the estimates.

## Optional Peak Shaving

**Off by default and after restart.** Uses existing dispatch controls to reduce grid import towards a target when explicitly enabled. Simulation-tested, not yet tested on physical inverter hardware; it cannot guarantee a hard import cap or prevent transient export. See the [feature guide and limitations](https://github.com/PlainSeer/ha-alphaess-modbus/blob/main/docs/peak-shaving.md).

Hillview's Force Import pause/resume and Cutoff SoC Hold monitoring changes have not been adopted, and optional PV energy registers are not included.

## New default dashboard · beta v1.19.3b5

**Beta v1.19.3b5:** the dark dashboard is now the integration's default dashboard design. It includes live readings, an animated power-flow diagram, an eight-hour solar graph, compact control rows and grouped diagnostics. Power Flow sits above Energy Today and Battery Health; diagnostics stay on their own page. Panels stack on phones.

Screenshots show example data.

![Dark AlphaESS Overview — example data](https://raw.githubusercontent.com/PlainSeer/ha-alphaess-modbus/v1.19.3b5/docs/images/dashboard-overview.png)

<details>
<summary>Controls, Diagnostics and mobile</summary>

![AlphaESS Controls — example data, modes off](https://raw.githubusercontent.com/PlainSeer/ha-alphaess-modbus/v1.19.3b5/docs/images/dashboard-controls.png)

![AlphaESS Diagnostics — example data](https://raw.githubusercontent.com/PlainSeer/ha-alphaess-modbus/v1.19.3b5/docs/images/dashboard-diagnostics.png)

<img src="https://raw.githubusercontent.com/PlainSeer/ha-alphaess-modbus/v1.19.3b5/docs/images/dashboard-mobile.png" alt="AlphaESS on a 390-pixel phone viewport — example data" width="390">

</details>

Installing this beta through HACS and **restarting HA** packages the frontend and creates **AlphaESS (Beta)** in the sidebar, or migrates this fork's recognised three-view Beta dashboard. Existing managed AlphaESS strategy dashboards also use the dark design. No YAML copy/paste or separate frontend installation is needed. Refresh the frontend after restart. [Installation and migration details](https://github.com/PlainSeer/ha-alphaess-modbus/blob/v1.19.3b5/docs/managed-dashboard.md).

This makes it the default **AlphaESS integration dashboard**, without changing your personal Home Assistant landing page or overwriting unrelated dashboards. Viewing it sends no inverter commands; control rows open HA's existing details dialogs.

## Stable managed dashboard

Battery power is labelled **Charging**, **Discharging** or **Idle**. This is a
display clarification only; signed sensor values and inverter controls are unchanged.

Overview, Controls (including Peak Shaving) and Diagnostics, with automatic entity
discovery and no extra custom-card dependencies. Add it once; the layout follows
integration updates after restart and frontend refresh. Existing dashboards are
not overwritten, and viewing it does not enable inverter controls.
Diagnostics uses compact name/value rows in two columns of panels, stacking on phones.
In storage mode, the integration maintains its own versioned dashboard resource as a second loading path. Refresh the frontend after updates; YAML resources remain user-managed.
Diagnostic builds include a loading report to help investigate frontend failures without collecting entity values.

[Dashboard setup and replacement guide](https://github.com/PlainSeer/ha-alphaess-modbus/blob/main/docs/managed-dashboard.md).

![AlphaESS Overview — example values](https://raw.githubusercontent.com/PlainSeer/ha-alphaess-modbus/main/docs/images/dashboard-overview.png)

![AlphaESS Controls — example values, all modes off](https://raw.githubusercontent.com/PlainSeer/ha-alphaess-modbus/main/docs/images/dashboard-controls.png)

## Documentation and installation

For requirements, setup, controls and limitations, see the [README](https://github.com/PlainSeer/ha-alphaess-modbus#readme), [energy calculation notes](https://github.com/PlainSeer/ha-alphaess-modbus/blob/main/docs/passive-house-energy.md) and [release history](https://github.com/PlainSeer/ha-alphaess-modbus/releases).

Version-specific changes, fixes and acknowledgements belong in the [GitHub release notes](https://github.com/PlainSeer/ha-alphaess-modbus/releases).

Existing installations should update through HACS and retain their configured integration and entities.

## Credits

Original Python integration: [Senalse](https://github.com/senalse/ha-alphaess-modbus). Original YAML package: [Axel Koegler / Hillview](https://projects.hillviewlodge.ie/alphaess/). AlphaESS artwork identifies supported hardware; it does not imply endorsement.
