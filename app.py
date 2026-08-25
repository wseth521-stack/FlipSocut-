import urllib.parse
import json

import streamlit as st
import pandas as pd
import numpy as np
import requests

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.common.exceptions import WebDriverException, TimeoutException
except Exception:
    webdriver = None
    ChromeOptions = None
    WebDriverException = Exception

import re
import time
from html import unescape
from datetime import datetime, timezone
from pathlib import Path
import pickle

COMP_CACHE_PATH = Path.home() / ".flipscout_comp_cache.pkl"

def load_comp_cache():
    try:
        if COMP_CACHE_PATH.exists():
            data = pickle.loads(COMP_CACHE_PATH.read_bytes())
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}

def save_comp_cache(cache):
    try:
        COMP_CACHE_PATH.write_bytes(pickle.dumps(cache))
        return True
    except Exception:
        return False

def clear_comp_cache():
    try:
        if COMP_CACHE_PATH.exists(): COMP_CACHE_PATH.unlink()
        return True
    except Exception:
        return False


CONFIG_PATH = Path.home() / ".flipscout_config.json"

def load_local_config():
    try:
        if CONFIG_PATH.exists():
            import json
            return json.loads(CONFIG_PATH.read_text())
    except:
        pass
    return {}

def save_local_config(data):
    try:
        import json
        CONFIG_PATH.write_text(json.dumps(data))
        return True
    except:
        return False

def clear_local_config():
    try:
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
        return True
    except:
        return False



def recommended_bid_range(resale_value, premium, tax, selling_fee, min_profit, current_bid=0):
    """Return profit-first best bid range and hard ceiling."""
    resale = clean_num(resale_value) or 0
    cbid = clean_num(current_bid) or 0
    if resale <= 0:
        return 0.0, 0.0, 0.0

    net_resale = resale * (1 - float(selling_fee))
    denom = (1 + float(premium)) * (1 + float(tax))
    if denom <= 0:
        denom = 1.0

    absolute_max = max(0.0, (net_resale - float(min_profit)) / denom)

    # Profit-first zone preserves extra cushion instead of bidding right to the ceiling.
    best_low = max(cbid, absolute_max * 0.65)
    best_high = max(best_low, absolute_max * 0.85)

    return round(best_low, 2), round(best_high, 2), round(absolute_max, 2)


def enforce_final_verdicts(scored, min_bid, max_bid, min_profit, min_roi):
    """Authoritative final deal gate. Nothing downstream may upgrade a row to BUY."""
    out = scored.copy()

    for i in out.index:
        bid = clean_num(out.at[i, "Current Bid"]) if "Current Bid" in out.columns else clean_num(out.at[i, "currentBid"]) if "currentBid" in out.columns else None
        profit = clean_num(out.at[i, "Projected Profit"]) if "Projected Profit" in out.columns else 0
        roi_pct = clean_num(out.at[i, "Projected ROI %"]) if "Projected ROI %" in out.columns else 0
        source = str(out.at[i, "Resale Source"]) if "Resale Source" in out.columns else ""

        # Recover comp evidence from either scored output or underlying source columns.
        comp_count = 0
        for c in ["Comp 1 $","Comp 2 $","Comp 3 $","comp1","comp2","comp3"]:
            if c in out.columns:
                v = clean_num(out.at[i,c]) or 0
                if v > 0:
                    comp_count += 1
        # Avoid double counting if both display and raw columns exist.
        comp_count = min(comp_count, 3)

        override = 0
        for c in ["Resale Override $","resaleOverride"]:
            if c in out.columns:
                override = max(override, clean_num(out.at[i,c]) or 0)

        verified_resale = (comp_count >= 2) or (override > 0)
        msrp_only = ("msrp" in source.lower()) and not verified_resale

        title = str(out.at[i,"title"] if "title" in out.columns else out.at[i,"Title"] if "Title" in out.columns else "").lower()
        condition_text = str(out.at[i,"condition"]).lower() if "condition" in out.columns else ""

        # v3.11: only the listing title and simple Nellis Condition influence risk.
        # Detailed badge parsing is intentionally excluded because it caused false SKIPs.
        risk_blob = " ".join([title, condition_text])

        severe_terms = [
            "not functional",
            "major damage",
            "parts only",
            "for parts",
            "broken",
            "incomplete",
            "upper unit only"
        ]
        serious_risk = any(x in risk_blob for x in severe_terms)

        caution_terms = [
            "untested",
            "as-is",
            "as is",
            "used"
        ]
        caution_risk = any(x in risk_blob for x in caution_terms)

        within_budget = bid is not None and bid >= float(min_bid) and bid <= float(max_bid)
        meets_profit = (profit or 0) >= float(min_profit)
        # UI min_roi may be decimal (.60) while displayed ROI is percent (60).
        required_roi_pct = float(min_roi) * 100 if float(min_roi) <= 10 else float(min_roi)
        meets_roi = (roi_pct or 0) >= required_roi_pct

        if serious_risk:
            verdict = "SKIP"
            reason = "SERIOUS LISTING RISK"
        elif not within_budget:
            verdict = "SKIP"
            reason = f"OUTSIDE BID RANGE ${float(min_bid):.0f}-${float(max_bid):.0f}"
        elif verified_resale and meets_profit and meets_roi and not msrp_only and not caution_risk:
            verdict = "BUY"
            reason = "VERIFIED RESALE + PROFIT PASS"
        elif within_budget and meets_profit and meets_roi:
            verdict = "WATCH"
            reason = "RESEARCH / CONDITION CAUTION" if caution_risk else "VERIFY SOLD COMPS"
        else:
            verdict = "SKIP"
            reason = "PROFIT BELOW RULES"

        out.at[i,"Verdict"] = verdict
        if "Data Status" in out.columns:
            out.at[i,"Data Status"] = reason
        if not verified_resale and "Resale Source" in out.columns:
            out.at[i,"Resale Source"] = "UNVERIFIED / MSRP estimate"

    return out



# ---------- v2.0 Product Identity ----------
MODEL_STOPWORDS={"MAX","FUEL","CORDLESS","TOOL","COMBO","KIT","OPEN","BOX","NEW","USED","PIECE","PACK","VOLT","AMP","BRUSHLESS","PREMIUM","GENERIC"}

def extract_product_identity(title, brand=""):
    text=str(title or "").upper()
    brand_u=str(brand or "").upper().strip()
    upc_match=re.search(r'(?i)\b(?:UPC|EAN)\s*[:#-]?\s*(\d{8,14})\b',str(title or ""))
    upc=upc_match.group(1) if upc_match else ""

    # Accept classic alphanumeric models (DW861W) and numeric-hyphen models (2737-20).
    tokens=re.findall(r'\b(?:[A-Z]{1,8}\d[A-Z0-9-]{2,14}|\d{3,6}-\d{2,6})\b',text)
    candidates=[]
    for tok in tokens:
        compact=tok.replace("-","")
        if tok in MODEL_STOPWORDS or tok==brand_u: continue
        if re.fullmatch(r'\d+(?:V|W|A|AH|IN|MM|CM|PC|PCS)',compact): continue
        if compact in {"20VMAX","18VMAX","12VMAX","M18","M12"}: continue
        # Numeric-hyphen manufacturer formats are accepted; otherwise require letters+digits.
        valid_numeric_hyphen=bool(re.fullmatch(r'\d{3,6}-\d{2,6}',tok))
        valid_alpha_num=bool(re.search(r'[A-Z]',compact) and re.search(r'\d',compact))
        if valid_numeric_hyphen or valid_alpha_num:
            candidates.append(tok)

    def strength(tok):
        if re.fullmatch(r'\d{3,6}-\d{2,6}',tok): return 3
        c=tok.replace("-","")
        if re.match(r'^[A-Z]{2,}\d{2,}',c): return 3
        if re.search(r'[A-Z]{2,}',c) and len(re.findall(r'\d',c))>=2: return 2
        return 1

    candidates=sorted(dict.fromkeys(candidates),key=lambda x:(strength(x),len(x)),reverse=True)
    model=candidates[0] if candidates and strength(candidates[0])>=2 else ""
    identity="UPC/EAN" if upc else ("Likely exact model" if model else "Product family only")
    confidence="HIGH" if upc else ("MEDIUM" if model else "LOW")
    query=" ".join(x for x in [brand_u,model] if x).strip()
    if not query:
        words=[w for w in re.findall(r'[A-Za-z0-9]+',str(title or "")) if w.upper() not in MODEL_STOPWORDS]
        query=" ".join(([brand_u] if brand_u else [])+words[:5]).strip()
    return {"deepUPC":upc,"model":model,"identity":identity,"identityConfidence":confidence,"exactQuery":query}


# ---------- v2.6 Free/Direct Nellis Discovery ----------
DIRECT_CACHE_PATH = Path.home() / ".flipscout_direct_cache.json"
DIRECT_CACHE_SCHEMA = 301


def load_direct_cache():
    try:
        if DIRECT_CACHE_PATH.exists():
            return json.loads(DIRECT_CACHE_PATH.read_text())
    except:
        pass
    return {}

def save_direct_cache(cache):
    try:
        DIRECT_CACHE_PATH.write_text(json.dumps(cache))
    except:
        pass

def normalize_nellis_url(url):
    u = str(url or "").strip()
    if not u:
        return ""
    if u.startswith("/"):
        u = "https://www.nellisauction.com" + u
    return u.split("#")[0]

def extract_nellis_listing_links(html):
    """Best-effort extraction of public Nellis listing links from a results/search page."""
    if not html:
        return []
    links = set()

    # href links
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        href = m.group(1)
        if "/p/" in href:
            links.add(normalize_nellis_url(href))

    # JSON-escaped URLs / paths
    for m in re.finditer(r'["\'](\/p\/[^"\']+)["\']', html, re.I):
        links.add(normalize_nellis_url(m.group(1)))

    # Canonical item patterns already used by FlipScout
    for m in re.finditer(r'https://www\.nellisauction\.com/p/[^\s"\'<>]+', html, re.I):
        links.add(normalize_nellis_url(m.group(0)))

    return sorted(x for x in links if x.startswith("https://www.nellisauction.com/p/"))

