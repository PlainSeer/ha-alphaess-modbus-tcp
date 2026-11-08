# Passive house-energy and battery-full indicators

These additions use data already present in the coordinator. They make no Modbus requests and issue no inverter commands. Other entities, daily calculations, PredBat helpers and controls are unchanged.

## Battery Full

On means the existing raw `battery_status` is exactly `1` (charging not permitted, discharging permitted), matching Hillview’s battery-full interpretation clarified by Axel Koegler. Other documented statuses (`0`, `256`, `257`, `512`, `513`) are off; missing, malformed or unrecognised statuses and coordinator failure make it unavailable. This uses the existing 0x0103 reading and its unchanged polling interval. It does not infer full from SoC.

This corrects the former Battery Full (SoC) indicator in place: the unique ID retains `_battery_full_soc`, preserving existing entity IDs and custom names. The default display name becomes Battery Full. Existing Battery Status text labels remain unchanged for compatibility; this indicator compares the raw numeric value, not those labels. It is a passive indication, not a charging command or an independent guarantee of cell balancing.

## Today's House Energy

Calculation: daily grid import − grid export + PV − battery charge + battery discharge, in kWh. Reuses existing daily entities, including their AC-PV contribution exactly once. Those five daily source entities must remain enabled and have valid same-day baselines. The new sensor waits while baselines are missing or dates disagree at startup/midnight; a counter below its baseline makes it unavailable rather than treating a reset as zero consumption.

It inherits source rounding, baseline timing, AC-PV integration and restore limitations. A first installation partway through a day does not recover the earlier hours. Source dates use the existing integration's date handling; an HA/host timezone mismatch can keep this estimate unavailable. No existing daily logic has been changed.

## House Energy Since Tracking

Starts at zero, using counter deltas from the start of tracking; it is not a historical lifetime total. All five counter timestamps must advance before a new counter balance is accepted. Positive AC-PV power is integrated using the previous successful sample for intervals up to 60 seconds. Negative AC power contributes zero.

The accumulated value and counter snapshot restore after restart. Counter deltas can recover during outages, but AC-PV generation during downtime cannot be reconstructed. Gaps longer than 60 seconds are skipped and `ac_energy_gap` records that history is incomplete. This flag also becomes true after restoration. If any counter decreases, all counter baselines are rebased and `counter_resets` increments; energy in that interval is unknowable and omitted.

Both energy entities are estimates: asynchronous readings, counter resolution and energy-conversion losses can cause differences from integration of house-load power. Small decreases (including a small negative initial value) are possible, so state class is `total`, not `total_increasing`. Do not use these as billing measurements.

## Existing setup and rollout

Keep the existing PredBat house-energy integration helper and daily utility meter. No configuration is migrated, no automation consumes these new entities, and installing this release does not migrate PredBat to these sensors. Compare recorded values over complete days before separately deciding whether to change any consumer. The battery indicator does not control charging, Hold/Freeze, EV charging, export or Demand/ECO.

Tests exercise the real HA sensor classes with an in-memory coordinator: counter balance/reset, partial refresh, invalid inputs, AC-PV accounting/gaps, restore precision, midnight baselines, battery status codes and registry identity. They do not connect to an inverter or certify live operation.

## House Load Energy Today (Accumulated)

Separate increment-only daily estimate, using Current House Load rather than the daily counter balance. Adds accepted power × elapsed seconds / 3,600,000 using the previous sample (left integration). Outside the legacy EPS calculation, a negative balance is inconsistent: the previous accepted power may be estimated only until its original 10-second expiry, then the omitted interval is flagged. Missing/failed reads do not qualify for this estimate. It updates on completed coordinator callbacks even if power is constant; it issues no new Modbus requests and does not use filtered power. Positive balances are not rejected merely because they increase sharply. This avoids discarding genuine short, ramping or changing loads, but does not remove positive artifacts from asynchronous readings; a positive artifact can also be held during a following negative balance.

Resets at midnight in the HA timezone, including daylight-saving transitions. A callback spanning midnight counts only the new-day part in the new total. Same-day accumulated kWh restores at full precision, but the prior power sample is discarded so restart downtime is not invented. The first day is partial and starts at zero when enabled. HA must be running to display a midnight reset; starting on a later day starts a new total.

Once a daily amount is known from accepted power or a same-day restore, it remains numeric when required readings are missing, malformed, unsuccessful or stale (>60 seconds), or the bounded estimate expires. Accumulation pauses, `accumulation_paused` becomes true, and `incomplete_day` flags lost coverage. The retained total is a partial estimate, not evidence that real consumption was zero during the gap. No unmeasured power is added and the gap is not backfilled on recovery. Intervals longer than 60 seconds are also skipped. Before any amount is known, the sensor remains unavailable rather than manufacturing zero. Backwards clock adjustments suspend publication and accumulation until time catches up, including across a source outage.

The entity is passive and has state class `total_increasing`; it issues no inverter commands and does not automatically configure consumers. PredBat or another application can read it if separately configured, so a retained partial amount must not be mistaken for complete consumption history. Keep existing helpers and compare full days before separately choosing to migrate consumers. Power integration is an estimate, not recovered hardware-meter history. Tests cover constant power, invalid/stale inputs, outages, negative power, midnight/DST dates, backwards time and restart restoration.
