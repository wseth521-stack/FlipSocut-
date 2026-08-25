# FlipScout AI Web v4.9 — Nellis Inventory Index

v4.9 replaces the old generic 100-listing discovery path.

It now:
- discovers Nellis's public Algolia client configuration from Nellis's own site;
- uses Nellis's public search index to collect a broad pool of product listing URLs;
- verifies the exact selected pickup location from each Nellis product page;
- keeps Phoenix and Mesa separate;
- keeps the other pickup cities separate;
- applies optional category/subcategory/star filters only after pickup verification;
- uses HTTP requests only, with no Selenium/Chromium active scan;
- remains suitable for Render Free testing.

If Nellis changes its search configuration, FlipScout now returns a clear search-index error rather than silently scanning Las Vegas inventory.
