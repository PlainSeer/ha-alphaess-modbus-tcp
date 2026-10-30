# Saved dashboard preview

This local fixture renders the production `alphaess-power-flow` card directly.
The surrounding native-panel previews are representative renderers, not a
running Home Assistant frontend. The README previews use a light theme and daytime example readings. The fixture metadata
contains generic entry/device IDs and no inverter serials, connection settings,
credentials or captured household readings.

Generate saved views after installing Home Assistant test dependencies:

```sh
PYTHONPATH=. python tools/dashboard-preview/generate.py
```

Serve the repository root and open `/tools/dashboard-preview/index.html`.
The preview requires the Material Design Icons 7.4.47 CSS and WOFF2 font in
`vendor/`; download from `@mdi/font@7.4.47` and rewrite the CSS font paths to the
same folder. Vendor files are not committed or installed by the integration.
Home Assistant itself provides `ha-icon`, `ha-card` and the native card renderer.

`check-browser.cjs` uses a locally installed Playwright to test Chromium and
WebKit at 320, 390, 667, 767, 768, 844 and 1024 pixels. It serves only the repository on a
loopback port, blocks external browser requests, checks runtime errors and
horizontal clipping, and writes the Main, Readings, Diagnostics and phone README screenshots. It has no HA
credentials or live service endpoints.

The previous strategy preview remains in `preview.js` for legacy development;
the default `index.html` now previews the saved dashboard.
