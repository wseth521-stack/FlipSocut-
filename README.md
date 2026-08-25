# FlipScout AI Web v4.5 — Location Authority Fix

This version keeps the lightweight Render-safe scanner and fixes cross-market inventory.

Changes:
- Uses the same Nellis query parameter names seen in browser URLs:
  - Location Name
  - Star Rating
  - Taxonomy Level 1
  - Taxonomy Level 2
- Normalizes friendly market labels such as Phoenix, AZ -> Phoenix.
- Hard-verifies every parsed listing's pickup city/state before displaying it.
- Phoenix accepts Phoenix/Mesa Arizona pickup inventory.
- Mesa accepts Mesa/Phoenix Arizona pickup inventory.
- Las Vegas accepts Las Vegas/North Las Vegas/Henderson Nevada inventory.
- Philadelphia accepts Philadelphia Pennsylvania inventory.
- Explicit wrong-state listings are always rejected.
- No Chromium/Selenium is used in the active scan path.
- Designed to stay compatible with Render Free during testing.

Upload all files over the existing GitHub files and commit to main. Auto-Deploy should deploy v4.5 automatically.