def parse_nellis_listing_basic(url, timeout=20):
    cache = load_direct_cache()
    key = normalize_nellis_url(url)

    # v3.0.1: NEVER trust legacy cached location data.
    # Only reuse entries created by this schema with a verified pickup field.
    cached = cache.get(key)
    if isinstance(cached, dict):
        if (
            cached.get("_cacheSchema") == DIRECT_CACHE_SCHEMA
            and cached.get("pickupVerified") is True
            and str(cached.get("pickupCity","")).strip()
            and str(cached.get("pickupState","")).strip()
        ):
            return cached

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    out = {
        "itemUrl": key, "title": "", "currentBid": None, "retailPrice": None,
        "condition": "", "category": "", "brand": "", "city": "", "state": "",
        "pickupAddress": "", "pickupCity": "", "pickupState": "", "pickupZip": "",
        "lotId": "", "inventoryNumber": "", "buyersPremiumPct": None,
        "qualityFlags": "", "bidSource": "Direct Nellis", "source": "Direct Nellis",
        "pickupVerified": False, "auctionStatus": "", "_cacheSchema": DIRECT_CACHE_SCHEMA
    }
    try:
        r = requests.get(key, headers=headers, timeout=timeout)
        if not r.ok:
            out["directStatus"] = f"HTTP {r.status_code}"
            return out

        html = r.text
        txt = unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)))

        # Current-vs-ended auction status.
        lowtxt = txt.lower()
        if re.search(r'\b(?:ended|won for|auction ended)\b', lowtxt):
            out["auctionStatus"] = "ended"
        elif re.search(r'\b(?:current price|current bid|time left|ends)\b', lowtxt):
            out["auctionStatus"] = "active"
        else:
            out["auctionStatus"] = "unknown"

        # Title
        title_patterns = [
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            r'<title>(.*?)</title>',
            r'"title"\s*:\s*"([^"]+)"'
        ]
        for pat in title_patterns:
            m = re.search(pat, html, re.I | re.S)
            if m:
                t = unescape(m.group(1)).strip()
                t = re.sub(r"\s*\|\s*Nellis.*$", "", t, flags=re.I)
                out["title"] = t
                break

        # Bid
        bid, bid_count, note = extract_live_bid_from_html(html)
        if bid is not None:
            out["currentBid"] = bid
        out["bidCount"] = bid_count if bid_count is not None else ""
        out["bidFetchNote"] = note

        # Retail/MSRP
        retail_patterns = [
            r'(?i)"retailPrice"\s*:\s*"?\$?([0-9][0-9,]*(?:\.[0-9]+)?)"?',
            r'(?i)(?:retail|msrp|estimated retail(?: price)?)\s*[:$ ]{0,12}\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)'
        ]
        for pat in retail_patterns:
            m = re.search(pat, html) or re.search(pat, txt)
            if m:
                try:
                    out["retailPrice"] = float(m.group(1).replace(",", ""))
                    break
                except:
                    pass

        # Condition
        cm = re.search(r'(?i)"condition"\s*:\s*"([^"]+)"', html)
        if not cm:
            cm = re.search(r'(?i)\bcondition\s*[:\-]\s*([A-Za-z ]{2,30})', txt)
        if cm:
            out["condition"] = cm.group(1).strip()

        # Brand
        bm = re.search(r'(?i)"brand"\s*:\s*(?:\{[^{}]*?"name"\s*:\s*)?"([^"]+)"', html)
        if bm:
            out["brand"] = bm.group(1).strip()

        # ACTUAL pickup location from listing page.
        pickup_patterns = [
            r'(?i)Pickup\s*Location.{0,160}?([0-9]{1,6}\s+[^,<]{3,80})\s+([A-Za-z .]+),\s*([A-Z]{2}),\s*(\d{5})',
            r'(?i)"pickupLocation"\s*:\s*"([^"]+)"',
            r'(?i)"address"\s*:\s*"([^"]+)"'
        ]
        pm = re.search(pickup_patterns[0], txt)
        if pm:
            out["pickupAddress"] = pm.group(1).strip()
            out["pickupCity"] = pm.group(2).strip()
            out["pickupState"] = pm.group(3).strip()
            out["pickupZip"] = pm.group(4).strip()
        else:
            # Try JSON-ish fields.
            raw_pick = ""
            for pat in pickup_patterns[1:]:
                m = re.search(pat, html)
                if m:
                    raw_pick = unescape(m.group(1)).strip()
                    break
            if raw_pick:
                out["pickupAddress"] = raw_pick
                m = re.search(r'([A-Za-z .]+),\s*([A-Z]{2})\s*(\d{5})', raw_pick)
                if m:
                    out["pickupCity"] = m.group(1).strip()
                    out["pickupState"] = m.group(2).strip()
                    out["pickupZip"] = m.group(3).strip()

        # v3.0.1: do NOT use shoppingLocation / search location as proof of pickup.
        # If Pickup Location cannot be parsed from the item page, leave it unknown.
        if out["pickupCity"] and out["pickupState"]:
            out["pickupVerified"] = True

        # Canonical city/state come ONLY from verified actual pickup location.
        if out["pickupVerified"]:
            out["city"] = out["pickupCity"]
            out["state"] = out["pickupState"]
        else:
            out["city"] = ""
            out["state"] = ""

        # Inventory number
        inv = re.search(r'(?i)(?:Inventory Number|inventoryNumber)\s*[:#]?\s*"?(\d{6,})"?', txt + " " + html)
        if inv:
            out["inventoryNumber"] = inv.group(1)

        # Buyer's premium
        prem = re.search(r'(?i)Buyers?\s*Premium.{0,30}?([0-9]+(?:\.[0-9]+)?)\s*%', txt)
        if prem:
            out["buyersPremiumPct"] = float(prem.group(1))

        # Quality/risk flags from visible listing page.
        flags = []
        quality_terms = [
            "Used","Untested","No Damage","No Assembly Needed","In Package",
            "Unknown if Missing Parts","Not Functional","Major Damage",
            "Not In Package","Missing Parts","Damaged","Broken","Cracked"
        ]
        low = txt.lower()
        for term in quality_terms:
            if term.lower() in low:
                flags.append(term)
        out["qualityFlags"] = " | ".join(dict.fromkeys(flags))

        # lot id
        lid = re.search(r'(?i)"lotId"\s*:\s*"?(\d+)"?', html)
        if not lid:
            lid = re.search(r'/(\d+)(?:\?|$)', key)
        if lid:
            out["lotId"] = lid.group(1)

        out["directStatus"] = "OK"
        cache[key] = out
        save_direct_cache(cache)
        return out
    except Exception as e:
        out["directStatus"] = "ERROR: " + str(e)[:100]
        return out

def infer_nellis_search_location(page_url):
    """Infer requested Nellis location from common search URL parameters."""
    try:
        u = urllib.parse.unquote_plus(str(page_url or ""))
        m = re.search(r'(?i)(?:Location(?:\+|%20|\s)*Name|LocationName|location)=([^&#]+)', u)
        if m:
            city = urllib.parse.unquote_plus(m.group(1)).strip()
            city = re.split(r'[,&]', city)[0].strip()
            if city:
                return city
        for city in ["Mesa","Phoenix","Las Vegas","Houston","Dallas"]:
            if re.search(rf'(?i)\b{re.escape(city)}\b', u):
                return city
    except Exception:
        pass
    return ""

