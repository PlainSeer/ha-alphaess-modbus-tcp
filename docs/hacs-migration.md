# Move to the standalone PlainSeer repository

The maintained repository is now
[`PlainSeer/ha-alphaess-modbus-tcp`](https://github.com/PlainSeer/ha-alphaess-modbus-tcp).
It is independent of GitHub's fork network. The previous
[`PlainSeer/ha-alphaess-modbus`](https://github.com/PlainSeer/ha-alphaess-modbus)
repository remains available for existing installations and rollback.

The integration is still called **AlphaESS Modbus TCP**. Its domain and install
directory remain `alphaess_modbus`, and its configuration, device/entity unique
IDs, dashboard URL and saved-dashboard ownership records are unchanged.
Existing Home Assistant integration entries should be retained.

## Existing HACS installations

The two repositories install into the same directory. Remove the old HACS
download before downloading the new one; removing the old download afterward
would also remove the newly installed files.

1. Make a Home Assistant backup and record the currently installed version.
2. In **HACS**, remove the download for `PlainSeer/ha-alphaess-modbus`.
   Keep the configured AlphaESS integration in **Settings → Devices & services**.
   Do not restart Home Assistant yet.
3. In **HACS → Custom repositories**, add
   `https://github.com/PlainSeer/ha-alphaess-modbus-tcp`, category **Integration**.
   Download **v1.20.2** from the new repository.
4. Once that download has completed, restart Home Assistant and refresh the
   frontend. The existing integration configuration is reused; do not add a
   second inverter entry.
5. Verify that the integration is loaded, existing entity IDs and readings are
   present, and both the AlphaESS dashboard and your other dashboards work.
   Check the usual battery/solar/grid readings and automation status.

HACS uses a different repository ID for the new source. Check any update
notifications or automations that refer to its HACS update entity after moving.
The inverter's integration entities continue using their existing identifiers.

If the new download fails, restore the previous repository's download before
restarting. To roll back later, remove the new HACS download, add/download the
previous repository at the recorded version, then restart. Retain the configured
AlphaESS integration throughout. Restoring the HA backup is the fallback if
configuration or registry entries were removed separately.

## New installations

Add the new repository to HACS as an **Integration**, download the latest stable
release, restart HA and configure **AlphaESS Modbus TCP** as usual.

## What moved

All 21 original branches and 18 tags were copied with their original commit IDs.
The 18 published releases were copied separately, including the v1.20.1 ZIP.
Historical release descriptions record their original publication date and
source; GitHub's displayed creation dates reflect the import.

Stars, watchers, download counters, issues and pull requests do not transfer to a
new repository. Their original records remain with the previous repository.
The new download badge counts assets downloaded from this repository.

Package installation/configuration preservation is checked offline against the
HACS 2.0.5 file lifecycle. Live HA migration and physical inverter operation have
not been exercised as part of creating this repository.
