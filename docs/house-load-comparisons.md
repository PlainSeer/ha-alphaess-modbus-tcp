# Optional house-load comparisons

Development experiment for [issue #10](https://github.com/PlainSeer/ha-alphaess-modbus-tcp/issues/10), introduced in v1.21.4b2. This does not change the default house-load calculation or resolve every register-timing peak.

## Scope

Two separate diagnostic power sensors are available:

| Name | Unique-ID suffix | Method |
| --- | --- | --- |
| House Load Median 5 (Comparison) | `house_load_median_5_comparison` | Median of up to five fresh accepted samples within ten seconds |
| House Load Battery Step (Comparison) | `house_load_battery_step_comparison` | Briefly hold the previous accepted result around a large battery/load step |

Both are **disabled by default**. Disabled entities do not subscribe to updates or run expiry timers. Enabled entities only observe the existing coordinator and maintain their own in-memory filter state. They do not issue Modbus requests, modify the shared house-load sample, create energy totals, or call controller services. Existing house-load/filtered sensors, daily accumulation and dispatch controllers retain their inputs and behaviour. The separate opt-in PV meter counter from v1.21.4b1 is unchanged.

## How the experiments behave

Only completed polls with newly read required power inputs advance a filter. Fresh unchanged readings count; cached callbacks and partial polls do not. Power inputs must be less than ten seconds old, and operating mode less than sixty seconds old. EPS retains its existing grid-only source calculation and bypasses the battery-step heuristic.

The **median** keeps at most five accepted values less than ten seconds old. At a steady two-second cadence, five accepted values give the usual five-sample trailing median. During warm-up or slower polling, the newest odd-sized window of one or three samples is used instead; `window_samples` reports the effective size. This avoids averaging two observations into an invented midpoint. Response delay depends on cadence and the signal; it is not always four seconds.

The **battery-step hold** requires both a battery-power change greater than 2,000 W between accepted power samples and a house-load change greater than 2,000 W from its accepted baseline. It can continue that hold for the next fresh sample, but stops after two held samples or four elapsed seconds, whichever comes first. Repeated changes do not extend the original hold deadline. If the deadline arrives without another fresh sample, the comparison reports unknown until fresh input arrives. This is a heuristic, not a dispatch-command detector.

During the source's bounded negative-balance estimate, each experiment can retain its own preceding result only until its existing deadline or the source estimate's deadline, whichever is earlier. Estimated source values do not fill the median window or seed a new baseline. Missing/failed input immediately clears history. Long gaps, mode changes and restart reseed from accepted input. Stale values expire through an entity-only timer even if polling stops; that timer never notifies dispatch controllers. Filter state is not restored after restart. The power filters use elapsed time rather than local calendar boundaries; existing daily-counter midnight and restore handling is unchanged.

These rules deliberately differ from an offline replay that forward-fills missing values or counts held estimates as fresh observations.

## Known trade-offs

Both filters can remove genuine short loads. A real increase from 500 W to 3,500 W lasting four seconds, supplied by the battery, can be absent from both comparison outputs. Normal self-consumption can produce a battery step just as a dispatch transition can. A test containing only short loads without nearby battery steps cannot establish that all genuine loads are preserved.

Filtered energy can be lower because artifacts were removed, real load was omitted, or both. A small net kWh difference is not proof of accuracy. Recorder reconstruction also cannot establish a synchronised inverter snapshot or independently identify every dispatch transition. Use independent measurements where available.

## Testing alongside existing readings

1. Install the beta and restart Home Assistant as usual. Enable the two disabled diagnostic sensors under the AlphaESS device's entities **while no dispatch is active**: enabling an entity can cause Home Assistant to reload the integration.
2. Keep existing house-load, accumulated energy and control mappings unchanged. Record the original **Current House Load** alongside both comparisons.
3. Include ordinary battery-to-house operation, sustained and short appliance loads, charging/discharging transitions, PV changes and gaps. Observe normal transitions; do not create rapid inverter-command sequences to exercise the filters.
4. In [issue #10](https://github.com/PlainSeer/ha-alphaess-modbus-tcp/issues/10), report timestamps, polling mode, original/comparison values and their attributes. Export history for offline energy comparison if desired, accounting for unknown periods and estimate expiry.

Attributes identify `experimental`, `comparison_only`, `method`, `source`, `estimated`, `sample_reason`, `window_samples` and configured time/sample limits. They describe filter decisions, not verified physical truth. Do not select these experiments as control inputs or replace an existing energy source on the strength of a cleaner graph.

Regression tests include the public sample-3 recorder excerpt, true short and sustained loads, source failures, stale/partial/unchanged polls, expiry, mode changes, restart, clock rollback and unchanged original daily totals across midnight. These are offline checks, not hardware certification.
