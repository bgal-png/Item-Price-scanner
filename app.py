import streamlit as st
import requests
from bs4 import BeautifulSoup
import pandas as pd
import unicodedata
import math
import json
import os
import time
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_SHOPS = ["cocky-kontaktni.cz", "cocky-online.cz", "cocky-optika.cz", "alensa.cz"]

DEFAULT_SETTINGS = {
    "margin": 30.0,
    "rounding": "None",
    "offset_type": "None",
    "offset_value": 0.0,
    "alert_threshold": 0.0,
    "alert_threshold_type": "Kč",
    "competitors": [],
}

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner_settings.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------
def load_settings_file() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_settings_file(settings: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # silently fail on cloud


# ---------------------------------------------------------------------------
# Session state init
# ---------------------------------------------------------------------------
def init_state():
    if "shop_settings" not in st.session_state:
        saved = load_settings_file()
        st.session_state.shop_settings = {}
        for shop in DEFAULT_SHOPS:
            s = dict(DEFAULT_SETTINGS)
            if shop in saved:
                s.update(saved[shop])
            s["competitors"] = s.get("competitors", [])
            st.session_state.shop_settings[shop] = s

    if "shop_files" not in st.session_state:
        st.session_state.shop_files = {shop: None for shop in DEFAULT_SHOPS}

    if "scan_results" not in st.session_state:
        st.session_state.scan_results = []

    if "url_text" not in st.session_state:
        st.session_state.url_text = ""


# ---------------------------------------------------------------------------
# Core logic (module-level, no UI deps)
# ---------------------------------------------------------------------------
def apply_rounding(price: float, rule: str) -> float:
    if rule == "End in .90":
        return math.floor(price) + 0.90
    elif rule == "End in .99":
        return math.floor(price) + 0.99
    elif rule == "Round to integer":
        return float(round(price))
    return round(price, 2)


def scrape_url(url: str) -> tuple[float | None, dict, str | None]:
    """Returns (overall_lowest, catalog, product_name)."""
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        # First hit the homepage to get cookies, then the product page
        parsed = urlparse(url)
        homepage = f"{parsed.scheme}://{parsed.netloc}/"
        session.get(homepage, timeout=10)
        time.sleep(1)
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Request failed: {e}")

    html = resp.content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")


    h1 = soup.find("h1", class_=lambda c: c and "c-product-info__name" in c)
    product_name = h1.get_text(strip=True) if h1 else None

    def _parse_price(text: str) -> float | None:
        raw = text.replace("Kč", "").replace("\xa0", "").replace("\u00a0", "").replace(" ", "").replace(",", ".").strip()
        try:
            return float(raw)
        except ValueError:
            return None

    # Try primary selector, then broader fallbacks
    price_spans = (
        soup.find_all("span", class_=lambda c: c and "c-offer__price" in c)
        or soup.find_all(attrs={"data-testid": lambda v: v and "price" in v.lower()})
        or soup.find_all(class_=lambda c: c and "price" in c.lower() and "offer" in c.lower())
    )

    all_prices = [p for p in (_parse_price(s.get_text()) for s in price_spans) if p is not None]

    if not all_prices:
        return None, {}, product_name

    overall_lowest = min(all_prices)
    catalog = {}

    # Try primary shop-link selector, then broader fallback
    offer_links = (
        soup.find_all("a", attrs={"data-testid": "Offer Exit Button"})
        or soup.find_all("a", attrs={"data-testid": lambda v: v and "offer" in v.lower()})
    )

    for btn in offer_links:
        label = unicodedata.normalize("NFD", btn.get("aria-label", "")).encode("ascii", "ignore").decode("utf-8").lower()
        p_span = (
            btn.find_previous("span", class_=lambda c: c and "c-offer__price" in c)
            or btn.find_previous(attrs={"data-testid": lambda v: v and "price" in v.lower()})
        )
        if p_span:
            price = _parse_price(p_span.get_text())
            if price is not None:
                catalog[label] = price

    return overall_lowest, catalog, product_name


def analyze_shop(shop: str, catalog: dict, overall_lowest: float, product_name: str) -> dict:
    s = st.session_state.shop_settings[shop]
    margin_req = s["margin"] / 100

    result = {
        "shop": shop,
        "status": "not_found",
        "status_text": "❌ Not found",
        "price": None,
        "market": None,
        "cost": None,
        "min_price": None,
        "target": None,
    }

    # Lowest market price (filtered by competitors if set)
    competitors = s.get("competitors", [])
    if competitors:
        comp_prices = []
        for lbl, prc in catalog.items():
            for comp in competitors:
                if comp.split(".")[0].lower() in lbl:
                    comp_prices.append(prc)
                    break
        lowest_market = min(comp_prices) if comp_prices else overall_lowest
    else:
        lowest_market = overall_lowest

    result["market"] = lowest_market

    # Find this shop's price in catalog
    found_price = None
    for lbl, prc in catalog.items():
        if shop.split(".")[0] in lbl:
            found_price = prc
            break

    if found_price is None:
        return result

    result["price"] = found_price

    # Competitive target
    offset_type = s["offset_type"]
    offset_val = s["offset_value"]
    if offset_type == "Kč below market":
        result["target"] = apply_rounding(lowest_market - offset_val, s["rounding"])
    elif offset_type == "% below market":
        result["target"] = apply_rounding(lowest_market * (1 - offset_val / 100), s["rounding"])
    elif offset_type == "Kč above market":
        result["target"] = apply_rounding(lowest_market + offset_val, s["rounding"])
    elif offset_type == "% above market":
        result["target"] = apply_rounding(lowest_market * (1 + offset_val / 100), s["rounding"])

    # Pricelist lookup
    df = st.session_state.shop_files.get(shop)
    if df is None:
        result["status"] = "no_pricelist"
        result["status_text"] = "⚠️ No pricelist loaded"
        return result

    search_term = product_name.lower().replace(" ", "") if product_name else ""
    cost_vat = None
    for _, row in df.iterrows():
        row_name = str(row.iloc[1]).lower().replace(" ", "")
        if search_term and (search_term in row_name or row_name in search_term):
            try:
                cost_vat = float(row.iloc[2])
            except (ValueError, TypeError):
                pass
            break

    if cost_vat is None:
        result["status"] = "no_cost"
        result["status_text"] = "⚠️ Not in pricelist"
        return result

    min_allowed = apply_rounding(cost_vat / (1 - margin_req), s["rounding"])
    result["cost"] = cost_vat
    result["min_price"] = min_allowed

    gap = min_allowed - found_price
    threshold = s["alert_threshold"]
    threshold_type = s["alert_threshold_type"]

    if gap > 0:
        if threshold_type == "%":
            gap_pct = (gap / min_allowed) * 100
            should_alert = gap_pct > threshold if threshold > 0 else True
            gap_str = f"{round(gap, 2)} Kč ({round(gap_pct, 1)}%)"
        else:
            should_alert = gap > threshold if threshold > 0 else True
            gap_str = f"{round(gap, 2)} Kč"

        if should_alert:
            result["status"] = "alert"
            result["status_text"] = f"🚨 -{gap_str}"
        else:
            result["status"] = "warning"
            result["status_text"] = f"⚠️ -{gap_str} (within threshold)"
    else:
        actual_margin = round((1 - cost_vat / found_price) * 100, 1)
        result["status"] = "healthy"
        result["status_text"] = f"✅ {actual_margin}% margin"

    return result


def fmt_czk(val) -> str:
    if val is None:
        return "—"
    return f"{val:,.2f} Kč".replace(",", " ")


def status_color(status: str) -> str:
    return {
        "healthy":     "#a6e3a1",
        "alert":       "#f38ba8",
        "warning":     "#f9e2af",
        "not_found":   "#585b70",
        "no_pricelist":"#f9e2af",
        "no_cost":     "#f9e2af",
        "error":       "#f38ba8",
    }.get(status, "#cdd6f4")


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Heureka Price Scanner", page_icon="📊", layout="wide")
init_state()

st.title("📊 Heureka Price Scanner")

# ===========================================================================
# SIDEBAR — per-shop settings & pricelist upload
# ===========================================================================
with st.sidebar:
    st.header("Shop Configuration")

    for shop in DEFAULT_SHOPS:
        with st.expander(f"⚙️ {shop}", expanded=False):
            s = st.session_state.shop_settings[shop]

            margin = st.number_input("Target Margin (%)", value=s["margin"], min_value=0.0, max_value=99.0, step=0.5, key=f"margin_{shop}")
            rounding = st.selectbox("Price Rounding", ["None", "End in .90", "End in .99", "Round to integer"],
                index=["None", "End in .90", "End in .99", "Round to integer"].index(s["rounding"]), key=f"rounding_{shop}")
            offset_type = st.selectbox("Market Offset", ["None", "Kč below market", "% below market", "Kč above market", "% above market"],
                index=["None", "Kč below market", "% below market", "Kč above market", "% above market"].index(s["offset_type"]), key=f"offset_type_{shop}")
            offset_value = st.number_input("Offset Value", value=s["offset_value"], min_value=0.0, step=1.0, key=f"offset_val_{shop}")

            col1, col2 = st.columns([2, 1])
            with col1:
                alert_threshold = st.number_input("Alert Threshold", value=s["alert_threshold"], min_value=0.0, step=1.0, key=f"threshold_{shop}")
            with col2:
                alert_type = st.selectbox("Unit", ["Kč", "%"], index=["Kč", "%"].index(s["alert_threshold_type"]), key=f"threshold_type_{shop}")

            st.caption("Competitors (one per line, empty = all shops)")
            comp_text = st.text_area("", value="\n".join(s["competitors"]), height=80, key=f"competitors_{shop}", label_visibility="collapsed")

            if st.button("Save Settings", key=f"save_{shop}"):
                st.session_state.shop_settings[shop].update({
                    "margin": margin,
                    "rounding": rounding,
                    "offset_type": offset_type,
                    "offset_value": offset_value,
                    "alert_threshold": alert_threshold,
                    "alert_threshold_type": alert_type,
                    "competitors": [c.strip() for c in comp_text.splitlines() if c.strip()],
                })
                save_settings_file(st.session_state.shop_settings)
                st.success("Saved!")

            st.divider()
            uploaded = st.file_uploader(f"Pricelist for {shop}", type=["xlsx", "xls", "csv"], key=f"upload_{shop}")
            if uploaded is not None:
                try:
                    df = pd.read_csv(uploaded) if uploaded.name.endswith(".csv") else pd.read_excel(uploaded)
                    st.session_state.shop_files[shop] = df
                    st.success(f"Loaded {len(df)} rows")
                except Exception as e:
                    st.error(f"Failed to load: {e}")
            elif st.session_state.shop_files[shop] is not None:
                st.info(f"Pricelist loaded ({len(st.session_state.shop_files[shop])} rows)")

# ===========================================================================
# MAIN — URLs + shop selection + scan
# ===========================================================================
st.subheader("Product URLs")

col_text, col_btns = st.columns([4, 1])

with col_text:
    url_input = st.text_area(
        "Enter Heureka.cz product URLs (one per line)",
        value=st.session_state.url_text,
        height=120,
        key="url_input_area",
        label_visibility="collapsed",
        placeholder="https://www.heureka.cz/...\nhttps://www.heureka.cz/...",
    )
    st.session_state.url_text = url_input

with col_btns:
    url_file = st.file_uploader("Load URL file", type=["txt", "csv"], label_visibility="collapsed")
    if url_file is not None:
        raw = url_file.read().decode("utf-8")
        loaded_urls = [ln.strip() for ln in raw.splitlines() if ln.strip().startswith("http")]
        if loaded_urls:
            st.session_state.url_text = "\n".join(loaded_urls)
            st.rerun()

    if st.button("Clear URLs", use_container_width=True):
        st.session_state.url_text = ""
        st.rerun()

st.subheader("Shops to Monitor")
shop_cols = st.columns(len(DEFAULT_SHOPS))
selected_shops = []
for i, shop in enumerate(DEFAULT_SHOPS):
    with shop_cols[i]:
        if st.checkbox(shop, value=(i == 0), key=f"chk_{shop}"):
            selected_shops.append(shop)

st.divider()

scan_clicked = st.button("🚀 Start Scan", type="primary", use_container_width=False)

if scan_clicked:
    urls = [u.strip() for u in st.session_state.url_text.splitlines() if u.strip().startswith("http")]

    if not urls:
        st.warning("Please enter at least one Heureka.cz URL.")
    elif not selected_shops:
        st.warning("Please select at least one shop.")
    else:
        all_results = []
        progress_bar = st.progress(0, text="Starting scan…")

        for idx, url in enumerate(urls):
            slug = urlparse(url).path.split("/")[-1].replace("-", " ").title()
            progress_bar.progress((idx) / len(urls), text=f"Scanning {idx + 1}/{len(urls)}: {slug}…")

            try:
                overall_lowest, catalog, scraped_name = scrape_url(url)
                product_name = scraped_name or slug
            except Exception as e:
                all_results.append({
                    "product": slug,
                    "overall_lowest": None,
                    "shops": [{"shop": "Error", "status": "error", "status_text": str(e),
                               "price": None, "market": None, "cost": None, "min_price": None, "target": None}],
                })
                time.sleep(1)
                continue

            if overall_lowest is None:
                all_results.append({
                    "product": product_name,
                    "overall_lowest": None,
                    "shops": [{"shop": "—", "status": "not_found", "status_text": "❌ No prices found on page",
                               "price": None, "market": None, "cost": None, "min_price": None, "target": None}],
                })
                time.sleep(1)
                continue

            shops = [analyze_shop(s, catalog, overall_lowest, product_name) for s in selected_shops]
            all_results.append({
                "product": product_name,
                "overall_lowest": overall_lowest,
                "shops": shops,
            })

            if idx < len(urls) - 1:
                time.sleep(1.5)  # polite delay

        progress_bar.progress(1.0, text="Scan complete!")
        st.session_state.scan_results = all_results

# ===========================================================================
# RESULTS
# ===========================================================================
if st.session_state.scan_results:
    st.divider()
    total_products = len(st.session_state.scan_results)
    total_rows = sum(len(r["shops"]) for r in st.session_state.scan_results)
    st.caption(f"📊 {total_products} product(s) · {total_rows} shop result(s)")

    for product_group in st.session_state.scan_results:
        lowest_text = fmt_czk(product_group["overall_lowest"])
        st.markdown(f"### 📦 {product_group['product']}")
        st.caption(f"Market lowest: **{lowest_text}**")

        rows = []
        for sd in product_group["shops"]:
            rows.append({
                "Shop":      sd["shop"],
                "Price":     fmt_czk(sd.get("price")),
                "Market":    fmt_czk(sd.get("market")),
                "Cost":      fmt_czk(sd.get("cost")),
                "Min Price": fmt_czk(sd.get("min_price")),
                "Target":    fmt_czk(sd.get("target")),
                "Status":    sd.get("status_text", "—"),
                "_status":   sd.get("status", ""),
            })

        df_display = pd.DataFrame(rows)

        def color_row(row):
            clr = status_color(row["_status"])
            styles = [""] * len(row)
            status_idx = list(row.index).index("Status")
            styles[status_idx] = f"color: {clr}; font-weight: bold"
            return styles

        styled = (
            df_display
            .style.apply(color_row, axis=1)
            .hide(["_status"], axis="columns")
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)
        st.markdown("---")

