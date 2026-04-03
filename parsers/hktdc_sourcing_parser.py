from __future__ import annotations

"""
Парсер товаров HKTDC Sourcing.

У HKTDC часто меняется разметка и бывают защиты. Поэтому алгоритм:
- открываем базовую страницу sourcing
- ищем поле поиска по набору селекторов, вводим query, Enter
- собираем карточки по "мягким" селекторам
"""

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

try:
    from stealth_utils import apply_stealth
except ImportError:
    apply_stealth = None


_local_appdata = os.environ.get("LOCALAPPDATA")
if _local_appdata:
    _default_pw_path = str(Path(_local_appdata) / "ms-playwright")
    _pw_path = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").lower()
    if (not _pw_path) or ("cursor-sandbox-cache" in _pw_path):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _default_pw_path


@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("hktdc_sourcing_results.json")
    headless: bool = True
    navigation_timeout: int = 30_000
    parse_delay: int = 3_000
    proxy: Optional[dict] = None


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "HKTDC SOURCING"
    supplier: Optional[str] = None
    price: Optional[str] = None
    image_url: Optional[str] = None


BASE_URLS = [
    "https://sourcing.hktdc.com/en",
    "https://sourcing.hktdc.com",
]


def _clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    s = " ".join(text.replace("\u00a0", " ").split()).strip()
    return s or None


def _safe_inner_text(loc: Locator, timeout: int = 2_000) -> Optional[str]:
    try:
        if loc.count() == 0:
            return None
        return loc.first.inner_text(timeout=timeout)
    except Exception:
        return None


def _safe_attribute(loc: Locator, name: str, timeout: int = 2_000) -> Optional[str]:
    try:
        if loc.count() == 0:
            return None
        return loc.first.get_attribute(name, timeout=timeout)
    except Exception:
        return None


def _find_search_input(page: Page) -> Optional[Locator]:
    selectors = [
        "input[type='search']",
        "input[name*='keyword' i]",
        "input[name*='search' i]",
        "input[placeholder*='Search' i]",
        "input[placeholder*='keyword' i]",
        "input[aria-label*='Search' i]",
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible(timeout=1_500):
                return loc
        except Exception:
            continue
    return None


def collect_offers(page: Page, max_items: int = 40) -> List[TenderResult]:
    results: List[TenderResult] = []
    card_selectors = [
        "[data-testid*='product']",
        ".product-item",
        ".product-card",
        "div:has(a[href*='product'])",
        "li:has(a[href*='product'])",
    ]
    cards = None
    for sel in card_selectors:
        loc = page.locator(sel)
        if loc.count() > 0:
            cards = loc
            break
    if cards is None:
        # fallback: ссылки, похожие на товары
        links = page.locator("a[href*='product']")
        seen = set()
        count = min(links.count(), max_items)
        for i in range(count):
            a = links.nth(i)
            href = _safe_attribute(a, "href") or ""
            title = _clean(_safe_inner_text(a))
            if not href or not title:
                continue
            url = href if href.startswith("http") else ("https://sourcing.hktdc.com" + href)
            key = (url, title)
            if key in seen:
                continue
            seen.add(key)
            results.append(TenderResult(title=title, url=url))
        return results

    count = min(cards.count(), max_items)
    for i in range(count):
        c = cards.nth(i)
        try:
            link = c.locator("a[href]").first
            href = _safe_attribute(link, "href") or ""
            if not href:
                continue
            url = href if href.startswith("http") else ("https://sourcing.hktdc.com" + href)

            title = _clean(_safe_inner_text(c.locator("h3, h2").first) or _safe_inner_text(link))
            if not title:
                continue

            supplier = _clean(_safe_inner_text(c.locator(".supplier, .company, .supplier-name, a[href*='supplier']").first))
            price = _clean(_safe_inner_text(c.locator(".price, [class*='price']").first))
            image_url = _safe_attribute(c.locator("img").first, "src") or _safe_attribute(c.locator("img").first, "data-src")

            results.append(TenderResult(title=title, url=url, supplier=supplier, price=price, image_url=image_url))
        except Exception:
            continue

    return results


def run_search(cfg: SearchConfig) -> List[TenderResult]:
    print(f"Запуск Playwright для HKTDC (headless={cfg.headless})...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=cfg.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx_kwargs = dict(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        if cfg.proxy:
            ctx_kwargs["proxy"] = cfg.proxy
            print(f"[proxy] HKTDC: используется прокси {cfg.proxy.get('server', '?')}")
        context = browser.new_context(**ctx_kwargs)
        if apply_stealth:
            apply_stealth(context)
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)
        page.set_default_timeout(cfg.navigation_timeout)

        collected: List[TenderResult] = []
        try:
            base_ok = False
            for base in BASE_URLS:
                try:
                    page.goto(base, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
                    page.wait_for_timeout(2_000)
                    base_ok = True
                    break
                except Exception:
                    continue
            if not base_ok:
                return []

            try:
                if page.locator("iframe[src*='_Incapsula_Resource']").count() > 0 or page.locator("text=/incapsula|cloudflare|captcha|access denied|verify/i").count() > 0:
                    print("⛔ HKTDC: похоже на антибот/капчу, результаты могут быть недоступны.", file=sys.stderr)
            except Exception:
                pass

            search = _find_search_input(page)
            if search:
                search.click()
                search.fill(cfg.query)
                search.press("Enter")
                page.wait_for_load_state("domcontentloaded", timeout=cfg.navigation_timeout)
                page.wait_for_timeout(cfg.parse_delay)
            else:
                # fallback: остаёмся на странице, пробуем собрать что есть
                page.wait_for_timeout(cfg.parse_delay)

            collected.extend(collect_offers(page))
        except PlaywrightTimeoutError as e:
            print(f"Таймаут HKTDC: {e}", file=sys.stderr)
        finally:
            context.close()
            browser.close()

        return collected


def save_results(path: Path, results: List[TenderResult]) -> None:
    data = [asdict(r) for r in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: List[str]) -> SearchConfig:
    parser = argparse.ArgumentParser(description="Парсер HKTDC Sourcing по ключевым словам")
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-p", "--pages", type=int, default=1)
    parser.add_argument("-o", "--output", type=Path, default=Path("hktdc_sourcing_results.json"))
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--timeout", type=int, default=60_000)
    args = parser.parse_args(argv)
    return SearchConfig(query=args.query, pages=args.pages, output=args.output, headless=args.headless, navigation_timeout=args.timeout)


def main(argv: List[str]) -> int:
    cfg = parse_args(argv)
    try:
        results = run_search(cfg)
        save_results(cfg.output, results)
        print(f"Сохранено: {len(results)} в {cfg.output}")
        return 0
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

