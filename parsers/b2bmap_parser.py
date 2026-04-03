from __future__ import annotations

"""
Парсер товаров B2BMAP.

Актуальный URL поиска:  /products/search?keyword=<query>
Карточки:  .product-list-card
Заголовок: .product-list-view-title
Картинка:  .product-img
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

try:
    from stealth_utils import apply_stealth
except ImportError:
    apply_stealth = None


@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("b2bmap_results.json")
    headless: bool = True
    navigation_timeout: int = 30_000
    parse_delay: int = 3_000
    proxy: Optional[dict] = None


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "B2BMAP"
    supplier: Optional[str] = None
    price: Optional[str] = None
    image_url: Optional[str] = None


BASE = "https://b2bmap.com"

_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = {runtime: {}};
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
"""

_NAV_JUNK = frozenset({
    "products", "home", "b2bmap", "sign in", "login", "register",
    "post buying lead", "showcase your products now", "showcase your products",
    "post buy requirement", "premium member", "free member",
    "help", "faq", "sitemap", "categories", "all categories",
    "terms of use", "privacy policy", "copyright", "contact us",
    "about us", "browse categories", "member login",
    "search products & suppliers", "product directory: browse products by name",
    "post your products", "for supplier", "for seller",
})


def _clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    s = " ".join(text.replace("\u00a0", " ").split()).strip()
    return s or None


def _is_junk(title: str) -> bool:
    tl = title.lower().strip()
    if tl in _NAV_JUNK or len(tl) < 5:
        return True
    for j in ("post buy", "showcase your", "premium member", "free member",
              "sign in", "login", "register", "product directory", "browse product",
              "member login", "post your"):
        if j in tl:
            return True
    return False


def _is_nav_url(url: str) -> bool:
    ul = url.lower()
    for seg in ("/pricing", "/login", "/signin", "/register", "/signup",
                "/contact", "/about", "/faq", "/help", "/sitemap",
                "/terms", "/privacy", "/member", "/myzone"):
        if seg in ul:
            return True
    return False


def collect_offers(page: Page, max_items: int = 40) -> List[TenderResult]:
    results: List[TenderResult] = []
    seen = set()

    cards = page.locator(".product-list-card")
    count = cards.count()
    if count > 0:
        for i in range(min(count, max_items)):
            try:
                card = cards.nth(i)
                title_el = card.locator(".product-list-view-title, a.product-list-view-title")
                title = ""
                url = ""
                if title_el.count() > 0:
                    title = _clean(title_el.first.inner_text(timeout=1000)) or ""
                    url = title_el.first.get_attribute("href") or ""

                if not title or not url:
                    link = card.locator("a[href]").first
                    if not url:
                        url = link.get_attribute("href") or ""
                    if not title:
                        title = _clean(link.inner_text(timeout=1000)) or ""

                if not title or not url or _is_junk(title) or _is_nav_url(url):
                    continue

                if url.startswith("/"):
                    url = BASE + url

                if url in seen:
                    continue
                seen.add(url)

                img_url = None
                img = card.locator("img.product-img, img")
                if img.count() > 0:
                    src = img.first.get_attribute("src") or img.first.get_attribute("data-src") or ""
                    if src and "icon" not in src.lower() and not src.endswith(".svg"):
                        if src.startswith("/"):
                            src = BASE + src
                        img_url = src

                supplier = None
                sup_el = card.locator("a[href*='/company'], a[href*='products'], .company-name, .supplier")
                if sup_el.count() > 0:
                    for j in range(min(sup_el.count(), 3)):
                        href_s = sup_el.nth(j).get_attribute("href") or ""
                        if "/products" in href_s and "/search" not in href_s:
                            s_text = _clean(sup_el.nth(j).inner_text(timeout=500))
                            if s_text and not _is_junk(s_text) and "more product" not in s_text.lower():
                                supplier = s_text
                                break

                desc = None
                desc_el = card.locator("p, .product-description, .desc")
                if desc_el.count() > 0:
                    desc = _clean(desc_el.first.inner_text(timeout=500))

                results.append(TenderResult(
                    title=title,
                    url=url,
                    supplier=supplier,
                    image_url=img_url,
                ))
            except Exception:
                continue
        return results

    links = page.locator("a[href*='/products/']")
    count = links.count()
    for i in range(min(count, 100)):
        try:
            a = links.nth(i)
            href = a.get_attribute("href") or ""
            txt = _clean(a.inner_text(timeout=500))
            if not href or not txt or _is_junk(txt) or _is_nav_url(href):
                continue
            if href == f"{BASE}/products" or href == f"{BASE}/products/":
                continue
            if "/search" in href:
                continue
            url = href if href.startswith("http") else (BASE + href)
            if url in seen:
                continue
            seen.add(url)
            results.append(TenderResult(title=txt, url=url))
            if len(results) >= max_items:
                break
        except Exception:
            continue

    return results


