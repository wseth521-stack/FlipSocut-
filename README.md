# FlipScout AI v5.0 Production

This is the production browser-scanner edition.

Why v5.0:
The lightweight HTTP-only builds could not reliably reproduce Nellis's market/session state. v5.0 restores the browser-based scanner that can work with the same rendered Nellis filters users see on the site.

Features:
- Exact pickup-location selector
- Phoenix and Mesa remain separate
- Other multi-pickup locations remain separate
- Condition/star-rating filtering
- Category and subcategory filtering
- Current bid range
- Minimum profit target
- Live scan progress
- Exact pickup verification on every item page
- Target bid low/high
- Absolute max bid
- Estimated resale
- Estimated profit
- Deal score
- Direct Nellis auction links
- No broken expand/full-screen results feature

Hosting:
This build includes Chromium + ChromeDriver and is intended for a Render Standard instance (2 GB RAM) or another Docker host with enough memory for Streamlit + headless Chromium.

Deployment:
1. Upload all files to the existing GitHub repository.
2. Commit to main.
3. Render Auto-Deploy will rebuild the service.
4. Upgrade the Render instance to Standard / 2 GB before production scanning.