def direct_discover_nellis(page_url, max_items=25):
    """Discover public listing links and verify each item's actual pickup location."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = requests.get(str(page_url).strip(), headers=headers, timeout=25)
    r.raise_for_status()
    links = extract_nellis_listing_links(r.text)[:int(max_items)]
    rows = [parse_nellis_listing_basic(u) for u in links]
    df = pd.DataFrame(rows)
    if not df.empty:
        df["requestedSearchCity"] = infer_nellis_search_location(page_url)
        df["locationSource"] = np.where(
            df.get("pickupVerified", pd.Series(False, index=df.index)).fillna(False).astype(bool),
            "Verified item-page Pickup Location",
            "UNVERIFIED — excluded"
        )
    return df
# ---------- v2.6 Multi-Market Comp Support ----------
def market_search_urls(query, location="Phoenix, AZ"):
    """Build user-openable marketplace searches. These are supporting asking-price sources, not sold-proof."""
    q = urllib.parse.quote_plus(str(query or "").strip())
    loc = urllib.parse.quote_plus(str(location or "").strip())
    return {
        "eBay active": f"https://www.ebay.com/sch/i.html?_nkw={q}",
        "Facebook Marketplace": f"https://www.facebook.com/marketplace/search/?query={q}",
        "OfferUp": f"https://offerup.com/search?q={q}",
        "Mercari": f"https://www.mercari.com/search/?keyword={q}",
        "Google Shopping": f"https://www.google.com/search?tbm=shop&q={q}",
    }


def preliminary_candidate_score(row):
    """Free/local ranking before any paid comp research. Higher = worth researching first."""
    bid = clean_num(row.get("currentBid", 0))
    retail = clean_num(row.get("retailPrice", 0))
    title = str(row.get("title", "") or "").lower()
    score = 0.0
    if retail > 0:
        score += min(45.0, max(0.0, (1.0 - bid / retail) * 45.0))
    if any(w in title for w in HIGH_DEMAND_WORDS):
        score += 25.0
    if bid > 0:
        score += 10.0
    if retail >= 100:
        score += 10.0
    if any(w in title for w in ["parts only","upper unit only","damaged","broken","incomplete"]):
        score -= 40.0
    return round(score, 2)

def median_positive(values):
    vals=[]
    for v in values:
        try:
            x=float(v)
            if x > 0:
                vals.append(x)
        except:
            pass
    return float(np.median(vals)) if vals else None

def local_ask_quick_sale(marketplace_prices, offerup_prices, mercari_prices):
    """Conservative support estimate from ASKING prices only. Never counts as sold-proof."""
    medians=[]
    for group in [marketplace_prices, offerup_prices, mercari_prices]:
        med=median_positive(group)
        if med:
            medians.append(med)
    if not medians:
        return None
    # Asking prices tend to be optimistic; use 80% of cross-market median as support only.
    return float(np.median(medians)) * 0.80


st.set_page_config(page_title="FlipScout AI", page_icon="🔥", layout="wide", initial_sidebar_state="expanded")



st.markdown("""
<style>
.block-container{padding-top:2rem;padding-bottom:3rem;max-width:1500px}
[data-testid="stSidebar"]{border-right:1px solid rgba(255,255,255,.08)}
h1,h2,h3{letter-spacing:-.02em}
.fs-hero{padding:1.4rem 1.5rem;border:1px solid rgba(255,255,255,.09);border-radius:18px;
background:linear-gradient(135deg,rgba(255,75,75,.10),rgba(35,66,95,.18));margin-bottom:1.25rem}
.fs-title{font-size:2.45rem;font-weight:800;line-height:1.05}
.fs-sub{margin-top:.55rem;opacity:.78;font-size:1rem;max-width:900px}
.fs-step{padding:.9rem 1rem;border-radius:12px;border:1px solid rgba(255,255,255,.08);
background:rgba(255,255,255,.025);min-height:92px}
.fs-step b{font-size:1.02rem}
div[data-testid="stMetric"]{border:1px solid rgba(255,255,255,.08);padding:.75rem .9rem;border-radius:12px;background:rgba(255,255,255,.02)}
div.stButton>button{border-radius:10px;font-weight:700;min-height:44px}
div[data-testid="stDataFrame"]{border-radius:14px;overflow:hidden;border:1px solid rgba(255,255,255,.08)}
.fs-footer{opacity:.58;font-size:.85rem;text-align:center;padding-top:.4rem}
</style>
""", unsafe_allow_html=True)





# ---------- helpers ----------
CATEGORY_RESALE = {
    "Electronics": 0.55,
    "Home Improvement": 0.60,
    "Tools": 0.65,
    "Furniture & Appliances": 0.48,
    "Furniture": 0.45,
    "Appliances": 0.52,
    "Outdoors & Sports": 0.52,
    "Patio & Garden": 0.48,
    "Home & Household Essentials": 0.40,
    "Baby": 0.42,
    "Pool": 0.45,
    "Rugs": 0.35,
    "Monitors": 0.55,
}
CONDITION_MULT = {
    "new": 1.00,
    "open box": 0.92,
    "like new": 0.90,
    "used": 0.76,
    "salvage": 0.35,
    "unknown": 0.60,
}
HIGH_DEMAND_WORDS = [
    "milwaukee","dewalt","makita","ryobi","apple","iphone","ipad","macbook",
    "dyson","shark","sony","samsung","lg","bose","jbl","nintendo","playstation",
    "xbox","traeger","weber","blackstone","peloton","keurig","ninja","vitamix",
    "kitchenaid","roomba","ecovacs","craftsman","husky","ego","stihl"
]
RISK_WORDS = [
    "untested","does not power","not working","for parts","salvage","partial",
    "missing","incomplete","as-is","as is","broken","damaged","no returns",
    "upper unit only","unit only","unknown functionality"
]

def clean_num(v):
    try:
        if pd.isna(v): return 0.0
        if isinstance(v, str):
            v = v.replace("$","").replace(",","").strip()
        return float(v)
    except:
        return 0.0

def normalize_condition(v):
    s = str(v or "").strip().lower()
    for k in CONDITION_MULT:
        if k in s:
            return k
    return "unknown"

def estimated_resale(row):
    # If actual comps were entered, use 90% of the median comp as a conservative quick-sale estimate.
    comps = []
    for c in ["comp1","comp2","comp3"]:
        v = clean_num(row.get(c, 0))
        if v > 0:
            comps.append(v)
    if comps:
        return max(0, float(np.median(comps)) * 0.90)

    retail = clean_num(row.get("retailPrice", 0))
    cat = str(row.get("category", "") or "")
    base = CATEGORY_RESALE.get(cat, 0.45)
    cond = CONDITION_MULT.get(normalize_condition(row.get("condition","")), 0.60)
    title = str(row.get("title","") or "").lower()
    brand_boost = 1.08 if any(w in title for w in HIGH_DEMAND_WORDS) else 1.0
    risk_cut = 0.72 if any(w in title for w in RISK_WORDS) else 1.0
    return max(0, retail * base * cond * brand_boost * risk_cut)

def calc_row(row, premium, tax, selling_fee, min_profit, min_roi):
    bid = clean_num(row.get("currentBid", 0))
    retail = clean_num(row.get("retailPrice", 0))
    resale_override = clean_num(row.get("resaleOverride", 0))
    resale = resale_override if resale_override > 0 else estimated_resale(row)

    acquisition = bid * (1 + premium) * (1 + tax)
    sale_net_before_cost = resale * (1 - selling_fee)
    profit = sale_net_before_cost - acquisition
    roi = (profit / acquisition * 100) if acquisition > 0 else 999

    # Max bid must satisfy BOTH desired min profit and desired min ROI.
    max_acq_profit = max(0, sale_net_before_cost - min_profit)
    max_acq_roi = max(0, sale_net_before_cost / (1 + min_roi))
    max_acq = min(max_acq_profit, max_acq_roi)
    denom = (1 + premium) * (1 + tax)
    max_bid = max_acq / denom if denom else 0

    title = str(row.get("title","") or "")
    tl = title.lower()
    risk_hits = sum(1 for w in RISK_WORDS if w in tl)
    demand_hits = sum(1 for w in HIGH_DEMAND_WORDS if w in tl)

    # score: margin + ROI + bid discount + demand - risk
    margin_score = np.clip(profit / max(min_profit, 1) * 28, 0, 35)
    roi_score = np.clip(roi / max(min_roi*100, 1) * 25, 0, 30)
    discount_score = np.clip((1 - bid / retail) * 20 if retail > 0 else 0, 0, 20)
    demand_score = min(15, demand_hits * 8)
    risk_penalty = min(45, risk_hits * 18)
    score = float(np.clip(margin_score + roi_score + discount_score + demand_score - risk_penalty, 0, 100))

    data_problem = bid <= 0 or retail <= 0
    if data_problem:
        verdict = "DATA MISSING"
    elif profit >= min_profit and roi >= min_roi*100 and bid <= max_bid and risk_hits == 0:
        verdict = "BUY"
    elif profit > 0 and bid <= max_bid * 1.15 and risk_hits <= 1:
        verdict = "WATCH"
    else:
        verdict = "SKIP"

    confidence = 75
    if retail <= 0: confidence -= 25
    if bid <= 0: confidence -= 35
    if demand_hits: confidence += 10
    if risk_hits: confidence -= 25
    if normalize_condition(row.get("condition","")) in ("new","open box","like new"): confidence += 8
    confidence = int(np.clip(confidence, 25, 95))

    return pd.Series({
        "Verdict": verdict,
        "Deal Score": round(score),
        "Current Bid": round(bid,2),
        "Retail/MSRP": round(retail,2),
        "Est. Quick Sale": round(resale,2),
        "Est. Total Cost": round(acquisition,2),
        "Projected Profit": round(profit,2),
        "Projected ROI %": round(roi,1),
        "MAX BID": round(max_bid,2),
        "Confidence %": confidence,
        "Risk Flags": risk_hits,
        "Data Status": "CHECK BID FIELD" if bid <= 0 else "OK",
    })


def extract_live_bid_from_html(html):
    """
    Best-effort parser for Nellis server-rendered pages.
    Tries structured JSON first, then visible text patterns.
    Returns (bid, bid_count, source_note).
    """
    if not html:
        return None, None, "empty response"

    text = unescape(html)

    # 1) Structured JSON/script payloads
    bid_patterns = [
        r'"currentBid"\s*:\s*\{[^{}]*?"amount"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
        r'"currentBid"\s*:\s*"?\$?([0-9]+(?:\.[0-9]+)?)"?',
        r'"highBid"\s*:\s*"?\$?([0-9]+(?:\.[0-9]+)?)"?',
        r'"winningBid"\s*:\s*"?\$?([0-9]+(?:\.[0-9]+)?)"?',
        r'"price"\s*:\s*"?\$?([0-9]+(?:\.[0-9]+)?)"?',
    ]
    count_patterns = [
        r'"bidCount"\s*:\s*"?([0-9]+)"?',
        r'"totalBids"\s*:\s*"?([0-9]+)"?',
    ]

    bid = None
    for pat in bid_patterns:
        m = re.search(pat, text, flags=re.I|re.S)
        if m:
            try:
                bid = float(m.group(1))
                if bid >= 0:
                    break
            except:
                pass

    bid_count = None
    for pat in count_patterns:
        m = re.search(pat, text, flags=re.I|re.S)
        if m:
            try:
                bid_count = int(m.group(1))
                break
            except:
                pass

    if bid is not None:
        return bid, bid_count, "Nellis page JSON"

    # 2) Visible HTML/text patterns
    visible_patterns = [
        r'Current\s+Bid.{0,120}?\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)',
        r'High\s+Bid.{0,120}?\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)',
        r'Bid\s+Price.{0,120}?\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)',
        r'Currently\s+at.{0,80}?\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)',
    ]
    for pat in visible_patterns:
        m = re.search(pat, text, flags=re.I|re.S)
        if m:
            try:
                return float(m.group(1).replace(",","")), bid_count, "Nellis visible page"
            except:
                pass

    return None, bid_count, "bid not found"

def fetch_nellis_live_bid(item_url, lot_id=None, timeout=15):
    """
    Fetch a public Nellis listing directly from the user's computer.
    Does not log in or place bids.
    """
    urls = []
    if item_url and str(item_url).startswith("http"):
        urls.append(str(item_url))
    if lot_id:
        canonical = f"https://www.nellisauction.com/p/item/{str(lot_id).strip()}"
        if canonical not in urls:
            urls.append(canonical)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    last_note = "no URL"
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                bid, count, note = extract_live_bid_from_html(r.text)
                if bid is not None:
                    return bid, count, note
                last_note = note
            else:
                last_note = f"HTTP {r.status_code}"
        except Exception as e:
            last_note = str(e)[:80]

    return None, None, last_note


def comp_relevance(query, title, evidence_type="Exact model"):
    """Conservative title relevance screen for resale comps."""
    q = str(query or "").lower().strip()
    t = str(title or "").lower().strip()
    if not q or not t:
        return 0, "empty"

    qtokens = [x for x in re.findall(r"[a-z0-9]+", q) if len(x) > 1]
    ttokens = set(re.findall(r"[a-z0-9]+", t))
    models = [x for x in qtokens if any(c.isdigit() for c in x) and any(c.isalpha() for c in x)]

    # Exact model identifiers are hard gates.
    if evidence_type == "Exact model" and models:
        missing = [m for m in models if m not in ttokens and m not in t.replace("-","")]
        if missing:
            return 0, "model mismatch"

    overlap = sum(1 for x in qtokens if x in ttokens)
    ratio = overlap / max(1, len(qtokens))
    score = int(round(ratio * 100))

    # Penalize obvious accessories/parts unless query itself asks for them.
    accessory_words = {
        "replacement","switch","clamp","blade","wheel","battery","charger","attachment",
        "part","parts","repair","belt","guard","handle","case","insert","cutterhead","trigger"
    }
    q_has_accessory = any(w in qtokens for w in accessory_words)
    t_accessory = any(w in ttokens for w in accessory_words)
    if t_accessory and not q_has_accessory:
        score -= 45

    if "parts" in ttokens and "parts" not in qtokens:
        score -= 25

    return max(0, min(100, score)), "ok"


def extract_model_token(title):
    """Find a likely manufacturer model/SKU while rejecting sizes, voltages and quantities."""
    text=str(title or "")
    raw=re.findall(r"\b[A-Za-z0-9][A-Za-z0-9\-\.]{2,20}\b",text)
    spec_re=re.compile(r"^(?:\d+(?:\.\d+)?(?:v|w|a|ah|amp|amps|inch|in|ft|lb|lbs|oz|gal|gallon|piece|pieces|pc|pcs)|\d+[- ]?piece|\d+in1)$",re.I)
    bad={"20V","18V","12V","120V","240V","7-PIECE","6-PIECE","5-PIECE","4-PIECE","3-PIECE","2-PIECE"}
    scored=[]
    for tok in raw:
        u=tok.upper()
        compact=re.sub(r"[^A-Z0-9]","",u)
        if u in bad or spec_re.match(u):
            continue
        if not (any(c.isalpha() for c in compact) and any(c.isdigit() for c in compact)):
            continue
        # Reject tokens dominated by a generic quantity/spec word.
        if any(x in u for x in ["PIECE","PACK"]) and not u.startswith(("DW","DC","M","SV","V","HP")):
            continue
        score=min(len(compact),12)
        prefixes=("DW","DWE","DCB","DCD","DCF","DCS","DCV","DCL","M18","M12","SV","V","HP","HD","CM","BIS","BL")
        if compact.startswith(prefixes): score+=8
        # Model-like mixed alphanumeric strings get preference.
        if re.search(r"[A-Z]{2,}\d{2,}",compact): score+=6
        scored.append((score,tok))
    return max(scored)[1] if scored else ""


KNOWN_RESALE_BRANDS = [
    "DEWALT","MILWAUKEE","MAKITA","RYOBI","RIDGID","BOSCH","HILTI",
    "DYSON","SHARK","NINJA","VITAMIX","KITCHENAID","KEURIG",
    "APPLE","SAMSUNG","SONY","LG","BOSE","JBL","NINTENDO","PLAYSTATION","XBOX",
    "TRAEGER","WEBER","BLACKSTONE","PELOTON","ECOVACS","IROBOT","BISSELL",
    "CRAFTSMAN","HUSQVARNA","EGO","GREENWORKS","KOBALT"
]

def infer_brand_from_title(title, existing_brand=""):
    """Infer only well-known resale brands from the actual listing title."""
    existing=str(existing_brand or "").strip()
    if existing:
        return existing
    title_u=str(title or "").upper()
    for brand in KNOWN_RESALE_BRANDS:
        if re.search(rf'(?<![A-Z0-9]){re.escape(brand)}(?![A-Z0-9])', title_u):
            return brand.title() if brand not in {"LG","JBL","EGO"} else brand
    return ""

def safe_title_model(title, brand=""):
    """Accept a model from the title only when it looks manufacturer-specific."""
    candidate=extract_model_token(title)
    if not candidate:
        return ""
    c=str(candidate).strip()
    compact=re.sub(r"[^A-Za-z0-9]","",c).upper()
    # Reject generic voltage/spec/quantity tokens.
    if compact in {"M18","M12","20VMAX","18VMAX","12VMAX","40V","60V","80V"}:
        return ""
    if re.fullmatch(r'\d+(?:V|W|A|AH|IN|MM|CM|PC|PCS|LB|LBS)',compact,re.I):
        return ""
    # Known brand + model-like token is enough title context; otherwise stay family-only.
    if brand and (re.search(r'[A-Za-z]',compact) and re.search(r'\d',compact)):
        return c
    return ""

def make_comp_query(row):
    """Build the best resale search query without falsely promoting family terms to exact model."""
    title=str(row.get("title","") or "").strip()
    brand=infer_brand_from_title(title, row.get("brand",""))
    category=str(row.get("category","") or "").strip()
    model=safe_title_model(title, brand)

    if model:
        q=" ".join(x for x in [brand,model] if x).strip()
        return q,True,model

    noise={
        "new","used","updated","compatible","with","for","and","the","a","an","tool",
        "cordless","battery","batteries","pack","set","kit","inch","in","of","to","by",
        "bare","only","replacement","premium","generic","open","box","like","heavy","duty"
    }
    words=[w for w in re.findall(r"[A-Za-z0-9]+",title)
           if w.lower() not in noise and not w.isdigit()]
    if brand:
        words=[w for w in words if w.lower()!=brand.lower()]

    distinctive=[]
    for w in words:
        if len(w)<3: continue
        if re.fullmatch(r"\d+(?:pc|pcs|in|inch|ft|v|w|oz|lb|lbs)",w,re.I): continue
        if w.lower() not in [x.lower() for x in distinctive]:
            distinctive.append(w)
        if len(distinctive)>=4: break

    q=" ".join(([brand] if brand else [])+distinctive).strip()
    return q,False,""

def fetch_ebay_sold_comps(token, query, limit=25, days_back=90, condition="any"):
    """Call the eBay sold-listings actor and return a DataFrame."""
    actor = "caffein.dev~ebay-sold-listings"
    payload = {
        "keywords": [query.strip()],
        "daysToScrape": int(days_back),
        "count": int(limit),
        "categoryId": "0",
        "ebaySite": "ebay.com",
        "sortOrder": "endedRecently",
        "itemLocation": "default",
        "itemCondition": condition,
        "includeCompletedListings": True,
    }
    rr = requests.post(url, params={"token": token}, json=payload, timeout=240)
    if rr.status_code >= 400:
        msg = rr.text[:500]
        if rr.status_code in (402, 403) or any(x in msg.lower() for x in ["usage", "credit", "billing", "limit", "subscription"]):
            pass
    data = rr.json()
    return pd.DataFrame(data if isinstance(data, list) else [])

def relevant_comp_prices(cdf, query, exact_model):
    """Return filtered relevant prices + diagnostics."""
    if cdf is None or cdf.empty:
        return [], 0, 0, "INSUFFICIENT"

    if "title" not in cdf.columns:
        cdf = cdf.copy()
        cdf["title"] = ""

    evidence = "Exact model" if exact_model else "Related / broader search"
    rel = cdf["title"].apply(lambda x: comp_relevance(query, x, evidence))
    work = cdf.copy()
    work["Relevance %"] = rel.apply(lambda x: x[0])
    work["Relevance Note"] = rel.apply(lambda x: x[1])

    threshold = 70 if exact_model else 55
    relevant = work[work["Relevance %"] >= threshold].copy()
    rejected = len(work) - len(relevant)

    price_col = "soldPrice" if "soldPrice" in relevant.columns else (
        "totalPrice" if "totalPrice" in relevant.columns else None
    )
    if not price_col:
        return [], len(relevant), rejected, "INSUFFICIENT"

    prices = pd.to_numeric(relevant[price_col], errors="coerce").dropna()
    prices = prices[prices > 0]
    if not len(prices):
        return [], len(relevant), rejected, "INSUFFICIENT"

    # IQR price outlier removal.
    if len(prices) >= 4:
        q1, q3 = prices.quantile(.25), prices.quantile(.75)
        iqr = q3 - q1
        filtered = prices[(prices >= max(0, q1 - 1.5 * iqr)) & (prices <= q3 + 1.5 * iqr)]
        if len(filtered) >= 2:
            prices = filtered

    if exact_model and len(prices) >= 5:
        confidence = "HIGH"
    elif exact_model and len(prices) >= 2:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return list(prices.astype(float)), len(relevant), rejected, confidence


def comp_condition_compatible(nellis_condition, comp_condition):
    """Keep sold evidence reasonably comparable to the Nellis item's condition."""
    n = str(nellis_condition or "").lower()
    c = str(comp_condition or "").lower()
    if not c or c in {"none", "nan", "unknown"}:
        return True
    if any(x in n for x in ["open box", "like new"]):
        return not any(x in c for x in ["parts", "repair", "damaged"])
    if "new" in n and "used" not in n:
        return any(x in c for x in ["new", "open box"])
    if "used" in n or "pre-owned" in n:
        return not any(x in c for x in ["parts", "repair", "damaged"])
    return True