def _make_browser(p, cfg):
    browser = p.chromium.launch(
        headless=cfg.headless,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
    )
    ctx_kw = dict(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="en-US",
    )
    if cfg.proxy:
        ctx_kw["proxy"] = cfg.proxy
    context = browser.new_context(**ctx_kw)
    context.add_init_script(_INIT_SCRIPT)
    return browser, context


def run_search(cfg: SearchConfig) -> List[TenderResult]:
    print(f"[B2BMAP] query='{cfg.query}' headless={cfg.headless}")
    with sync_playwright() as p:
        browser, context = _make_browser(p, cfg)
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)
        page.set_default_timeout(cfg.navigation_timeout)
        collected: List[TenderResult] = []
        try:
            q = urllib.parse.quote(cfg.query)
            url = f"{BASE}/products/search?keyword={q}"
            page.goto(url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
            page.wait_for_timeout(cfg.parse_delay)
            collected = collect_offers(page)
            print(f"[B2BMAP] '{cfg.query}' -> {len(collected)} items")
        except Exception as e:
            print(f"[B2BMAP] error: {e}", file=sys.stderr)
        finally:
            context.close()
            browser.close()
        return collected


def run_search_batch(queries: List[str], cfg: SearchConfig) -> Dict[str, list]:
    """Batch: one browser, multiple queries."""
    result_map: Dict[str, list] = {}
    print(f"[B2BMAP batch] {len(queries)} queries, headless={cfg.headless}")
    with sync_playwright() as p:
        browser, context = _make_browser(p, cfg)
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)
        page.set_default_timeout(cfg.navigation_timeout)

        for qi, q in enumerate(queries):
            print(f"[B2BMAP batch] [{qi+1}/{len(queries)}] '{q}'")
            try:
                encoded = urllib.parse.quote(q)
                url = f"{BASE}/products/search?keyword={encoded}"
                page.goto(url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
                page.wait_for_timeout(cfg.parse_delay)
                items = collect_offers(page)
                for it in items:
                    it.source = "B2BMAP"
                result_map[q] = [asdict(r) for r in items]
                print(f"[B2BMAP batch]   -> {len(items)} items")
            except Exception as e:
                print(f"[B2BMAP batch]   error: {e}")
                traceback.print_exc()
                result_map[q] = []
                try:
                    page.close()
                    page = context.new_page()
                    page.set_default_navigation_timeout(cfg.navigation_timeout)
                    page.set_default_timeout(cfg.navigation_timeout)
                except Exception:
                    pass
            time.sleep(1)

        try:
            context.close()
            browser.close()
        except Exception:
            pass

    return result_map


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="B2BMAP parser")
    parser.add_argument("query", help="Search query")
    parser.add_argument("-o", "--output", type=Path, default=Path("b2bmap_results.json"))
    parser.add_argument("--headless", action="store_true", default=True)
    args = parser.parse_args()
    cfg = SearchConfig(query=args.query, output=args.output, headless=args.headless)
    results = run_search(cfg)
    data = [asdict(r) for r in results]
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(data)} to {args.output}")
