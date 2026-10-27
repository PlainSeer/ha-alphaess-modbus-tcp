# AlphaESS Modbus TCP — PlainSeer

[Repository](https://github.com/PlainSeer/ha-alphaess-modbus-tcp) · [Issues](https://github.com/PlainSeer/ha-alphaess-modbus-tcp/issues) · [HACS migration](https://github.com/PlainSeer/ha-alphaess-modbus-tcp/blob/main/docs/hacs-migration.md)

![AlphaESS icon](https://raw.githubusercontent.com/PlainSeer/ha-alphaess-modbus-tcp/main/custom_components/alphaess_modbus/brand/icon.png)

Local Home Assistant integration for AlphaESS inverters via Modbus TCP, without a cloud dependency.

## USE AT YOUR OWN RISK

Unofficial community-maintained software, supplied without warranty. The integration includes controls that write to your inverter. Incorrect settings can cause unexpected operation, financial loss or equipment damage. Keep a restorable backup. Passing tests does not certify hardware safety.

## About this project

Standalone successor to [PlainSeer's previous repository](https://github.com/PlainSeer/ha-alphaess-modbus), originally derived from Senalse's v1.14.0 integration, with passive sensors and optional controls inspired by Axel Koegler's Hillview package. Original authors retain full credit. This is not an official AlphaESS integration or full Hillview parity.

## Sensor overview

- **Current House Load Filtered / Current PV Production Filtered:** smooth the existing power calculations. These displayed values do not drive inverter controls.
- **Excess Power:** filtered PV minus filtered house load, clamped at zero; an estimate, not an export command.
- **Battery Full:** on when existing raw battery status is `1` (charging not permitted, discharging permitted), matching Hillview. Invalid, missing or unrecognised status makes it unavailable.
- **Today's House Energy:** a daily balance of import, export, PV and battery energy, including AC PV once. This estimate can decrease during the day.
- **House Energy Since Tracking:** accumulates from zero when first enabled; not lifetime consumption. Downtime can leave gaps.
- **House Load Energy Today (Accumulated):** integrates existing house-load power into kWh. Only increases within each day, resets at HA-local midnight and restores same-day totals after restart. Missing readings and downtime leave gaps.

These passive sensors use data already read by the integration, without adding register reads or writes or changing polling. They do not automatically replace existing helpers or automations. See the energy calculation notes below before using the estimates.

## Optional Peak Shaving

**Off by default and after restart.** Uses existing dispatch controls to reduce grid import towards a target when explicitly enabled. Simulation-tested, not yet tested on physical inverter hardware; it cannot guarantee a hard import cap or prevent transient export. See the [feature guide and limitations](https://github.com/PlainSeer/ha-alphaess-modbus-tcp/blob/main/docs/peak-shaving.md).

Hillview's Force Import pause/resume and Cutoff SoC Hold monitoring changes have not been adopted, and optional PV energy registers are not included.

## Default saved dashboard

**v1.20.2** provides three saved Home Assistant views: **Main**, **Readings** and **Diagnostics**. It does not use a custom dashboard or view strategy. Native panels stack on phones.

**Main** keeps the four-node flow and its battery charge, health, temperature and work-mode readings together. Force charging/discharging, export/import, Peak Shaving, operating settings, schedules and maintenance are on the same page. **Readings** contains daily energy, solar strings, battery details, filtered power and house-energy estimates. Diagnostics remains separate.

![AlphaESS Main — local preview with example data](https://raw.githubusercontent.com/PlainSeer/ha-alphaess-modbus-tcp/cbf2b9229f95cfb16cfa224b13fe6c2c447e49ae/docs/images/dashboard-main.png)

<details>
<summary>Readings, Diagnostics and phone previews</summary>

![AlphaESS Readings — local preview with example data](https://raw.githubusercontent.com/PlainSeer/ha-alphaess-modbus-tcp/cbf2b9229f95cfb16cfa224b13fe6c2c447e49ae/docs/images/dashboard-readings.png)

![AlphaESS Diagnostics — local preview with example data](https://raw.githubusercontent.com/PlainSeer/ha-alphaess-modbus-tcp/cbf2b9229f95cfb16cfa224b13fe6c2c447e49ae/docs/images/dashboard-diagnostics.png)

<img src="https://raw.githubusercontent.com/PlainSeer/ha-alphaess-modbus-tcp/cbf2b9229f95cfb16cfa224b13fe6c2c447e49ae/docs/images/dashboard-mobile.png" alt="AlphaESS Main on a 390-pixel phone viewport — local preview with example data" width="390">

</details>

Images use the production flow card and representative native panels with example readings; they are not screenshots of a connected inverter. Native card appearance follows your HA version and theme.

Update to **v1.20.2** through HACS, restart HA and refresh the frontend. A fresh installation creates **AlphaESS**. An existing recognised **AlphaESS (Beta)** dashboard is updated in place, keeping its current name, permissions and URL. No duplicate is created. Customised, YAML and unrelated dashboards are preserved. [Setup, migration and rollback](https://github.com/PlainSeer/ha-alphaess-modbus-tcp/blob/v1.20.2/docs/saved-dashboard.md).

HACS displays the README from the installed version. An older installation can therefore show older previews until updated; refreshing HACS alone does not change the installed version.

## Documentation and installation

Moving from the previous PlainSeer repository? Read the [HACS migration guide](https://github.com/PlainSeer/ha-alphaess-modbus-tcp/blob/main/docs/hacs-migration.md) first.

For requirements, setup, controls and limitations, see the [README](https://github.com/PlainSeer/ha-alphaess-modbus-tcp#readme), [energy calculation notes](https://github.com/PlainSeer/ha-alphaess-modbus-tcp/blob/main/docs/passive-house-energy.md) and [release history](https://github.com/PlainSeer/ha-alphaess-modbus-tcp/releases).

Version-specific changes, fixes and acknowledgements belong in the [GitHub release notes](https://github.com/PlainSeer/ha-alphaess-modbus-tcp/releases).

Existing installations should update through HACS and retain their configured integration and entities.

## Credits

Maintainer: [PlainSeer](https://github.com/PlainSeer).

Original Python integration: [Senalse](https://github.com/senalse/ha-alphaess-modbus). Original YAML package: [Axel Koegler / Hillview](https://projects.hillviewlodge.ie/alphaess/). AlphaESS artwork identifies supported hardware; it does not imply endorsement.

## Release downloads

[![Release downloads](https://img.shields.io/github/downloads/PlainSeer/ha-alphaess-modbus-tcp/total?label=release%20downloads)](https://github.com/PlainSeer/ha-alphaess-modbus-tcp/releases)

Counts release assets hosted in this repository, including updates and repeat downloads. These are not unique installations; counts and stars from the previous repository do not transfer.
