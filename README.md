# FlipScout AI Web v4.0

This is the deployable website edition of FlipScout.

## What changed
- No local Python required for customers.
- No visible Selenium scanner window.
- No Chrome extension.
- No pasted Nellis URL.
- No Troubleshooting tab.
- No Flip Tracker.
- No expand/full-screen-results experiment.
- Nellis scanning runs in a hidden server-side Chrome browser.

## Local Docker test
Build:
docker build -t flipscout-web .

Run:
docker run --rm -p 8501:8501 -e PORT=8501 flipscout-web

Then open:
http://localhost:8501

## Deploy to Render
1. Create a GitHub repository.
2. Upload all files from this folder to the repository.
3. In Render, choose New > Web Service.
4. Connect the GitHub repository.
5. Render will detect the Dockerfile.
6. Use a paid/starter instance rather than a very small free instance because Chrome scanning needs memory.
7. Deploy.
8. Add a custom domain later if desired.

## Customer workflow
1. Visit FlipScout website.
2. Enter a Nellis location.
3. Optionally enter category/subcategory and condition/star rating.
4. Set current-bid range and minimum-profit target.
5. Click FIND PROFITABLE DEALS.
6. FlipScout scans Nellis server-side and shows results.

## Important
The Nellis site can change its markup/filter labels. Server-side browser automation is more deployment-friendly than the old local scanner, but the filter-clicking selectors may occasionally need maintenance if Nellis changes its UI.
