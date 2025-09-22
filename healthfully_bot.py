#!/usr/bin/env python3
# Healthfully Farm availability -> Telegram notifier
# ONLY looks at the /shop/ catalog grid. No product-page checks.

import os, re, requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from textwrap import shorten

SHOP_URL  = os.getenv("SHOP_URL", "https://healthfullyfarm.com/shop/")
TIMEOUT   = int(os.getenv("TIMEOUT", "20"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
BUY_TXT_RE = re.compile(r"\bbuy\s*now\b", re.I)

def fetch(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def normalize_url(base: str, href: str) -> str:
    if href.startswith(("http://", "https://")): return href
    if href.startswith("/"):
        from urllib.parse import urlparse, urlunparse
        p = urlparse(base)
        return urlunparse((p.scheme, p.netloc, href, "", "", ""))
    from urllib.parse import urljoin
    return urljoin(base, href)

def collapse_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()

def product_cards(soup: BeautifulSoup):
    # Only catalog tiles
    cards = soup.select("ul.products li.product, li.product, article.product, .wc-block-grid__product")
    return cards

def card_is_oos(card) -> bool:
    if card.select_one(".outofstock, .stock.out-of-stock, .badge.out-of-stock"):
        return True
    return "out of stock" in collapse_text(card.get_text(" ", strip=True)).lower()

def card_has_buy_button(card) -> bool:
    """
    Strict: tile must contain an enabled add-to-cart/buy button INSIDE the tile.
    We purposely only accept buttons/links with WooCommerce add-to-cart class
    or a 'BUY NOW' label inside this tile. Title/image links do not count.
    """
    # Primary: WooCommerce add-to-cart button in catalog
    for b in card.select("a.add_to_cart_button, button.add_to_cart_button"):
        if b.get("aria-disabled", "").lower() == "true": continue
        if b.has_attr("disabled"): continue
        if "disabled" in (b.get("class") or []): continue
        return True

    # Fallback: text says BUY NOW on a button-like element inside the tile
    for b in card.select("a.button, button, .woocommerce a.button, .btn, .button"):
        txt = collapse_text(b.get_text(" ", strip=True)).lower()
        if BUY_TXT_RE.search(txt):
            if b.get("aria-disabled", "").lower() == "true": continue
            if b.has_attr("disabled"): continue
            if "disabled" in (b.get("class") or []): continue
            return True

    return False

def extract_name_url(card, base_url):
    name = None
    for sel in [".woocommerce-loop-product__title", ".product-title", "h2", "h3", "h4"]:
        t = card.select_one(sel)
        if t and t.get_text(strip=True):
            name = t.get_text(strip=True); break
    if not name:
        img = card.select_one("img[alt]")
        if img and img.get("alt"):
            name = img.get("alt").strip()
    a = card.select_one("a.woocommerce-LoopProduct-link, a[href]")
    url = normalize_url(base_url, a["href"]) if a else base_url
    return name, url

def parse_catalog(html_text: str, base_url: str):
    soup = BeautifulSoup(html_text, "html.parser")
    cards = product_cards(soup)

    in_list, oos_list = [], []
    seen = set()

    for c in cards:
        name, url = extract_name_url(c, base_url)
        if not name:
            continue

        oos = card_is_oos(c)
        buy = card_has_buy_button(c)
        in_stock = buy and not oos  # prefer OOS if both appear

        key = name.strip().lower()
        if key in seen:
            continue
        seen.add(key)

        (in_list if in_stock else oos_list if oos else oos_list).append(
            {"name": name, "url": url, "in_stock": in_stock}
        )

    return in_list, oos_list

def build_message(in_stock, out_stock, checked_url: str) -> str:
    lines = [
        "🧺 *Healthfully Farm — Availability*",
        f"_Checked:_ {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        f"_Source:_ {checked_url}",
        "",
        f"✅ In stock ({len(in_stock)}):",
    ]
    if not in_stock:
        lines.append("• _Nothing right now_")
    else:
        for p in in_stock:
            lines.append(f"• [{shorten(p['name'], width=60, placeholder='…')}]({p['url']})")
    lines += ["", f"❌ Out of stock ({len(out_stock)}):"]
    if not out_stock:
        lines.append("• _None shown_")
    else:
        for p in out_stock:
            lines.append(f"• {shorten(p['name'], width=60, placeholder='…')}")
    return "\n".join(lines)


def telegram_send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(api, json={
        "chat_id": CHAT_ID, "text": text,
        "parse_mode": "Markdown", "disable_web_page_preview": True
    }, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def main():
    html = fetch(SHOP_URL)                # only the catalog page
    in_stock, out_stock = parse_catalog(html, SHOP_URL)
    msg = build_message(in_stock, out_stock, SHOP_URL)
    print(msg)
    telegram_send(msg)

if __name__ == "__main__":
    main()
