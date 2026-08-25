# FlipScout AI Web v4.6 — Multi-Market Lightweight Edition

Current market selector:
- Phoenix, AZ
- Las Vegas, NV
- Houston, TX
- Philadelphia, PA
- Denver, CO
- Dallas, TX

How v4.6 works:
- Uses a controlled market dropdown instead of free-text location entry.
- Tries the lightweight Nellis search page first.
- Hard-verifies every listing's actual pickup city/state.
- If Nellis serves its default market instead, FlipScout falls back to Nellis sitemap product discovery.
- Stops only after finding verified listings for the selected market or hitting a bounded candidate limit.
- Keeps category, subcategory, condition/star rating, bid range, minimum profit, resale estimates, max bid, and deal scoring.
- No Selenium/Chromium active scan path.
- Designed for Render Free testing.
- Footer/version label updated to v4.6.

Known market verification clusters:
- Phoenix market: Phoenix + Mesa, AZ
- Las Vegas market: Las Vegas + North Las Vegas + Henderson, NV
- Houston market: Houston + Katy, TX
- Philadelphia market: Philadelphia, PA
- Denver market: Denver, CO
- Dallas market: Dallas, TX
