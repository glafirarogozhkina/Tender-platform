# -*- coding: utf-8 -*-
"""
Парсер товаров с DHgate.com — использует извлечение из __NEXT_DATA__ JSON
(наиболее надёжный метод) с DOM-fallback через BeautifulSoup.

Интерфейс: run_search(cfg) -> List[TenderResult]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

try:
    from stealth_utils import apply_stealth
except ImportError:
    apply_stealth = None

STEALTH_JS_INLINE = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const p = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' },
            ];
            p.refresh = () => {};
            return p;
        }
    });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    window.chrome = { runtime: {} };
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
}
"""


@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("dhgate_results.json")
    headless: bool = True
    navigation_timeout: int = 30_000
    parse_delay: int = 3_000
    proxy: Optional[dict] = None


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "DHGATE.COM"
    price: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    shop_name: Optional[str] = None
    image_url: Optional[str] = None
    itemcode: Optional[str] = None


BASE_URL = "https://www.dhgate.com"
SEARCH_URL = "https://www.dhgate.com/wholesale/search.do"


def _parse_price_us(price_text: Optional[str]) -> tuple:
    if not price_text:
        return None, None
    text = price_text.replace(",", ".").strip()
    numbers = re.findall(r"[\d.]+", text)
    if len(numbers) >= 2:
        try:
            return float(numbers[0]), float(numbers[1])
        except ValueError:
            pass
    if len(numbers) == 1:
        try:
            v = float(numbers[0])
            return v, v
        except ValueError:
            pass
    return None, None