def build_comp_search_plan(row, model="", brand=""):
    """Progressively broaden comp searches without relaxing exact-model verification."""
    title = str(row.get("title", "") or "").strip()
    brand = str(brand or row.get("brand", "") or "").strip()
    model = str(model or "").strip()
    product_words = [w for w in re.findall(r"[A-Za-z0-9]+", title)
                     if len(w) > 2 and w.lower() not in {
                         "new","used","open","box","like","with","for","and","the","only",
                         "cordless","battery","batteries","compatible","replacement","premium"
                     }]
    plan=[]
    if model:
        is_upc=bool(re.fullmatch(r"\d{8,14}", model))
        if is_upc:
            plan.append(("Exact UPC", model, True))
        else:
            plan.append(("Exact model", " ".join(x for x in [brand, model] if x).strip(), True))
        descriptors=[]
        for w in product_words:
            if w.lower() != brand.lower() and model.lower() not in w.lower():
                descriptors.append(w)
            if len(descriptors) >= 3: break
        if not is_upc:
            if descriptors:
                plan.append(("Model + product", " ".join(x for x in [brand, model] + descriptors if x).strip(), True))
            plan.append(("Model only", model, True))
    else:
        q, _, _ = make_comp_query(row)
        if q: plan.append(("Product family", q, False))
        if brand and product_words:
            plan.append(("Brand + attributes", " ".join([brand] + product_words[:4]), False))
    # de-duplicate while preserving order
    seen=set(); out=[]
    for stage,q,exact in plan:
        key=q.lower().strip()
        if key and key not in seen:
            seen.add(key); out.append((stage,q,exact))
    return out


def research_comp_plan(token, row, model, brand, limit=25, days_back=90, cache=None):
    """Run staged sold-comp research and return accepted evidence plus diagnostics."""
    cache = cache if cache is not None else {}
    accepted_frames=[]; support_frames=[]; all_frames=[]; stages=[]
    ncond=str(row.get("condition","") or "")
    for stage, query, exact in build_comp_search_plan(row, model, brand):
        key=f"v23|{query.lower().strip()}|{limit}|{days_back}|any"
        cdf=cache.get(key)
        if cdf is None:
            cdf=fetch_ebay_sold_comps(token, query, limit=limit, days_back=days_back, condition="any")
            cache[key]=cdf
        if cdf is None or cdf.empty:
            stages.append(f"{stage}: 0 returned")
            continue
        work=cdf.copy()
        if "title" not in work.columns: work["title"]=""
        if exact and re.fullmatch(r"\d{8,14}", query):
            # UPC searches are exact only when the sold record itself echoes the same UPC/GTIN/EAN.
            id_cols=[c for c in work.columns if str(c).lower() in {"upc","gtin","ean","productid","product_id"}]
            def _upc_rel(r):
                vals=" ".join(str(r.get(c,"")) for c in id_cols)
                return (100,"UPC match") if query in re.sub(r"\D","",vals) else (0,"UPC not confirmed in sold record")
            rel=work.apply(_upc_rel,axis=1)
        else:
            rel=work["title"].apply(lambda x: comp_relevance(query, x, "Exact model" if exact else "Related / broader search"))
        work["Relevance %"]=rel.apply(lambda x:x[0]); work["Relevance Note"]=rel.apply(lambda x:x[1])
        work["Search Stage"]=stage; work["Search Query"]=query
        threshold=70 if exact else 55
        ok=work["Relevance %"]>=threshold
        if "condition" in work.columns:
            ok &= work["condition"].apply(lambda c: comp_condition_compatible(ncond,c))
        matched=work[ok].copy()
        all_frames.append(work)
        if exact and not matched.empty:
            accepted_frames.append(matched)
        elif (not exact) and not matched.empty:
            support_frames.append(matched)
        stages.append(f"{stage}: {len(matched)} relevant")
        # Two exact-model comps are enough to verify; don't spend more actor calls.
        if exact and sum(len(x) for x in accepted_frames) >= 2:
            break
    accepted=pd.concat(accepted_frames, ignore_index=True) if accepted_frames else pd.DataFrame()
    support=pd.concat(support_frames, ignore_index=True) if support_frames else pd.DataFrame()
    all_df=pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame()
    if not accepted.empty:
        dedupe=[c for c in ["url","itemId","title"] if c in accepted.columns]
        if dedupe: accepted=accepted.drop_duplicates(subset=[dedupe[0]])
    if not support.empty:
        dedupe=[c for c in ["url","itemId","title"] if c in support.columns]
        if dedupe: support=support.drop_duplicates(subset=[dedupe[0]])
    return accepted, support, all_df, stages, cache


def deep_scan_nellis_identity(item_url, fallback_title=""):
    """Deep-inspect a public Nellis lot page for model/MPN/SKU/UPC and listing risk."""
    result={
        "deepModel":"","deepUPC":"","deepBrand":"","deepSKU":"",
        "deepRisk":"","deepText":"","deepTitle":"","deepDescription":"","deepAttributes":"","identitySource":""
    }
    if not item_url:
        return result
    try:
        r=requests.get(
            str(item_url),
            headers={
                "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
                "Accept":"text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            },
            timeout=25
        )
        if not r.ok:
            return result

        raw=unescape(r.text)
        visible=re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",raw))
        result["deepText"]=visible[:20000]

        # Search both visible text and embedded page-state/JSON because auction sites
        # often hide product attributes from the rendered text.
        search_blob=re.sub(r'\\u0022','"',raw)
        search_blob=re.sub(r'\\u003[aA]','>',search_blob)
        search_blob=re.sub(r'\\u003[cC]','<',search_blob)
        search_blob=unescape(search_blob)

        # v2.2: extract richer product identity from structured page data before guessing from text.
        def _first(patterns, blob=search_blob):
            for _pat in patterns:
                _m=re.search(_pat, blob, re.I|re.S)
                if _m:
                    return re.sub(r"\s+"," ",unescape(_m.group(1))).strip(" \t\r\n\"'.,:;")
            return ""

        result["deepTitle"]=_first([
            r'"(?:productName|itemName|name|title)"\s*:\s*"([^"\n]{4,240})"',
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)'
        ])
        result["deepDescription"]=_first([
            r'"(?:description|productDescription|shortDescription)"\s*:\s*"([^"\n]{8,1500})"',
            r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']+)'
        ])
        # Keep a compact diagnostic string of labeled identifiers/specs found on the lot page.
        _attrs=[]
        for _label in ["model","modelNumber","mpn","manufacturerPartNumber","sku","productSku","upc","gtin","ean"]:
            _m=re.search(r'"'+re.escape(_label)+r'"\s*:\s*"?([^",}]{2,60})', search_blob, re.I)
            if _m: _attrs.append(f"{_label}={_m.group(1).strip()}")
        result["deepAttributes"]=" | ".join(dict.fromkeys(_attrs))[:1000]

        labeled_model_patterns=[
            r'(?i)"(?:model|modelNumber|model_number|mpn|manufacturerPartNumber|manufacturer_part_number)"\s*:\s*"([^"]{2,40})"',
            r'(?i)(?:model(?:\s*(?:number|no\.?|#))?|mfr\.?\s*model|manufacturer\s*part\s*(?:number|no\.?|#)|mpn)\s*[:#\-]?\s*([A-Z0-9][A-Z0-9\-\.\/]{2,30})'
        ]
        for pat in labeled_model_patterns:
            m=re.search(pat,search_blob)
            if m:
                cand=re.sub(r'\s+',' ',m.group(1)).strip(" .,:;")
                token=extract_model_token(cand)
                if token:
                    result["deepModel"]=token
                    result["identitySource"]="Nellis model/MPN field"
                    break

        # Explicit SKU can be useful even if it is not accepted as a manufacturer model.
        sm=re.search(r'(?i)"(?:sku|productSku|product_sku)"\s*:\s*"([^"]{2,40})"',search_blob)
        if not sm:
            sm=re.search(r'(?i)\bSKU\s*[:#\-]?\s*([A-Z0-9][A-Z0-9\-\.]{2,30})',visible)
        if sm:
            result["deepSKU"]=sm.group(1).strip()

        # UPC / GTIN / EAN.
        upc_patterns=[
            r'(?i)"(?:upc|gtin|gtin12|gtin13|ean)"\s*:\s*"?(\\d{8,14})"?',
            r'(?i)\b(?:upc|gtin|ean)\s*[:#\-]?\s*(\d{8,14})'
        ]
        for pat in upc_patterns:
            um=re.search(pat,search_blob)
            if um:
                result["deepUPC"]=um.group(1)
                if not result["identitySource"]:
                    result["identitySource"]="Nellis UPC/GTIN"
                break

        # Brand from structured data/page state.
        bm=re.search(r'(?i)"brand"\s*:\s*(?:\{\s*"[^"]+"\s*:\s*)?"([^"]{2,50})"',search_blob)
        if bm:
            result["deepBrand"]=bm.group(1).strip()

        # Last conservative model attempt: inspect richer page text around product identity words.
        if not result["deepModel"]:
            context_patterns=[
                r'(?i)(?:description|specifications|details|product information).{0,1200}',
                r'(?i)(?:model|mpn|manufacturer part).{0,250}'
            ]
            contexts=[]
            for pat in context_patterns:
                contexts += re.findall(pat,visible)
            contexts.extend([result.get("deepTitle",""), result.get("deepDescription","")])
            contexts.append(str(fallback_title or ""))
            for context in contexts:
                token=extract_model_token(context)
                if token:
                    result["deepModel"]=token
                    result["identitySource"]="Nellis listing details"
                    break

        risks=[]
        low=visible.lower()
        risk_terms=[
            "untested","missing","damaged","parts only","incomplete","upper unit only",
            "final sale","nonrefundable","does not power on","broken","cracked",
            "missing pieces","missing parts","for parts"
        ]
        for w in risk_terms:
            if w in low:
                risks.append(w)
        result["deepRisk"]=", ".join(sorted(set(risks)))

    except Exception as e:
        result["identitySource"]="Deep scan error: "+str(e)[:60]
    return result


# ---------- v2.0 Trusted Product Identity ----------
def trusted_product_model(candidate, context="", brand=""):
    c=str(candidate or "").strip()
    ctx=str(context or "")
    b=str(brand or "").strip()
    if not c: return ""

    # Website/frontend artifacts can never be product models.
    if re.search(r'(?i)\.(?:js|css|html?|json|map|svg|png|jpe?g|webp)(?:\?|$)',c): return ""
    if re.search(r'(?i)https?://|webpack|chunk|bundle|runtime|static|assets?',c): return ""
    if re.match(r'(?i)^v\d+[-_][A-Za-z0-9_-]{5,}$',c): return ""
    if len(c)>20: return ""

    compact=re.sub(r'[^A-Za-z0-9]','',c)
    if compact.upper() in {"M18","M12","20VMAX","18VMAX","12VMAX"}: return ""
    if re.fullmatch(r'\d+(?:V|W|A|AH|IN|MM|CM|PC|PCS)',compact,re.I): return ""

    numeric_hyphen=bool(re.fullmatch(r'\d{3,6}-\d{2,6}',c))
    alpha_num=bool(re.search(r'[A-Za-z]',compact) and re.search(r'\d',compact))
    if not (numeric_hyphen or alpha_num): return ""

    # Candidate needs product context, not merely occurrence in raw site code.
    labeled=bool(re.search(r'(?i)\b(model|model number|model no|mpn|manufacturer part|sku)\b',ctx))
    branded=bool(b and re.search(re.escape(b),ctx,re.I))
    if not (labeled or branded): return ""
    return c

