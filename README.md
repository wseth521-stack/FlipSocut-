# FlipScout AI Web v4.7 — Market Cluster Edition

v4.7 changes location handling from single cities to Nellis market clusters.

Market selector:
- Arizona (Phoenix + Mesa)
- Nevada (Las Vegas + North Las Vegas + Henderson)
- Houston Area (Houston + Katy)
- Dallas / Fort Worth (Dallas + Fort Worth + Arlington + Irving)
- Philadelphia, PA
- Denver, CO (Denver + Aurora)

Changes:
- Accepts legitimate multiple pickup cities within one selected market.
- Rejects explicit wrong-state listings.
- Parses location evidence from pickup city, state, shopping location, and pickup address.
- Expands sitemap discovery from roughly hundreds of candidates to as many as 2,500.
- Can inspect up to 2,200 candidate listing pages while looking for verified listings in the selected market.
- Keeps the lightweight HTTP-only scanner so Render Free does not need Chromium.
- Keeps category, subcategory, condition, bid range, minimum profit, resale estimates, max bid, and deal scoring.
- Footer updated to v4.7.