def _extract_from_next_data(html: str) -> List[TenderResult]:
    """Extract products from __NEXT_DATA__ JSON embedded in the page."""
    if not BS4_AVAILABLE:
        idx = html.find('id="__NEXT_DATA__"')
        if idx == -1:
            return []
        start = html.find(">", idx)
        end = html.find("</script>", start)
        if start == -1 or end == -1:
            return []
        script_text = html[start + 1:end]
    else:
        soup = BeautifulSoup(html, "lxml" if _lxml_ok() else "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return []
        script_text = script.string

    try:
        data = json.loads(script_text)
    except json.JSONDecodeError:
        return []

    raw_products = (
        data.get("props", {})
        .get("pageProps", {})
        .get("data", {})
        .get("totalProducts", [])
    )

    results: List[TenderResult] = []
    for item in raw_products:
        title = item.get("productname", "")
        if not title:
            continue
        url = item.get("productDurl") or item.get("productDetailUrl", "")
        price_raw = item.get("price", "")
        image = item.get("seo300ImagePath") or item.get("bigimagepath", "")
        seller = item.get("storeName") or ""
        itemcode = item.get("itemcode", "")

        if image and image.startswith("//"):
            image = "https:" + image
        if url and not url.startswith("http"):
            url = "https://www.dhgate.com" + url

        price_min, price_max = _parse_price_us(str(price_raw) if price_raw else None)

        results.append(
            TenderResult(
                title=title,
                url=url,
                price=str(price_raw) if price_raw else None,
                price_min=price_min,
                price_max=price_max,
                shop_name=seller or None,
                image_url=image or None,
                itemcode=str(itemcode) if itemcode else None,
            )
        )

    return results


def _parse_dom_fallback(html: str) -> List[TenderResult]:
    """Fallback: parse product cards from the DOM using BeautifulSoup."""
    if not BS4_AVAILABLE:
        return []
    soup = BeautifulSoup(html, "lxml" if _lxml_ok() else "html.parser")
    results: List[TenderResult] = []
    seen = set()

    for link in soup.select("a[href*='/product/']"):
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if not title or len(title) < 10:
            continue
        if not href.startswith("http"):
            href = "https://www.dhgate.com" + href

        if href in seen:
            continue
        seen.add(href)

        price_raw = None
        parent = link.find_parent("div", recursive=True)
        if parent:
            price_el = parent.find(
                lambda tag: tag.name == "span" and "$" in tag.get_text()
            )
            if price_el:
                price_raw = price_el.get_text(strip=True)

        price_min, price_max = _parse_price_us(price_raw)
        results.append(
            TenderResult(
                title=title,
                url=href,
                price=price_raw,
                price_min=price_min,
                price_max=price_max,
            )
        )

    return results


def _lxml_ok() -> bool:
    try:
        import lxml  # noqa: F401
        BeautifulSoup("<html></html>", "lxml")
        return True
    except Exception:
        return False


def run_search(cfg: SearchConfig) -> List[TenderResult]:
    """Основная функция поиска на DHgate.com."""
    print(f"[DHgate] Запуск Playwright (headless={cfg.headless}, channel=chrome)...")

    all_products: List[TenderResult] = []

    with sync_playwright() as p:
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
        ]
        browser = p.chromium.launch(
            headless=cfg.headless,
            channel="chrome",
            args=launch_args,
        )

        ctx_kwargs = dict(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        if cfg.proxy:
            ctx_kwargs["proxy"] = cfg.proxy
            print(f"[proxy] DHgate: {cfg.proxy.get('server', '?')}")

        context = browser.new_context(**ctx_kwargs)

        if apply_stealth:
            apply_stealth(context)
        else:
            context.add_init_script(STEALTH_JS_INLINE)

        page = context.new_page()

        try:
            print("[DHgate] Visiting homepage to establish session...")
            try:
                page.goto(BASE_URL, timeout=30_000)
            except Exception:
                pass
            page.wait_for_timeout(4000)

            for page_num in range(1, cfg.pages + 1):
                q = urllib.parse.quote_plus(cfg.query)
                url = (
                    f"{SEARCH_URL}?searchkey={q}"
                    f"&searchSource=sort&page={page_num}"
                )
                print(f"[DHgate] Page {page_num}/{cfg.pages}: {url}")

                try:
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    except Exception:
                        pass

                    # Wait for __NEXT_DATA__ or substantial content
                    for _ in range(20):
                        page.wait_for_timeout(3000)
                        html = page.content()
                        if "__NEXT_DATA__" in html or len(html) > 500_000:
                            break

                    for _ in range(3):
                        page.evaluate("window.scrollBy(0, window.innerHeight)")
                        page.wait_for_timeout(random.randint(800, 1500))

                    html = page.content()

                    products = _extract_from_next_data(html)
                    if not products:
                        products = _parse_dom_fallback(html)

                    if not products:
                        print(f"[DHgate] No products on page {page_num}, stopping.")
                        break

                    all_products.extend(products)
                    print(f"[DHgate] Found {len(products)} products on page {page_num}")

                    page.wait_for_timeout(random.randint(2000, 5000))

                except Exception as e:
                    print(f"[DHgate] Error on page {page_num}: {e}")
                    break

        except PlaywrightTimeoutError as e:
            print(f"[DHgate] Timeout: {e}", file=sys.stderr)
        finally:
            context.close()
            browser.close()

    print(f"[DHgate] Total: {len(all_products)} products")
    return all_products


def save_results(path: Path, results: List[TenderResult]) -> None:
    data = [asdict(r) for r in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: List[str]) -> SearchConfig:
    parser = argparse.ArgumentParser(
        description="Парсер товаров DHgate.com по ключевым словам"
    )
    parser.add_argument("query", help="Поисковый запрос (напр.: перчатки)")
    parser.add_argument("-p", "--pages", type=int, default=1)
    parser.add_argument("-o", "--output", type=Path, default=Path("dhgate_results.json"))
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--timeout", type=int, default=60_000)
    args = parser.parse_args(argv)
    return SearchConfig(
        query=args.query,
        pages=args.pages,
        output=args.output,
        headless=args.headless,
        navigation_timeout=args.timeout,
    )


def main(argv: List[str]) -> int:
    cfg = parse_args(argv)
    print(f"DHgate.com. Запрос: {cfg.query}, страниц: {cfg.pages}")
    try:
        results = run_search(cfg)
        save_results(cfg.output, results)
        print(f"Сохранено: {len(results)} предложений в {cfg.output}")
        return 0
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