def standardize(df):
    aliases = {
        "lotId": ["lotId","lot_id","id"],
        "title": ["title","name","item","description"],
        "currentBid": ["currentBid","current_bid","current_bid_amount","currentBidAmount","bid","bidAmount","currentPrice","current_price","price","current bid","current bid amount"],
        "bidCount": ["bidCount","bid_count","bids"],
        "retailPrice": ["retailPrice","retail_price","retail","msrp","retailValue","retail_value","estimatedRetailPrice","estimated retail price"],
        "condition": ["condition","quality"],
        "category": ["category","found in","taxonomyLevel1"],
        "city": ["city"],
        "state": ["state"],
        "shoppingLocation": ["shoppingLocation","shopping_location"],
        "locationName": ["locationName","location_name"],
        "endDate": ["endDate","end_date","closeDate","close_date","ending","end time"],
        "itemUrl": ["itemUrl","item_url","url","link"],
        "brand": ["brand"],
        "pickupAddress": ["pickupAddress","pickup_address"],
        "pickupCity": ["pickupCity","pickup_city"],
        "pickupState": ["pickupState","pickup_state"],
        "pickupZip": ["pickupZip","pickup_zip"],
        "inventoryNumber": ["inventoryNumber","inventory_number"],
        "buyersPremiumPct": ["buyersPremiumPct","buyers_premium_pct"],
        "qualityFlags": ["qualityFlags","quality_flags"],
        "qualityStars": ["qualityStars","quality_stars","starRating","rating"],
        "auctionStatus": ["auctionStatus","auction_status"],
    }
    cols_lower = {str(c).lower(): c for c in df.columns}
    out = pd.DataFrame(index=df.index)
    for target, variants in aliases.items():
        source = None
        for v in variants:
            if v in df.columns:
                source = v; break
            if v.lower() in cols_lower:
                source = cols_lower[v.lower()]; break
        out[target] = df[source] if source is not None else ""
    # v3.27: safely derive city/state from shoppingLocation.
    # Some Nellis rows have blank/malformed/non-string location values; never chain
    # .str accessors on a split component that can become NaN/float.
    def _safe_location_parts(value):
        text = "" if value is None else str(value).strip()
        if text.lower() in ("", "none", "nan"):
            return "", ""
        parts = [p.strip() for p in text.split(",") if str(p).strip()]
        city = parts[0] if len(parts) >= 1 else ""
        state = parts[1] if len(parts) >= 2 else ""
        return city, state

    loc_parts = out["shoppingLocation"].apply(_safe_location_parts)
    derived_city = loc_parts.apply(lambda x: x[0] if isinstance(x, tuple) else "")
    derived_state = loc_parts.apply(lambda x: x[1] if isinstance(x, tuple) else "")

    missing_city = out["city"].astype(str).str.strip().isin(["", "None", "nan"])
    out.loc[missing_city, "city"] = derived_city[missing_city]

    missing_state = out["state"].astype(str).str.strip().isin(["", "None", "nan"])
    out.loc[missing_state, "state"] = derived_state[missing_state]

    # Create a canonical item URL when only lotId is available.
    missing_url = out["itemUrl"].astype(str).str.strip().isin(["", "None", "nan"])
    out.loc[missing_url, "itemUrl"] = out.loc[missing_url, "lotId"].astype(str).apply(
        lambda x: f"https://www.nellisauction.com/p/item/{x}" if x and x not in ("None","nan") else ""
    )

    if "bidSource" in df.columns:
        out["bidSource"] = df["bidSource"].astype(str)
    elif "source" in df.columns:
        src=df["source"].astype(str)
        has_bid=pd.to_numeric(out["currentBid"],errors="coerce").notna()
        out["bidSource"]=np.where(has_bid,src,"")
    else:
        out["bidSource"] = np.where(pd.to_numeric(out["currentBid"], errors="coerce").notna(), "Imported", "")
    out["bidFetchNote"] = ""
    out["comp1"] = 0.0
    out["comp2"] = 0.0
    out["comp3"] = 0.0
    out["localMarketSupport"] = 0.0
    out["resaleOverride"] = 0.0
    return out





# ---------- v3.5 Arizona-First RSS Discovery ----------
def extract_rss_links(xml_text):
    """Extract actual Nellis item URLs from Bing RSS search results."""
    if not xml_text:
        return []
    found = []
    for m in re.finditer(r'<link>(.*?)</link>', xml_text, re.I | re.S):
        u = unescape(m.group(1)).strip()
        if is_real_nellis_item_url(u):
            found.append(u.split("?")[0].split("#")[0])
    # Some RSS payloads encode links in guid/url fields.
    for m in re.finditer(r'https?://(?:www\.)?nellisauction\.com/p/[^\s<>"\']+', xml_text, re.I):
        u = unescape(m.group(0)).strip().rstrip(".,);]")
        if is_real_nellis_item_url(u):
            found.append(u.split("?")[0].split("#")[0])

    out, seen = [], set()
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def discover_arizona_item_links_rss(selected_cities, depth=4):
    """
    Arizona-first discovery using Bing RSS results.
    RSS is substantially easier to parse reliably than normal search-result HTML.
    Every returned item is still verified from the actual Nellis item page.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    queries = []
    if "Mesa" in selected_cities:
        queries += [
            'site:nellisauction.com/p/ "8928 E Ray Rd" "Mesa, AZ"',
            'site:nellisauction.com/p/ "Mesa, AZ, 85212"',
            'site:nellisauction.com/p/ "Mesa, AZ" "Nellis Auction"',
        ]
    if "Phoenix" in selected_cities:
        queries += [
            'site:nellisauction.com/p/ "1402 S 40th Ave" "Phoenix, AZ"',
            'site:nellisauction.com/p/ "Phoenix, AZ, 85009"',
            'site:nellisauction.com/p/ "Phoenix, AZ" "Nellis Auction"',
        ]

    links, seen, diagnostics = [], set(), []

    for q in queries:
        for page in range(max(1, int(depth))):
            first = 1 + page * 50
            url = "https://www.bing.com/search?" + urllib.parse.urlencode({
                "q": q,
                "format": "rss",
                "count": 50,
                "first": first,
            })
            try:
                r = requests.get(url, headers=headers, timeout=25)
                diagnostics.append(f"RSS {q[:34]}... page {page+1}: HTTP {r.status_code}")
                if not r.ok:
                    continue
                for u in extract_rss_links(r.text):
                    if u not in seen:
                        seen.add(u)
                        links.append(u)
            except Exception as e:
                diagnostics.append(f"RSS {q[:34]}... page {page+1}: {str(e)[:60]}")

    return links, diagnostics


def build_arizona_first_inventory(selected_cities, target_count=25, max_checks=300, depth=4):
    """
    Primary v3.5 pipeline:
        pass
    1. Discover Arizona-targeted Nellis item URLs via RSS.
    2. Verify actual item-page pickup address.
    3. Reject ended and non-local listings.
    4. If RSS discovery is sparse, merge direct Nellis pool links as a fallback.
    """
    rss_links, diagnostics = discover_arizona_item_links_rss(selected_cities, depth=depth)

    # Fallback pool is only used to supplement RSS; actual pickup verification remains mandatory.
    fallback_links = []
    if len(rss_links) < max(10, int(target_count)):
        try:
            fallback_links, direct_notes = discover_nellis_pool_links(max_pages=min(8, int(depth)+3))
            diagnostics += ["FALLBACK DIRECT POOL"] + direct_notes
        except Exception as e:
            diagnostics.append(f"Direct fallback failed: {str(e)[:70]}")

    combined = []
    seen = set()
    for u in rss_links + fallback_links:
        if is_real_nellis_item_url(u) and u not in seen:
            seen.add(u)
            combined.append(u)

    accepted, wrong, ended, unverified = [], [], [], []
    checked = 0

    for url in combined[:int(max_checks)]:
        row = parse_nellis_listing_basic(url)
        checked += 1

        if not bool(row.get("pickupVerified", False)):
            unverified.append(row)
            continue
        if not verified_pickup_matches_selected(row, selected_cities):
            wrong.append(row)
            continue
        if not listing_is_active(row):
            ended.append(row)
            continue

        accepted.append(row)
        if len(accepted) >= int(target_count):
            break

    return {
        "accepted": accepted,
        "wrong_location": wrong,
        "ended": ended,
        "unverified": unverified,
        "checked": checked,
        "rss_discovered": len(rss_links),
        "total_discovered": len(combined),
        "diagnostics": diagnostics,
    }


# ---------- v3.3 Direct Nellis Inventory Pool ----------
def discover_nellis_pool_links(max_pages=8, per_page_hint=100):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }

    candidate_pages = [
        "https://www.nellisauction.com/search",
        "https://www.nellisauction.com/",
    ]

    for page in range(2, int(max_pages) + 1):
        candidate_pages.extend([
            f"https://www.nellisauction.com/search?page={page}",
            f"https://www.nellisauction.com/search?Page={page}",
            f"https://www.nellisauction.com/search?currentPage={page}",
        ])

    links, diagnostics = [], []
    seen = set()

    for page_url in candidate_pages:
        try:
            r = requests.get(page_url, headers=headers, timeout=25)
            diagnostics.append(f"{page_url} -> HTTP {r.status_code}")
            if not r.ok:
                continue

            found = extract_nellis_listing_links(r.text)
            valid = [u for u in found if is_real_nellis_item_url(u)]

            for u in valid:
                canonical = u.split("?")[0].split("#")[0]
                if canonical not in seen:
                    seen.add(canonical)
                    links.append(canonical)

            if len(links) >= int(per_page_hint) * 3:
                break
        except Exception as e:
            diagnostics.append(f"{page_url} -> {str(e)[:70]}")

    return links, diagnostics


def build_verified_local_inventory(selected_cities, target_count=25, max_checks=400, discovery_pages=8):
    links, diagnostics = discover_nellis_pool_links(max_pages=discovery_pages)

    accepted, wrong_location, ended, unverified = [], [], [], []
    checked = 0

    for url in links[:int(max_checks)]:
        row = parse_nellis_listing_basic(url)
        checked += 1

        if not bool(row.get("pickupVerified", False)):
            unverified.append(row)
            continue
        if not verified_pickup_matches_selected(row, selected_cities):
            wrong_location.append(row)
            continue
        if not listing_is_active(row):
            ended.append(row)
            continue

        accepted.append(row)
        if len(accepted) >= int(target_count):
            break

    return {
        "accepted": accepted,
        "wrong_location": wrong_location,
        "ended": ended,
        "unverified": unverified,
        "checked": checked,
        "discovered": len(links),
        "diagnostics": diagnostics,
    }


# ---------- v3.2 Arizona Discovery Engine ----------
def is_real_nellis_item_url(url):
    """Accept only actual Nellis product/item pages; reject search queries and search-engine text."""
    u = urllib.parse.unquote(str(url or "")).strip()

    # Strip common search-engine redirect wrappers.
    for prefix in ["url=", "u=", "q="]:
        if u.lower().startswith(prefix):
            u = u[len(prefix):]

    if not re.match(r'^https?://(?:www\.)?nellisauction\.com/p/', u, re.I):
        return False

    # Reject query/search text accidentally captured as a "URL".
    lowered = u.lower()
    if "site:nellisauction.com" in lowered:
        return False
    if re.search(r'/p/\s*["\']?mesa', lowered):
        return False
    if '"' in u or "'" in u:
        return False

    # Must contain a real path segment after /p/.
    tail = re.split(r'/p/', u, flags=re.I, maxsplit=1)[1]
    tail = tail.split("?")[0].split("#")[0].strip("/")
    if not tail:
        return False

    # Real Nellis listings generally have a slug and/or numeric identifier.
    if len(tail) < 6:
        return False
    if tail.lower().startswith(("search", "site:", "mesa", "phoenix")):
        return False

    return True


def extract_nellis_links_from_search_html(html):
    """Extract only real Nellis /p/ item URLs from public search-result HTML."""
    if not html:
        return []

    html = unescape(html)
    candidates = []

    # Normal absolute URLs.
    for m in re.finditer(r'https?://(?:www\.)?nellisauction\.com/p/[^\s"\'<>]+', html, re.I):
        candidates.append(m.group(0))

    # Percent-encoded absolute URLs.
    for m in re.finditer(r'https?%3A%2F%2F(?:www\.)?nellisauction\.com%2Fp%2F[^&"\'<> ]+', html, re.I):
        try:
            candidates.append(urllib.parse.unquote(m.group(0)))
        except Exception:
            pass

    # Search-engine redirect parameters.
    for m in re.finditer(r'[?&](?:url|u|q)=([^&"\']+)', html, re.I):
        try:
            decoded = urllib.parse.unquote_plus(m.group(1))
            # Sometimes the parameter is base64-ish/encoded junk; only keep literal Nellis URLs.
            if "nellisauction.com/p/" in decoded.lower():
                idx = decoded.lower().find("http")
                if idx >= 0:
                    decoded = decoded[idx:]
                candidates.append(decoded)
        except Exception:
            pass

    clean = []
    seen = set()

    for raw in candidates:
        u = urllib.parse.unquote(raw).strip()

        # Remove HTML/search-engine trailing characters.
        u = re.split(r'[\s"\'<>]', u)[0]
        u = u.rstrip(".,);]")

        # Strip tracking params only after validating the base URL.
        base = u.split("#")[0]
        if not is_real_nellis_item_url(base):
            continue

        # Keep query params only if they belong to the actual Nellis item URL;
        # canonicalize to the item path for cache stability.
        canonical = base.split("?")[0]

        if canonical not in seen:
            seen.add(canonical)
            clean.append(canonical)

    return clean

def web_discover_arizona_links(selected_cities, pages_per_query=3):
    """
    Free discovery via public search-engine HTML.
    Uses exact Nellis pickup-city/address phrases, then verifies every item page itself.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    queries=[]
    if "Mesa" in selected_cities:
        queries += [
            'site:nellisauction.com/p/ "Mesa, AZ" "8928 E Ray Rd"',
            'site:nellisauction.com/p/ "Mesa, AZ, 85212" Nellis Auction',
        ]
    if "Phoenix" in selected_cities:
        queries += [
            'site:nellisauction.com/p/ "Phoenix, AZ" "1402 S 40th Ave"',
            'site:nellisauction.com/p/ "Phoenix, AZ, 85009" Nellis Auction',
        ]

    links=[]
    seen=set()
    diagnostics=[]

    for q in queries:
        for page in range(max(1, int(pages_per_query))):
            # Bing public HTML: first=1,11,21...
            first = 1 + page * 10
            url = "https://www.bing.com/search?" + urllib.parse.urlencode({
                "q": q,
                "first": first,
                "count": 50,
            })
            try:
                r=requests.get(url,headers=headers,timeout=25)
                diagnostics.append(f"{q[:35]}... page {page+1}: HTTP {r.status_code}")
                if not r.ok:
                    continue
                for item_url in extract_nellis_links_from_search_html(r.text):
                    if item_url not in seen:
                        seen.add(item_url)
                        links.append(item_url)
            except Exception as e:
                diagnostics.append(f"{q[:35]}... page {page+1}: {str(e)[:50]}")

    return links, diagnostics

