# FlipScout AI Web v4.3 — Lightweight Render Edition

This build removes Chromium from the active Nellis scan path.

Why:
Render Free provides 512 MB RAM. The prior Streamlit + Chromium/Selenium process exceeded that limit and was killed with status 137.

v4.3:
- Uses normal HTTP requests instead of launching Chromium.
- Keeps the FlipScout UI, deal settings, profit analysis, and results workflow.
- Has bounded network timeouts.
- Runs on Render's Free plan for testing.
- Does not include the broken expand/full-screen feature.

Important:
Nellis may serve some inventory dynamically. If its public search HTML does not expose listing URLs to ordinary HTTP requests, FlipScout will report zero links rather than crash the server. That result will tell us whether the next step should use Nellis's underlying public data requests/API instead.


## v4.3 fix
Removed the remaining unconditional Selenium import that prevented the lightweight Render build from starting.
