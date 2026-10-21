# Optional managed AlphaESS dashboard

The bundled dashboard discovers enabled AlphaESS Modbus entities and groups them
into **Overview**, **Controls** and **Diagnostics**. It uses the integration's
entity registry identifiers, so entity renaming does not require manual YAML edits.
Each inverter gets its own views. Hidden and disabled entities are omitted.

The layout uses 95% of the available view width, with two columns of panels on
larger screens, stacked panels on phones and short single-line tile labels.
Long labels retain their full text in the tile's details/accessible description.
Diagnostics uses the same panels, grouping battery, grid, solar, energy, dispatch,
inverter/system and fault sensors. Other enabled sensors remain available in a
fallback group. Each diagnostic sensor uses a compact name/value row, without
repeated icons. Full names appear on the left and values on the right; long names
wrap on narrow screens. The two panel columns stack on phones.
No third-party dashboard cards are required.

Battery power shows **Charging** for negative readings, **Discharging** for
positive readings, and **Idle** at zero in Overview and Diagnostics. The displayed
magnitude keeps the sensor's unit; the underlying signed sensor value, history
and inverter controls are unchanged.

## Examples

These screenshots use invented values and the actual bundled card in a local
preview. They are examples, not readings from a live installation. Panels and
settings depend on which entities you have enabled.

![AlphaESS Overview with example values](images/dashboard-overview.png)

![AlphaESS Controls including optional Peak Shaving, all modes off](images/dashboard-controls.png)

## Add once

After installing a release containing this feature, restart Home Assistant and
refresh the browser/app frontend.

On HA 2026.5 or newer, go to **Settings → Dashboards → Add dashboard**, choose
**AlphaESS** under community dashboards, then choose the title/sidebar settings.

Alternatively, create a new empty dashboard, open its raw configuration editor,
and use the complete contents of [`managed_dashboard.yaml`](../examples/managed_dashboard.yaml):

```yaml
strategy:
  type: custom:alphaess-modbus
```

The integration registers its frontend module automatically. In storage resource
mode, it also creates or updates its own versioned Lovelace module resource as a
second loading path. Both paths use the same URL. It preserves existing resource
IDs and leaves unrelated resources and dashboard configurations alone. No separate
HACS frontend download is needed. YAML-managed resources are not modified; those
installations retain the extra-frontend-module loading path.

This fallback helps when the initial frontend module was missed. HA still starts
resources asynchronously and has a five-second strategy-registration timeout.
The diagnostic loader registers the strategies before importing the main dashboard
module. If that import fails or remains pending for eight seconds, it returns a
built-in Markdown report instead of depending on a custom card to display errors.
Already-open browser sessions need a full refresh after installing the update.

### Loading diagnostics

Diagnostic builds include a **Loader diagnostics** view when loading succeeds, or
a diagnostic report when the main module fails. The report shows the loader version,
relative timing, module execution stages, element registration and a bounded error
message. Server URLs are redacted; no entity values, credentials or stack traces are
collected. Nothing is uploaded, stored or sent to HA by the diagnostic loader.
Take a screenshot of the report when reporting a failure.

If HA's original plain strategy-registration timeout still appears, the diagnostic
loader itself did not register in time. It cannot report its own download or parse
failure. Successful loading on another device does not establish why that failed.
Imports are shared, with no automatic retry loop. Refresh to start a fresh attempt;
if a slow import finishes after the report appeared, revisit the dashboard.

If you uninstall the integration, also remove its `/alphaess_modbus/alphaess-dashboard.js`
resource from dashboard resources; removing only a dashboard does not require this.
If the strategy is not available,
refresh the frontend after restarting HA and check the logs for
`Could not load optional AlphaESS dashboard module`.

## Replace a view in an existing dashboard

Back up that view's YAML first. Replace **only the selected view's configuration**
with the appropriate example from [`managed_view.yaml`](../examples/managed_view.yaml):

```yaml
title: AlphaESS
path: alphaess
strategy:
  type: custom:alphaess-modbus
  page: overview
```

Use `page: controls` or `page: diagnostics` for additional views. Do not paste a
whole-dashboard `strategy:` configuration over a dashboard containing unrelated
views. With more than one inverter, set `device_id` under the view's strategy to
the HA device registry ID (the final ID in that device page's URL), not its entity
ID or IP address. The full managed dashboard discovers all inverters automatically.

## Updates and customisation

The one-time strategy configuration remains in HA. After integration updates,
restart HA and refresh the frontend to load the updated layout. No existing
dashboard is created, overwritten or migrated automatically.

A managed view follows the bundled design. To customise individual cards freely,
use the older static YAML examples instead; copied static cards remain yours and
do not update automatically. Keep your original dashboard until you are happy
with the replacement. Removing a managed dashboard/view removes only that layout;
it does not delete the integration, entities, history or inverter settings.

## Data and controls

Overview shows existing sensor states. The daily accumulated house-load value is
separate from the daily energy-balance estimate, which can decrease. An incomplete
day is marked when the accumulated sensor reports missing coverage. See
[energy calculation limits](passive-house-energy.md).

Opening the dashboard reads HA's registries and displays existing states. It does
not poll Modbus separately, alter registers, call services or enable modes. A tile
opens HA's normal more-info dialog; explicitly changing a setting there uses the
existing integration control. Maintenance buttons remain in their own labelled
group. Existing permissions still apply.

Peak Shaving is shown alongside its existing settings, including its upper
**Below SoC** threshold and read-only lower cutoff. Adding this dashboard does not
enable it. Read the [Peak Shaving guide](peak-shaving.md) before using it.

## Compatibility and checks

Frontend publishing is tested against HA 2024.12.5 and 2026.10.0. The community
picker is available from HA 2026.5; older installations can use the YAML strategy.
Discovery and rendering tests cover renamed entities, multiple inverters,
disabled/hidden entities, missing data, connection failures and passive tile
interactions. Local browser checks use example data, not a live inverter.

Home Assistant's [dashboard strategy documentation](https://developers.home-assistant.io/docs/frontend/custom-ui/custom-strategy/)
describes the underlying dashboard/view configuration.