def listing_is_active(row):
    """Only current auctions should enter the live deal pipeline."""
    status=str(row.get("auctionStatus","") or "").lower()
    if status == "active":
        return True
    if status == "ended":
        return False
    # Conservative fallback: a current bid plus no explicit ended marker.
    return row.get("currentBid") is not None and bool(row.get("pickupVerified", False))


# ---------- v3.1 Native Arizona Location Scanner ----------
NELLIS_LOCATION_PROFILES = {
    "Mesa": {
        "state": "AZ",
        "zip": "85212",
        "address_tokens": ["8928", "ray", "mesa"],
        "label": "Mesa, AZ"
    },
    "Phoenix": {
        "state": "AZ",
        "zip": "",
        "address_tokens": ["phoenix"],
        "label": "Phoenix, AZ"
    },
}

def verified_pickup_matches_selected(row, selected_cities):
    """Require actual item-page pickup verification and selected-city match."""
    if not bool(row.get("pickupVerified", False)):
        return False
    city = str(row.get("pickupCity","") or "").strip()
    state = str(row.get("pickupState","") or "").strip().upper()
    address = str(row.get("pickupAddress","") or "").lower()
    zipc = str(row.get("pickupZip","") or "").strip()

    for selected in selected_cities:
        profile = NELLIS_LOCATION_PROFILES.get(selected)
        if not profile:
            continue
        if state != profile["state"]:
            continue

        # Strongest signal: exact city from the listing page.
        if city.lower() == selected.lower():
            return True

        # Mesa warehouse fingerprint.
        if selected == "Mesa":
            if profile["zip"] and zipc == profile["zip"]:
                return True
            if all(tok in address for tok in profile["address_tokens"] if tok):
                return True

        # Phoenix fallback: actual pickup city must contain Phoenix.
        if selected == "Phoenix" and "phoenix" in city.lower():
            return True

    return False

def scan_verified_arizona_from_links(links, selected_cities, max_items=25):
    """Fetch listing URLs directly and retain only actual Mesa/Phoenix pickup locations."""
    rows=[]
    checked=0
    for url in links:
        if checked >= int(max_items):
            break
        row=parse_nellis_listing_basic(url)
        checked += 1
        if verified_pickup_matches_selected(row, selected_cities):
            rows.append(row)
    return pd.DataFrame(rows), checked



# ---------- v3.7 Filtered Nellis Search URL Scanner ----------
def _filtered_page_variants(search_url, pages=5):
    """Preserve every Nellis filter in the pasted URL while trying common pagination keys."""
    raw = str(search_url or "").strip()
    if not raw:
        return []

    parsed = urllib.parse.urlsplit(raw)
    if "nellisauction.com" not in parsed.netloc.lower():
        return []

    base_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    variants = [raw]

    for page in range(2, max(2, int(pages) + 1)):
        for key in ("page", "Page", "currentPage"):
            pairs = [(k,v) for k,v in base_pairs if k not in ("page","Page","currentPage")]
            pairs.append((key, str(page)))
            q = urllib.parse.urlencode(pairs, doseq=True)
            variants.append(urllib.parse.urlunsplit(
                (parsed.scheme or "https", parsed.netloc, parsed.path or "/search", q, parsed.fragment)
            ))

    out=[]
    seen=set()
    for u in variants:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def infer_target_locations_from_filtered_url(search_url):
    """Detect Mesa/Phoenix from the user's pasted Nellis filter URL when possible."""
    decoded = urllib.parse.unquote_plus(str(search_url or ""))
    found=[]
    for city in ("Mesa","Phoenix"):
        if re.search(rf'(?i)\b{re.escape(city)}\b', decoded):
            found.append(city)
    return found


