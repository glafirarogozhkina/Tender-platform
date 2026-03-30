from __future__ import annotations

"""
Парсер товаров B2BChinaSources.

Актуальный URL поиска:  /search.php?sk=<query>
Результаты в таблице, ссылки на продукты — .html
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
    output: Path = Path("b2bchinasources_results.json")
    headless: bool = True
    navigation_timeout: int = 30_000
    parse_delay: int = 3_000
    proxy: Optional[dict] = None


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "B2BCHINASOURCES"
    supplier: Optional[str] = None
    price: Optional[str] = None
    image_url: Optional[str] = None


BASE = "https://www.b2bchinasources.com"

_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = {runtime: {}};
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
"""

_NAV_JUNK = frozenset({
    "register", "sign in", "help", "inquiry basket", "mutual link",
    "products", "companies", "trade leads", "my b2b", "home",
    "china manufacturers", "product search", "company search",
    "category search", "search tips", "site map", "about us",
    "contact us", "privacy policy", "terms", "select all", "clear all",
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
    for j in ("select", "sign in", "register", "search tip", "mutual link",
              "inquiry basket", "china manufacturers", "view by"):
        if j in tl:
            return True
    return False


def collect_offers(page: Page, max_items: int = 40) -> List[TenderResult]:
    results: List[TenderResult] = []
    seen = set()

    links = page.locator("a[href$='.html']")
    count = links.count()
    for i in range(min(count, 200)):
        try:
            a = links.nth(i)
            href = a.get_attribute("href") or ""
            if not href or "/China-Manufacturers" not in href:
                continue
            txt = _clean(a.inner_text(timeout=500))
            if not txt or _is_junk(txt):
                continue
            url = href if href.startswith("http") else (BASE + href)
            key = url
            if key in seen:
                continue
            seen.add(key)

            img_url = None
            parent = a.locator("xpath=ancestor::tr[1]")
            if parent.count() > 0:
                img = parent.locator("img")
                if img.count() > 0:
                    src = img.first.get_attribute("src") or ""
                    if src and not src.endswith(".gif") and "spacer" not in src:
                        if src.startswith("/"):
                            src = BASE + src
                        elif not src.startswith("http"):
                            src = BASE + "/" + src
                        img_url = src

            supplier = None
            parent_td = a.locator("xpath=ancestor::td[1]")
            if parent_td.count() > 0:
                td_text = _clean(parent_td.inner_text(timeout=1000))
                if td_text:
                    m = re.search(r'from\s+(.+?)\s+for\b', td_text)
                    if m:
                        supplier = m.group(1).strip()

            results.append(TenderResult(
                title=txt,
                url=url,
                supplier=supplier,
                image_url=img_url,
            ))
            if len(results) >= max_items:
                break
        except Exception:
            continue

    if not results:
        trs = page.locator("table tr")
        tr_count = trs.count()
        for i in range(min(tr_count, 100)):
            try:
                tr = trs.nth(i)
                tr_links = tr.locator("a[href]")
                for j in range(min(tr_links.count(), 5)):
                    a = tr_links.nth(j)
                    href = a.get_attribute("href") or ""
                    txt = _clean(a.inner_text(timeout=500))
                    if not href or not txt or _is_junk(txt) or len(txt) < 5:
                        continue
                    if "/product" not in href.lower() and ".html" not in href.lower():
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
            if len(results) >= max_items:
                break

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
    print(f"[B2BChinaSources] query='{cfg.query}' headless={cfg.headless}")
    with sync_playwright() as p:
        browser, context = _make_browser(p, cfg)
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)
        page.set_default_timeout(cfg.navigation_timeout)
        collected: List[TenderResult] = []
        try:
            q = urllib.parse.quote(cfg.query)
            url = f"{BASE}/search.php?sk={q}"
            page.goto(url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
            page.wait_for_timeout(cfg.parse_delay)
            collected = collect_offers(page)
            print(f"[B2BChinaSources] '{cfg.query}' -> {len(collected)} items")
        except Exception as e:
            print(f"[B2BChinaSources] error: {e}", file=sys.stderr)
        finally:
            context.close()
            browser.close()
        return collected


def run_search_batch(queries: List[str], cfg: SearchConfig) -> Dict[str, list]:
    """Batch: one browser, multiple queries."""
    result_map: Dict[str, list] = {}
    print(f"[B2BChinaSources batch] {len(queries)} queries, headless={cfg.headless}")
    with sync_playwright() as p:
        browser, context = _make_browser(p, cfg)
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)
        page.set_default_timeout(cfg.navigation_timeout)

        for qi, q in enumerate(queries):
            print(f"[B2BChinaSources batch] [{qi+1}/{len(queries)}] '{q}'")
            try:
                encoded = urllib.parse.quote(q)
                url = f"{BASE}/search.php?sk={encoded}"
                page.goto(url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
                page.wait_for_timeout(cfg.parse_delay)
                items = collect_offers(page)
                for it in items:
                    it.source = "B2BCHINASOURCES"
                result_map[q] = [asdict(r) for r in items]
                print(f"[B2BChinaSources batch]   -> {len(items)} items")
            except Exception as e:
                print(f"[B2BChinaSources batch]   error: {e}")
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
    parser = argparse.ArgumentParser(description="B2BChinaSources parser")
    parser.add_argument("query", help="Search query")
    parser.add_argument("-o", "--output", type=Path, default=Path("b2bchinasources_results.json"))
    parser.add_argument("--headless", action="store_true", default=True)
    args = parser.parse_args()
    cfg = SearchConfig(query=args.query, output=args.output, headless=args.headless)
    results = run_search(cfg)
    data = [asdict(r) for r in results]
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(data)} to {args.output}")
