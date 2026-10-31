# Saved AlphaESS dashboard

The default dashboard has three saved Home Assistant views: Main, Readings
and Diagnostics. It does not use a custom dashboard or view strategy. Only the
four-node power-flow diagram is a custom card; the remaining panels use native
Home Assistant cards. A delayed flow module cannot prevent the native views
from being generated.

## Layout

Solar and Grid sit on the left, Battery and Load on the right, around a central
inverter. Solid paths carry smooth dots whose speed follows power. Direction
labels and unsigned diagram values make import/export and charge/discharge
explicit. Zero and unavailable readings stop their dots. The diagram has no EV
branch. Tapping a node opens its entity details and history.

Main keeps the four summary readings beneath the flow and includes all enabled
settings, grouped by function. Readings contains daily energy, solar strings,
battery details, filtered power and house-energy estimates. Maintenance tiles
open more-info rather than running a button immediately. Diagnostics shows each
enabled sensor and binary sensor once. Panels stack on phones and follow the
active Home Assistant theme.

The first control groups use this two-column order on wider screens:

| Left | Right |
| --- | --- |
| Battery & Grid | Force charging |
| Force discharging | Dispatch |
| Force import | Force export |
| Excess export | Peak shaving |

Operating settings, operating modes, scheduling and maintenance continue below.
Below 768 pixels, Main starts with Battery & Grid (including its four summary
readings), Force charging, Force discharging and Dispatch. The other cards keep
their previous stacked order. At 768 pixels and above, the two desktop columns
are retained. Screen-size visibility uses native Home Assistant configuration.

## Install and migration

For this beta, enable beta versions for the repository in HACS, select
v1.21.1b1, restart Home Assistant, then refresh its frontend. HACS displays the README for the installed
version, so an older installation may still show older previews until updated. During
integration setup, entity registry entries are matched by platform, config-entry
ID and stable unique ID. Renamed entities and multiple inverters are supported;
disabled, hidden and restored orphan entries are omitted.

The default URL remains `/dashboard-alphaess-native`. If it does not exist,
setup creates an admin-only **AlphaESS** dashboard. Existing sidebar metadata
and permissions are preserved on migration. In particular, an existing recognised
**AlphaESS (Beta)** dashboard keeps that name rather than creating a second entry.
The existing per-inverter view routes are retained even though their tab labels
now read Main, Readings and Diagnostics.

Only these existing configurations are automatically replaced:

- This fork's exact default `custom:alphaess-modbus` dashboard strategy, with no
  additional configuration keys.
- Its recognised three-view `custom:alphaess-beta-live` beta layout.
- The integration-owned default at this URL, including manually edited layouts.
  Ownership is recorded when the integration creates or migrates its dashboard;
  merely sharing this URL does not make an unrelated dashboard integration-owned.

Before migration, the original configuration is saved once through HA's Store
API as `alphaess_modbus.saved_dashboard_backup`. Before every replacement, the
immediately previous layout is also saved as `alphaess_modbus.saved_dashboard_previous`.
The original backup is retained; the previous-layout backup is replaced on each
subsequent update. If either required backup fails, replacement does not proceed. No other dashboard is edited. YAML dashboards remain managed
by their owner.

Entity/device registry events are debounced and the saved configuration is only
written when it actually changes. The default is maintained automatically on
integration setup (including after an update/restart) and registry changes.
Manual edits do not detach it: new sensors, renamed entities and layout changes
continue to appear. Viewing pages does not trigger configuration writes.

For permanent customisations, copy the dashboard configuration into a separate
storage dashboard with a different URL. The copy is user-managed and is never
refreshed by this integration. Keep custom copies updated manually when changing
entity IDs. The default sidebar metadata, permissions and view routes are retained.

## Automatic management (development)

**Manage AlphaESS dashboard automatically** is enabled by default in the
integration's options. Turning it off stops automatic creation and layout
updates, including after restart. It leaves the existing dashboard, sidebar
metadata, custom copies and frontend resources in place. The dashboard remains
usable and its sensor readings continue to update normally. This setting applies
in place: it does not reload the inverter integration, refresh polling, change
PredBat settings, call control services or write inverter registers.

All inverters share one saved dashboard. If any configured AlphaESS entry opts
out, automatic management pauses for the whole shared dashboard. Re-enable the
setting on every entry to resume. While management is off, manual changes to
the default are preserved; re-enabling restores the normal managed-layout policy
and backs up changes before replacement. A separate dashboard copy remains
user-managed regardless of this option.

Only AlphaESS entity/device registry events schedule a debounced refresh.
Renames, removals and visibility changes remain supported; unrelated registry
edits do not wake the dashboard updater. This does not alter live state updates.
Setup, preference changes and background refreshes use the same save lock.

A pending-save record is persisted before replacing the layout. If a layout or
ownership-completion save fails, the next setup or relevant refresh retries it,
even when HA's in-memory cache already shows the attempted layout. The last
working layout backup is retained. A failed initial layout can also be completed
on a later setup. If initial ownership recording fails before content is saved,
only the just-created blank registration is rolled back; a user edit made in the
meantime is preserved. Errors are logged; there is no repeated automatic retry
loop. Recovery never adopts a dashboard with a different recorded identity or a
YAML dashboard.

## Loading

The old strategy loader remains packaged for separately created legacy strategy
dashboards. The new default has no strategy timeout to wait for. The flow card
still needs its packaged JavaScript; if that file cannot load, HA may show a
card error while the other panels remain available. This is not a guarantee
against unrelated network or Companion App failures.

## Rollback

Export the saved dashboard configuration before making manual changes. To undo
the migration, restore the saved original config through HA's dashboard editor
or Lovelace configuration API, then select v1.19.3b6 in HACS, restart and refresh.
Restoring v1.19.3b6 alone leaves native panels readable but does not provide the
new flow card. Never edit HA's internal storage files directly.

## Preview and validation

README images are local renders of the production flow card with representative
native panels and example values. They are not connected-inverter screenshots;
native card appearance can vary with HA version/theme. The preview fixtures
and renderer are in `tools/dashboard-preview/`.

Automated checks cover migration backups, no-op refreshes, updates after manual
edits, protection of unrelated/YAML/custom-copy dashboards, entity
coverage and multiple inverters, passive flow interactions and signed readings.
Browser checks cover Chromium and WebKit at phone/tablet/desktop widths. A real
iPhone Companion App cold-start check is still required after installation.