def scan_filtered_nellis_search(search_url, page_depth=5, max_links=250):
    """
    v3.8:
        pass
    - The pasted FILTERED Nellis results URL is the primary filter/location authority.
    - Item pages are still opened for title, current bid, condition, retail, and active status.
    - If the URL explicitly contains Mesa/Phoenix, discovered listings inherit that filtered location.
    - Item-page pickup mismatches no longer discard an otherwise valid filtered result.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }

    pages = _filtered_page_variants(search_url, pages=page_depth)
    links=[]
    seen=set()
    diagnostics=[]

    for page_url in pages:
        try:
            r=requests.get(page_url, headers=headers, timeout=25)
            diagnostics.append(f"{page_url} -> HTTP {r.status_code}")
            if not r.ok:
                continue

            for u in extract_nellis_listing_links(r.text):
                u = normalize_nellis_url(u)
                if is_real_nellis_item_url(u) and u not in seen:
                    seen.add(u)
                    links.append(u)
                    if len(links) >= int(max_links):
                        break

            if len(links) >= int(max_links):
                break

        except Exception as e:
            diagnostics.append(f"{page_url} -> {str(e)[:80]}")

    target_locations = infer_target_locations_from_filtered_url(search_url)
    target_city = target_locations[0] if len(target_locations) == 1 else ""

    accepted=[]
    ended=[]
    unverified=[]
    location_conflicts=[]
    checked=0

    for u in links:
        row=parse_nellis_listing_basic(u)
        checked += 1

        # Require an active/current auction.
        if not listing_is_active(row):
            ended.append(row)
            continue

        # If the filtered URL explicitly says Mesa or Phoenix, trust that filtered pool.
        if target_city:
            parsed_city = str(row.get("pickupCity","") or "").strip()
            parsed_state = str(row.get("pickupState","") or "").strip()

            if parsed_city and parsed_city.lower() != target_city.lower():
                conflict = dict(row)
                conflict["filteredTargetCity"] = target_city
                location_conflicts.append(conflict)

            # Preserve raw item-page pickup fields for diagnostics.
            row["itemPagePickupCity"] = parsed_city
            row["itemPagePickupState"] = parsed_state
            row["itemPagePickupAddress"] = row.get("pickupAddress","")

            # Canonical location for this filtered batch comes from the user's Nellis URL.
            row["city"] = target_city
            row["pickupCity"] = target_city
            row["state"] = "AZ"
            row["pickupState"] = "AZ"
            row["locationSource"] = "Filtered Nellis results URL"
            row["pickupVerified"] = True

            accepted.append(row)
            continue

        # If URL has no explicit Mesa/Phoenix location, retain strict item-page verification.
        if not bool(row.get("pickupVerified", False)):
            unverified.append(row)
            continue

        if verified_pickup_matches_selected(row, ["Mesa","Phoenix"]):
            accepted.append(row)
        else:
            conflict = dict(row)
            conflict["filteredTargetCity"] = ""
            location_conflicts.append(conflict)

    return {
        "accepted": accepted,
        "ended": ended,
        "unverified": unverified,
        "location_conflicts": location_conflicts,
        "links_found": len(links),
        "checked": checked,
        "target_locations": target_locations,
        "diagnostics": diagnostics,
    }



# ---------- v3.9 Browser Session Scanner ----------
def start_nellis_browser():
    """
    Start one controlled Chrome session. Nellis is opened in a TAB inside that session.
    Repeated OPEN NELLIS clicks reuse the same controlled browser instead of opening another browser window.
    """
    if webdriver is None:
        raise RuntimeError("Selenium is not installed. Run: py -m pip install selenium")

    options = ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-notifications")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=options)

    # Keep the first tab neutral, then open Nellis as a second tab.
    driver.get("about:blank")
    driver.execute_script("window.open('https://www.nellisauction.com/search','_blank');")
    handles = driver.window_handles
    if len(handles) > 1:
        driver.switch_to.window(handles[-1])

    return driver




def detect_nellis_location_from_browser(driver):
    """Detect the currently selected Nellis location from the rendered page."""
    if not scanner_session_alive(driver):
        return ""
    try:
        visible = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
    except Exception:
        visible = ""

    # Search common heading/location formats like MESA, AZ or LAS VEGAS, NV.
    for pat in [
        r'(?m)^([A-Z][A-Z .\'-]+,\s*[A-Z]{2})\s*$',
        r'(?i)\b([A-Za-z .\'-]+,\s*[A-Z]{2})\b'
    ]:
        m = re.search(pat, visible)
        if m:
            loc = re.sub(r'\s+', ' ', m.group(1)).strip()
            if len(loc) <= 60:
                return loc
    return ""

def city_from_location_label(label):
    return label.split(",")[0].strip() if label else ""

def scanner_session_alive(driver):
    """Return True only if the Selenium session is still connected and usable."""
    if driver is None:
        return False
    try:
        _ = driver.window_handles
        _ = driver.current_url
        return True
    except Exception:
        return False

def ensure_nellis_tab(driver):
    """
    Reuse an existing Nellis tab if one exists; otherwise open a new Nellis tab
    in the SAME controlled Chrome session.
    """
    if driver is None or not scanner_session_alive(driver):
        return None

    nellis_handle = None
    for handle in driver.window_handles:
        try:
            driver.switch_to.window(handle)
            if "nellisauction.com" in (driver.current_url or "").lower():
                nellis_handle = handle
                break
        except Exception:
            continue

    if nellis_handle is None:
        driver.execute_script("window.open('https://www.nellisauction.com/search','_blank');")
        handles = driver.window_handles
        if handles:
            nellis_handle = handles[-1]
            driver.switch_to.window(nellis_handle)

    return driver


def browser_current_filtered_links(driver, max_links=250):
    """
    Read listing links from the ACTUAL browser DOM after the user has set Nellis filters.
    This preserves Nellis browser/session filters that are not reliably encoded in the URL.
    """
    if driver is None:
        return []

    html = driver.page_source
    links = extract_nellis_listing_links(html)

    # Also read rendered anchors directly from DOM.
    try:
        hrefs = driver.execute_script(
            "return Array.from(document.querySelectorAll('a[href]')).map(a => a.href);"
        ) or []
        links.extend(hrefs)
    except Exception:
        pass

    out=[]
    seen=set()
    for u in links:
        u = normalize_nellis_url(u)
        if is_real_nellis_item_url(u) and u not in seen:
            seen.add(u)
            out.append(u)
            if len(out) >= int(max_links):
                break
    return out


def _parse_nellis_page_source(url, html, visible_text=""):
    """
    Parse a Nellis item from the SAME browser session.
    visible_text is document.body.innerText, which avoids hidden template labels causing false condition flags.
    """
    out = {
        "itemUrl": normalize_nellis_url(url), "title": "", "currentBid": None, "retailPrice": None,
        "condition": "", "category": "", "brand": "", "city": "", "state": "",
        "pickupAddress": "", "pickupCity": "", "pickupState": "", "pickupZip": "",
        "lotId": "", "inventoryNumber": "", "buyersPremiumPct": None,
        "qualityFlags": "", "qualityStars": None,
        "bidSource": "Nellis Browser", "source": "Nellis Browser",
        "pickupVerified": False, "auctionStatus": "", "_cacheSchema": DIRECT_CACHE_SCHEMA
    }

    html = html or ""
    txt = unescape(re.sub(r"\s+", " ", visible_text or re.sub(r"<[^>]+>", " ", html)))
    lowtxt = txt.lower()

    if re.search(r'\b(?:ended|won for|auction ended)\b', lowtxt):
        out["auctionStatus"] = "ended"
    elif re.search(r'\b(?:current price|current bid|time left|ends)\b', lowtxt):
        out["auctionStatus"] = "active"
    else:
        out["auctionStatus"] = "unknown"

    # Title
    for pat in [
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<title>(.*?)</title>',
        r'"title"\s*:\s*"([^"]+)"'
    ]:
        m = re.search(pat, html, re.I | re.S)
        if m:
            out["title"] = re.sub(r"\s*\|\s*Nellis.*$", "", unescape(m.group(1)).strip(), flags=re.I)
            break

    # Bid
    bid, bid_count, note = extract_live_bid_from_html(html)
    out["currentBid"] = bid
    out["bidCount"] = bid_count if bid_count is not None else ""
    out["bidFetchNote"] = note

    # Retail
    for pat in [
        r'(?i)"retailPrice"\s*:\s*"?\$?([0-9][0-9,]*(?:\.[0-9]+)?)"?',
        r'(?i)(?:retail|msrp|estimated retail(?: price)?)\s*[:$ ]{0,12}\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)'
    ]:
        m = re.search(pat, html) or re.search(pat, txt)
        if m:
            try:
                out["retailPrice"] = float(m.group(1).replace(",", ""))
                break
            except Exception:
                pass

    # Quality/star rating: try structured page data and accessibility text.
    star_patterns = [
        r'(?i)"quality(?:Rating|Stars|Score)?"\s*:\s*"?([1-5])"?',
        r'(?i)"starRating"\s*:\s*"?([1-5])"?',
        r'(?i)([1-5])\s*(?:out of 5\s*)?stars?',
        r'(?i)aria-label=["\'][^"\']*?([1-5])\s*(?:out of 5\s*)?stars?'
    ]
    for pat in star_patterns:
        m = re.search(pat, html) or re.search(pat, txt)
        if m:
            try:
                out["qualityStars"] = int(m.group(1))
                break
            except Exception:
                pass

    # Only parse condition badges from VISIBLE browser text.
    # Order matters: specific phrases are removed before generic matching.
    flags = []

    visible_phrases = [
        "Not Functional",
        "Major Damage",
        "Unknown if Missing Parts",
        "Missing Parts",
        "Not In Package",
        "Untested",
        "Used",
        "No Damage",
        "No Assembly Needed",
        "In Package",
        "Damaged",
        "Broken",
        "Cracked",
    ]

    # Protect "Unknown if Missing Parts" from also becoming confirmed "Missing Parts".
    protected = lowtxt.replace("unknown if missing parts", "__unknown_missing__")

    for term in visible_phrases:
        if term == "Unknown if Missing Parts":
            if "__unknown_missing__" in protected:
                flags.append(term)
        elif term == "Missing Parts":
            if "missing parts" in protected:
                flags.append("Confirmed Missing Parts")
        elif term.lower() in lowtxt:
            flags.append(term)

    flags = list(dict.fromkeys(flags))
    out["qualityFlags"] = " | ".join(flags)

    # User-facing condition is intentionally simple in v3.11.
    if out["qualityStars"]:
        out["condition"] = f"{out['qualityStars']} Stars"
    else:
        primary = next(
            (x for x in [
                "Not Functional","Major Damage","Untested","Used","No Damage"
            ] if x in flags),
            ""
        )
        out["condition"] = primary

    # Actual pickup location from visible item page.
    pm = re.search(
        r'(?i)Pickup\s*Location.{0,180}?([0-9]{1,6}\s+[^,<]{3,100})\s+([A-Za-z .]+),\s*([A-Z]{2}),\s*(\d{5})',
        txt
    )
    if pm:
        out["pickupAddress"] = pm.group(1).strip()
        out["pickupCity"] = pm.group(2).strip()
        out["pickupState"] = pm.group(3).strip()
        out["pickupZip"] = pm.group(4).strip()
        out["pickupVerified"] = True
        out["city"] = out["pickupCity"]
        out["state"] = out["pickupState"]

    inv = re.search(r'(?i)(?:Inventory Number|inventoryNumber)\s*[:#]?\s*"?(\d{6,})"?', txt + " " + html)
    if inv:
        out["inventoryNumber"] = inv.group(1)

    lid = re.search(r'(?i)"lotId"\s*:\s*"?(\d+)"?', html)
    if lid:
        out["lotId"] = lid.group(1)

    return out


def scan_current_nellis_browser(driver, max_links=200, progress_callback=None):
    """
    v3.27:
        pass
    - Every scan starts from the CURRENT rendered Nellis results page.
    - Refreshes/stabilizes the current browser page before collecting links so a prior
      Mesa/Vegas scan cannot leak into a newly selected market.
    - Reports live progress while item pages are checked.
    """
    if driver is None:
        raise RuntimeError("Open Nellis first.")

    driver = ensure_nellis_tab(driver)
    if driver is None:
        raise RuntimeError("The Nellis scanner session is not available.")

    # Let Nellis finish any location/filter transition before taking the snapshot.
    try:
        time.sleep(1.0)
        driver.refresh()
        time.sleep(1.5)
    except Exception:
        time.sleep(1.0)

    results_url = driver.current_url
    scan_location = detect_nellis_location_from_browser(driver)

    # Snapshot links ONLY from the current rendered page after stabilization.
    links = browser_current_filtered_links(driver, max_links=max_links)

    rows = []
    ended = []
    errors = []
    original_url = results_url
    total = len(links)

    if progress_callback:
        progress_callback(0, total, scan_location)

    for i, u in enumerate(links, start=1):
        try:
            driver.get(u)
            time.sleep(0.20)
            try:
                visible_text = driver.execute_script(
                    "return document.body ? document.body.innerText : '';"
                ) or ""
            except Exception:
                visible_text = ""

            row = _parse_nellis_page_source(
                u,
                driver.page_source,
                visible_text=visible_text
            )
            row["scanLocation"] = scan_location

            if listing_is_active(row):
                rows.append(row)
            else:
                ended.append(row)

        except Exception as e:
            errors.append({"itemUrl": u, "error": str(e)[:120]})

        if progress_callback:
            progress_callback(i, total, scan_location)

    # Return to exactly the filtered results page used for this scan.
    try:
        driver.get(original_url)
        time.sleep(0.5)
    except Exception:
        pass

    return {
        "rows": rows,
        "ended": ended,
        "errors": errors,
        "links_found": len(links),
        "results_url": results_url,
        "scan_location": scan_location,
    }




# ---------- FlipScout Web v4.0: server-side Nellis scanner ----------

def start_server_browser():
    """Start hidden Chromium reliably on Render/Docker."""
    if webdriver is None:
        raise RuntimeError("Selenium is not installed.")

    options = ChromeOptions()
    options.binary_location = "/usr/bin/chromium"
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1400,1000")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--single-process")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    service = ChromeService(executable_path="/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(25)
    driver.set_script_timeout(15)
    return driver


def _click_text(driver, text):
    """Best-effort click on rendered control/text matching a filter label."""
    if not text:
        return False
    js = """
    const target = arguments[0].trim().toLowerCase();
    const els = [...document.querySelectorAll('button,a,label,span,div,[role="button"],[role="option"],input')];
    const exact = els.find(e => ((e.innerText || e.value || '').trim().toLowerCase() === target));
    if (exact) { exact.click(); return true; }
    const partial = els.find(e => ((e.innerText || e.value || '').trim().toLowerCase().includes(target)));
    if (partial) { partial.click(); return true; }
    return false;
    """
    try:
        return bool(driver.execute_script(js, str(text)))
    except Exception:
        return False


def apply_nellis_filter(driver, label, value):
    """
    Apply a Nellis search filter by opening the named filter control and clicking the value.
    This is intentionally generic so Nellis can change minor UI markup without breaking every filter.
    """
    if not value or str(value).strip().lower() in ("any", "all", "none"):
        return True

    # Open filter panel/button first when a label is supplied.
    if label:
        _click_text(driver, label)
        time.sleep(0.35)

    ok = _click_text(driver, value)
    time.sleep(0.55)
    return ok


def scan_nellis_web(location, category="", subcategory="", star_rating="", max_links=100,
                    progress_callback=None, status_callback=None):
    """
    Server-side Nellis scan with bounded waits and stage reporting.
    """
    def status(msg):
        if status_callback:
            status_callback(msg)

    driver = None
    try:
        status("Starting server browser…")
        driver = start_server_browser()

        status("Opening Nellis…")
        try:
            driver.get("https://www.nellisauction.com/search")
        except TimeoutException:
            # A timeout can still leave a usable partially-rendered page.
            status("Nellis took too long to fully load; continuing with rendered content…")
        time.sleep(2.0)

        status(f"Selecting location: {location}…")
        if location:
            apply_nellis_filter(driver, "Location", location)
            time.sleep(1.0)

        if category:
            status(f"Applying category: {category}…")
            apply_nellis_filter(driver, "Category", category)
            time.sleep(0.7)

        if subcategory:
            status(f"Applying subcategory: {subcategory}…")
            apply_nellis_filter(driver, "", subcategory)
            time.sleep(0.7)

        if star_rating:
            status(f"Applying condition: {star_rating}…")
            attempted = [
                str(star_rating),
                f"{str(star_rating).replace('.0','')} Stars",
                f"{str(star_rating).replace('.0','')} Star",
            ]
            _click_text(driver, "Condition")
            time.sleep(0.35)
            for candidate in attempted:
                if _click_text(driver, candidate):
                    break
            time.sleep(0.8)

        status("Reading rendered Nellis results…")
        scan_location = detect_nellis_location_from_browser(driver) or location
        links = browser_current_filtered_links(driver, max_links=int(max_links))

        if not links:
            # One extra bounded wait before giving up.
            time.sleep(2.0)
            links = browser_current_filtered_links(driver, max_links=int(max_links))

        rows, ended, errors = [], [], []
        total = len(links)

        if progress_callback:
            progress_callback(0, total, scan_location)

        status(f"Found {total} listing links. Reading item details…")

        for i, u in enumerate(links, 1):
            try:
                try:
                    driver.get(u)
                except TimeoutException:
                    pass

                time.sleep(0.12)

                try:
                    visible_text = driver.execute_script(
                        "return document.body ? document.body.innerText : '';"
                    ) or ""
                except Exception:
                    visible_text = ""

                row = _parse_nellis_page_source(
                    u, driver.page_source, visible_text=visible_text
                )
                row["scanLocation"] = scan_location

                if listing_is_active(row):
                    rows.append(row)
                else:
                    ended.append(row)

            except Exception as e:
                errors.append({"itemUrl": u, "error": str(e)[:160]})

            if progress_callback:
                progress_callback(i, total, scan_location)

        status("Finishing deal analysis…")
        return {
            "rows": rows,
            "ended": ended,
            "errors": errors,
            "links_found": total,
            "scan_location": scan_location,
        }

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def normalize_nellis_market(location):
    """
    Convert friendly location labels to the value Nellis uses in its URL.
    Returns (market_name_for_url, expected_state, accepted_pickup_cities).
    """
    raw = (location or "").strip()
    key = raw.lower()

    markets = {
        "phoenix": ("Phoenix", "AZ", {"phoenix", "mesa"}),
        "phoenix, az": ("Phoenix", "AZ", {"phoenix", "mesa"}),
        "mesa": ("Mesa", "AZ", {"mesa", "phoenix"}),
        "mesa, az": ("Mesa", "AZ", {"mesa", "phoenix"}),
        "las vegas": ("Las Vegas", "NV", {"las vegas", "north las vegas", "henderson"}),
        "las vegas, nv": ("Las Vegas", "NV", {"las vegas", "north las vegas", "henderson"}),
        "philadelphia": ("Philadelphia", "PA", {"philadelphia"}),
        "philadelphia, pa": ("Philadelphia", "PA", {"philadelphia"}),
    }

    if key in markets:
        return markets[key]

    # Generic fallback for future Nellis markets.
    if "," in raw:
        city, state = [x.strip() for x in raw.rsplit(",", 1)]
        return city, state.upper(), {city.lower()}
    return raw, "", {raw.lower()} if raw else set()


def _nellis_search_url(location, category="", subcategory="", star_rating=""):
    """Build the same style of search URL Nellis produces in the browser."""
    from urllib.parse import urlencode

    market_name, _, _ = normalize_nellis_market(location)

    params = []
    if star_rating:
        rating = str(star_rating).replace(" Stars", "").replace(" Star", "").strip()
        if rating and rating.lower() != "any":
            if "." not in rating:
                rating = rating + ".0"
            params.append(("Star Rating", rating))

    if market_name:
        params.append(("Location Name", market_name))

    if category:
        params.append(("Taxonomy Level 1", category))

    if subcategory:
        params.append(("Taxonomy Level 2", subcategory))

    return "https://www.nellisauction.com/search?" + urlencode(params)


def listing_matches_selected_market(row, location):
    """
    Hard location gate. A listing must match the selected market before FlipScout can show it.
    """
    market_name, expected_state, accepted_cities = normalize_nellis_market(location)

    city = str(
        row.get("pickupCity")
        or row.get("city")
        or row.get("shoppingLocation")
        or ""
    ).strip().lower()

    state = str(
        row.get("pickupState")
        or row.get("state")
        or ""
    ).strip().upper()

    # Reject an explicit wrong state immediately.
    if expected_state and state and state != expected_state:
        return False

    # If city is explicit, it must belong to the selected market cluster.
    if city and accepted_cities:
        # Some parsers expose strings like "Phoenix, AZ" in city/location.
        city_only = city.split(",")[0].strip()
        if city_only not in accepted_cities:
            return False

    # Require at least some positive location evidence.
    if expected_state and state == expected_state:
        return True
    if city and accepted_cities and city.split(",")[0].strip() in accepted_cities:
        return True

    return False


def _nellis_market_url_candidates(location, category="", subcategory="", star_rating=""):
    """Try Nellis market URL forms defensively; Nellis has changed market routing over time."""
    from urllib.parse import urlencode
    market_name, state, _ = normalize_nellis_market(location)
    full = f"{market_name}, {state}" if state else market_name
    base_filters = []
    if star_rating:
        rating = str(star_rating).replace(" Stars", "").replace(" Star", "").strip()
        if rating and rating.lower() != "any":
            base_filters.append(("Star Rating", rating if "." in rating else rating + ".0"))
    if category:
        base_filters.append(("Taxonomy Level 1", category))
    if subcategory:
        base_filters.append(("Taxonomy Level 2", subcategory))

    variants = [
        [("Location Name", full)],
        [("Location Name", market_name)],
        [("location", full)],
        [("Location", full)],
        [("market", full)],
    ]
    urls=[]
    for loc_params in variants:
        u="https://www.nellisauction.com/search?" + urlencode(loc_params + base_filters)
        if u not in urls:
            urls.append(u)
    return urls


def scan_nellis_lightweight(location, category="", subcategory="", star_rating="", max_links=100,
                             progress_callback=None, status_callback=None):
    """Low-memory scanner: HTTP only. No Chromium/Selenium process."""
    def status(msg):
        if status_callback:
            status_callback(msg)

    status("Connecting to Nellis without launching Chrome…")
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    status(f"Loading filtered Nellis inventory for {location}…")
    # Nellis can route the market through URL state and/or cookies. Seed both, then
    # try several known URL forms. The hard item-page location gate below remains
    # authoritative, so a wrong-market response can never leak into results.
    market_name, market_state, _ = normalize_nellis_market(location)
    full_market = f"{market_name}, {market_state}" if market_state else market_name
    for ck in ("location", "selectedLocation", "market", "selectedMarket"):
        session.cookies.set(ck, full_market, domain=".nellisauction.com")

    html_pages=[]
    for search_url in _nellis_market_url_candidates(location, category, subcategory, star_rating):
        try:
            resp = session.get(search_url, timeout=(8, 20))
            if resp.ok and resp.text:
                html_pages.append(resp.text)
        except requests.RequestException:
            continue
    if not html_pages:
        raise RuntimeError("Nellis did not return a search page for the selected market.")

    # Extract product URLs from all candidate market responses.
    patterns = [
        r'https://www\.nellisauction\.com/p/[^"\'<>\s\\]+',
        r'href=["\'](/p/[^"\']+)["\']',
        r'["\'](?:itemUrl|url)["\']\s*:\s*["\']([^"\']*/p/[^"\']+)["\']',
    ]
    found = []
    for html in html_pages:
      for pat in patterns:
        for m in re.findall(pat, html, flags=re.I):
            u = m if isinstance(m, str) else m[0]
            u = u.replace("\\u0026", "&").replace("\\/", "/")
            if u.startswith("/"):
                u = "https://www.nellisauction.com" + u
            if u.startswith("https://www.nellisauction.com/p/") and u not in found:
                found.append(u)
            if len(found) >= int(max_links):
                break
        if len(found) >= int(max_links):
            break

    links = found[:int(max_links)]
    total = len(links)
    status(f"Found {total} listing links. Reading item details…")
    if progress_callback:
        progress_callback(0, total, location)

    rows, ended, errors = [], [], []
    for i, u in enumerate(links, 1):
        try:
            r = session.get(u, timeout=(6, 15))
            r.raise_for_status()
            visible = re.sub(r"<script\b[^>]*>.*?</script>", " ", r.text, flags=re.I|re.S)
            visible = re.sub(r"<style\b[^>]*>.*?</style>", " ", visible, flags=re.I|re.S)
            visible = re.sub(r"<[^>]+>", " ", visible)
            visible = re.sub(r"\s+", " ", visible)
            row = _parse_nellis_page_source(u, r.text, visible_text=visible)
            row["scanLocation"] = location

            if not listing_matches_selected_market(row, location):
                errors.append({
                    "itemUrl": u,
                    "error": f"Rejected wrong pickup market for selected location: {location}"
                })
            elif listing_is_active(row):
                rows.append(row)
            else:
                ended.append(row)
        except Exception as e:
            errors.append({"itemUrl": u, "error": str(e)[:160]})
        if progress_callback:
            progress_callback(i, total, location)

    return {
        "rows": rows, "ended": ended, "errors": errors,
        "links_found": total, "scan_location": location,
        "search_url": search_url,
    }

# ---------- FlipScout Web v4.0 UI ----------
st.set_page_config(
    page_title="FlipScout AI",
    page_icon="🔥",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.block-container {padding-top:2rem; padding-bottom:3rem; max-width:1500px;}
[data-testid="stSidebar"] {border-right:1px solid rgba(255,255,255,.08);}
.fs-hero {padding:1.5rem; border:1px solid rgba(255,255,255,.09); border-radius:18px;
background:linear-gradient(135deg,rgba(255,75,75,.10),rgba(35,66,95,.18)); margin-bottom:1.25rem;}
.fs-title {font-size:2.45rem; font-weight:800; line-height:1.05;}
.fs-sub {margin-top:.55rem; opacity:.78; max-width:900px;}
div.stButton>button {border-radius:10px; font-weight:700; min-height:44px;}
div[data-testid="stDataFrame"] {border-radius:14px; border:1px solid rgba(255,255,255,.08);}
</style>

<div class="fs-hero">
  <div class="fs-title">🔥 FlipScout AI</div>
  <div class="fs-sub">Scan Nellis Auction inventory from the web, estimate resale value, and find strong flip opportunities before you bid.</div>
</div>
""", unsafe_allow_html=True)

st.sidebar.markdown("## 🔥 FlipScout")
st.sidebar.caption("Web Edition")
st.sidebar.divider()
st.sidebar.markdown("### Deal Settings")

bid_budget = st.sidebar.slider(
    "Current bid range",
    min_value=0,
    max_value=500,
    value=(0, 75),
    step=5,
)
min_current_bid = float(bid_budget[0])
max_current_bid = float(bid_budget[1])

min_profit = st.sidebar.number_input(
    "Minimum profit target",
    min_value=0,
    max_value=5000,
    value=75,
    step=5,
)

st.markdown("## Choose Nellis Inventory")
st.caption("FlipScout uses a lightweight web scan designed for low-memory hosting. No local Python or browser extension is required.")

c1, c2 = st.columns(2)
with c1:
    location = st.text_input(
        "Nellis location",
        value="Phoenix, AZ",
        help="Enter the Nellis market exactly as it appears on Nellis, e.g. Phoenix, AZ; Mesa, AZ; Las Vegas, NV; Philadelphia, PA."
    )
with c2:
    star_rating = st.selectbox(
        "Condition / star rating",
        ["Any", "5 Stars", "4 Stars", "3 Stars", "2 Stars", "1 Star"],
        index=0,
    )

c3, c4 = st.columns(2)
with c3:
    category = st.text_input(
        "Category (optional)",
        placeholder="Electronics",
    )
with c4:
    subcategory = st.text_input(
        "Subcategory (optional)",
        placeholder="Computers, Laptops, Tablets & Accessories",
    )

with st.expander("Advanced scan settings"):
    st.caption("FlipScout will use Nellis-style URL parameters and verify every listing's pickup market.")
    max_links = st.number_input(
        "Maximum listings to inspect",
        min_value=10,
        max_value=300,
        value=100,
        step=10,
    )

scan = st.button("🔥 FIND PROFITABLE DEALS", type="primary", use_container_width=True)

if scan:
    if not location.strip():
        st.error("Enter a Nellis location first.")
    else:
        progress = st.progress(0, text="Preparing web scan…")
        progress_text = st.empty()
        scan_status = st.info("Starting server browser…")

        def _status(message):
            scan_status.info(message)

        def _progress(done, total, loc):
            pct = 0 if total <= 0 else min(100, int(round(done / total * 100)))
            progress.progress(pct, text=f"Scanning {loc or location}: {pct}%")
            progress_text.caption(f"{done} of {total} listings checked")

        try:
            result = scan_nellis_lightweight(
                location=location.strip(),
                category=category.strip(),
                subcategory=subcategory.strip(),
                star_rating="" if star_rating == "Any" else star_rating,
                max_links=int(max_links),
                progress_callback=_progress,
                status_callback=_status,
            )
        except Exception as e:
            st.error(f"Web scan could not complete: {e}")
            st.stop()

        scan_status.success("Nellis scan finished.")
        progress.progress(100, text="Scan complete — 100%")
        progress_text.caption(f"{result['links_found']} listings checked")

        rows = result["rows"]
        if not rows:
            st.warning(
                f"No verified listings matched {result.get('scan_location') or location}. "
                "FlipScout rejected any inventory from other Nellis markets. "
                "Try broader category/condition filters if this market is correct."
            )
        else:
            live = standardize(pd.DataFrame(rows))

            for col in ["title","city","state","condition","category","brand","itemUrl","shoppingLocation"]:
                if col in live.columns:
                    live[col] = live[col].fillna("").astype(str)

            bid_num = pd.to_numeric(live["currentBid"], errors="coerce")
            live = live[
                bid_num.notna()
                & (bid_num >= min_current_bid)
                & (bid_num <= max_current_bid)
            ].copy()

            if live.empty:
                st.warning(
                    f"Listings were found, but none are currently inside your "
                    f"${min_current_bid:,.0f}–${max_current_bid:,.0f} bid range."
                )
            else:
                premium = 0.15
                tax = 0.08
                selling_fee = 0.0
                min_roi = 0.0

                scored = pd.concat(
                    [
                        live.reset_index(drop=True),
                        live.apply(
                            lambda r: calc_row(
                                r, premium, tax, selling_fee, min_profit, min_roi
                            ),
                            axis=1
                        ).reset_index(drop=True),
                    ],
                    axis=1,
                )
                scored = scored.loc[:, ~scored.columns.duplicated(keep="last")].copy()

                if "Comp Count" not in scored.columns:
                    scored["Comp Count"] = scored.apply(
                        lambda r: sum(
                            1 for c in ["comp1","comp2","comp3"]
                            if clean_num(r.get(c, 0)) > 0
                        ),
                        axis=1
                    )

                scored = enforce_final_verdicts(
                    scored,
                    min_bid=min_current_bid,
                    max_bid=max_current_bid,
                    min_profit=float(min_profit),
                    min_roi=0.0,
                )

                def _guidance(r):
                    resale = clean_num(r.get("Est. Quick Sale", 0)) or 0
                    low, high, hard = recommended_bid_range(
                        resale, premium, tax, selling_fee, min_profit,
                        current_bid=clean_num(r.get("Current Bid", 0)) or 0
                    )
                    return pd.Series({
                        "Best Bid From": low,
                        "Best Bid To": high,
                        "DO NOT EXCEED": hard,
                    })

                guidance = scored.apply(_guidance, axis=1)
                scored = pd.concat([scored, guidance], axis=1)
                scored = scored.loc[:, ~scored.columns.duplicated(keep="last")].copy()

                scored["Est. Profit"] = pd.to_numeric(
                    scored.get("Projected Profit", 0), errors="coerce"
                ).fillna(0)

                def _deal_label(r):
                    raw = str(r.get("Verdict", "")).upper()
                    profit = clean_num(r.get("Projected Profit", r.get("Est. Profit", 0))) or 0
                    score = clean_num(r.get("Deal Score", 0)) or 0
                    condition = str(r.get("condition", "") or "").lower()
                    if any(x in condition for x in ["not functional","major damage","broken","parts only"]):
                        return "❌ SKIP"
                    if raw == "BUY":
                        return "🔥 BUY"
                    if raw == "WATCH" or (profit >= float(min_profit) and score >= 70):
                        return "👀 WATCH"
                    return "❌ SKIP"

                scored["Deal"] = scored.apply(_deal_label, axis=1)
                scored = scored.sort_values(
                    ["Est. Profit", "Deal Score"], ascending=[False, False]
                )

                shown = scored[
                    (scored["Est. Profit"] >= float(min_profit))
                    | scored["Verdict"].isin(["BUY", "WATCH"])
                ].copy()

                if shown.empty:
                    st.info("The inventory loaded, but no listings currently meet your minimum-profit target.")
                else:
                    st.markdown("## Best Flip Opportunities")

                    cols = [
                        "Deal","title","city","condition",
                        "Current Bid","Best Bid From","Best Bid To","DO NOT EXCEED",
                        "Est. Quick Sale","Est. Profit","Deal Score","itemUrl"
                    ]
                    for c in cols:
                        if c not in shown.columns:
                            shown[c] = ""

                    shown = shown.loc[:, ~shown.columns.duplicated(keep="last")].copy()

                    st.dataframe(
                        shown[cols],
                        use_container_width=True,
                        hide_index=True,
                        height=650,
                        column_config={
                            "Deal": st.column_config.TextColumn("Deal"),
                            "title": st.column_config.TextColumn("Item"),
                            "city": st.column_config.TextColumn("Pickup"),
                            "condition": st.column_config.TextColumn("Nellis Condition"),
                            "Current Bid": st.column_config.NumberColumn("Current Bid", format="$%.2f"),
                            "Best Bid From": st.column_config.NumberColumn("Target Bid Low", format="$%.2f"),
                            "Best Bid To": st.column_config.NumberColumn("Target Bid High", format="$%.2f"),
                            "DO NOT EXCEED": st.column_config.NumberColumn("Absolute Max Bid", format="$%.2f"),
                            "Est. Quick Sale": st.column_config.NumberColumn("Estimated Resale", format="$%.2f"),
                            "Est. Profit": st.column_config.NumberColumn("Estimated Profit", format="$%.2f"),
                            "itemUrl": st.column_config.LinkColumn("Open Auction"),
                        },
                    )

                    st.caption(
                        f"{result['links_found']} listing links scanned • "
                        f"{len(result['ended'])} ended filtered • "
                        f"{len(result['errors'])} page errors"
                    )

st.divider()
st.caption("FlipScout AI Web v4.4")
