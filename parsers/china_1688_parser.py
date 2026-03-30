from __future__ import annotations

"""
Парсер товаров с 1688.com по поисковому запросу.

Логика:
- открываем страницу поиска с параметром keywords=
- забираем карточки из блока div.feeds-wrapper [data-tracker="offer"]
- для каждой карточки вытаскиваем:
  - title
  - url
  - price_cny
  - price_rub (цена * курс)
  - shop_name
  - sold
  - image_url
"""

import argparse
import json
import sys
import time
import urllib.parse
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Page,
    Locator,
)

try:
    from stealth_utils import apply_stealth
except ImportError:
    apply_stealth = None


@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("china_1688_results.json")
    headless: bool = True
    navigation_timeout: int = 30_000  # ms
    parse_delay: int = 3_000  # ms
    cny_to_rub_rate: float = 13.0  # курс юань → руб
    proxy: Optional[dict] = None  # {"server": "http://host:port", "username": "...", "password": "..."}


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "1688.COM"
    price_cny: Optional[float] = None
    price_rub: Optional[float] = None
    shop_name: Optional[str] = None
    sold: Optional[str] = None
    location: Optional[str] = None
    tags: Optional[str] = None
    image_url: Optional[str] = None


BASE_URL = "https://s.1688.com"


def _clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    stripped = " ".join(text.split())
    return stripped or None


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
        return loc.first.get_attribute(name)
    except Exception:
        return None


def _parse_price_cny(price_root: Locator) -> Optional[float]:
    """
    Парсинг цены из блока .price-item:
    <div class="price-item">
        <div>¥</div><div class="text-main">3</div><div>.5</div>
    </div>
    """
    try:
        # Собираем все куски текста внутри блока
        text = price_root.inner_text(timeout=1_000)
        if not text:
            return None
        text = text.replace("¥", "").strip()
        # Убираем лишние символы и пробелы
        text = text.replace(" ", "")
        # Заменяем запятую на точку (на всякий случай)
        text = text.replace(",", ".")
        # Оставляем только цифры и точку
        filtered = "".join(ch for ch in text if (ch.isdigit() or ch == "."))
        if not filtered:
            return None
        return float(filtered)
    except Exception:
        return None


def collect_offers(page: Page, cfg: SearchConfig) -> List[TenderResult]:
    """Сбор карточек товаров с 1688.com."""
    results: List[TenderResult] = []

    # На странице 1688 каждая карточка товара — это ссылка <a data-tracker="offer"> внутри блока .feeds-wrapper.
    # Раньше мы искали внутри неё ещё один <a>, из‑за чего парсер всегда пропускал карточки.
    # Поэтому сразу работаем с самими ссылками‑карточками.
    wrapper = page.locator("div.feeds-wrapper a[data-tracker='offer']")
    count = wrapper.count()
    if count == 0:
        return results

    # Ограничимся первыми 40 предложениями, чтобы не перегружать интерфейс
    max_items = min(count, 40)

    for i in range(max_items):
        card = wrapper.nth(i)  # сам <a data-tracker="offer"> — корень карточки
        try:
            # Внешняя ссылка на товар
            url = _safe_attribute(card, "href") or ""
            if not url:
                continue

            # Заголовок
            title_loc = card.locator(".offer-title-row .title-text div, .offer-title-row .title-text")
            title = _clean(_safe_inner_text(title_loc))
            if not title:
                continue

            # Цена
            price_root = card.locator(".offer-price-row .price-item").first
            price_cny = _parse_price_cny(price_root) if price_root.count() > 0 else None
            price_rub: Optional[float] = None
            if price_cny is not None and cfg.cny_to_rub_rate > 0:
                price_rub = round(price_cny * cfg.cny_to_rub_rate, 2)

            # Магазин / поставщик
            shop_loc = card.locator(".offer-shop-row .col-left .desc-text").first
            shop_name = _clean(_safe_inner_text(shop_loc))

            # Продано / количество
            sold_loc = card.locator(".offer-price-row .offer-desc-item .desc-text, .offer-desc-row .offer-desc-item .desc-text").first
            sold = _clean(_safe_inner_text(sold_loc))

            # Основное изображение
            img_loc = card.locator("img.main-img").first
            image_url = _safe_attribute(img_loc, "src")

            # Теги (например: 退货包运费, 先采后付)
            tag_nodes = card.locator(".offer-tag-row .desc-text")
            tags_list: List[str] = []
            try:
                for j in range(min(tag_nodes.count(), 5)):
                    t = _clean(_safe_inner_text(tag_nodes.nth(j)))
                    if t:
                        tags_list.append(t)
            except Exception:
                pass
            tags = "; ".join(tags_list) if tags_list else None

            results.append(
                TenderResult(
                    title=title,
                    url=url,
                    price_cny=price_cny,
                    price_rub=price_rub,
                    shop_name=shop_name,
                    sold=sold,
                    image_url=image_url,
                    tags=tags,
                )
            )
        except Exception:
            continue

    return results


def run_search(cfg: SearchConfig) -> List[TenderResult]:
    """Основная функция поиска товаров на 1688.com."""
    print(f"Запуск Playwright Chromium для 1688.com (headless={cfg.headless})...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=cfg.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx_kwargs = dict(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        if cfg.proxy:
            ctx_kwargs["proxy"] = cfg.proxy
            print(f"[proxy] 1688.com: прокси {cfg.proxy.get('server', '?')}")
        context = browser.new_context(**ctx_kwargs)
        if apply_stealth:
            apply_stealth(context)
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
            window.chrome = {runtime: {}};
        """)
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)
        page.set_default_timeout(cfg.navigation_timeout)

        collected: List[TenderResult] = []

        try:
            # Формируем URL поиска
            query_encoded = urllib.parse.quote(cfg.query)
            search_url = f"{BASE_URL}/selloffer/offer_search.htm?keywords={query_encoded}&charset=utf8"

            print(f"Загрузка результатов 1688.com для запроса: '{cfg.query}'")
            print(f"URL: {search_url}")

            page.goto(search_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)

            wait_sels = [
                "div.feeds-wrapper a[data-tracker='offer']",
                "a[data-tracker='offer']",
                ".offer-title-row",
            ]
            found = False
            for sel in wait_sels:
                try:
                    page.wait_for_selector(sel, timeout=8_000)
                    if page.locator(sel).count() > 0:
                        found = True
                        break
                except PlaywrightTimeoutError:
                    continue
            if not found:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2_000)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(1_000)

            page.wait_for_timeout(cfg.parse_delay)
            collected = collect_offers(page, cfg)
            print(f"✅ 1688.com: собрано {len(collected)} предложений")

        except PlaywrightTimeoutError as e:
            print(f"Таймаут при работе с 1688.com: {e}", file=sys.stderr)
        finally:
            context.close()
            browser.close()

        return collected


def run_search_batch(queries: List[str], cfg: SearchConfig) -> dict:
    """Batch-поиск: один browser для всех запросов."""
    print(f"Запуск Playwright batch для 1688.com ({len(queries)} запросов)...")
    result_map: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=cfg.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx_kwargs = dict(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        if cfg.proxy:
            ctx_kwargs["proxy"] = cfg.proxy
            print(f"[proxy] 1688 batch: прокси {cfg.proxy.get('server', '?')}")
        context = browser.new_context(**ctx_kwargs)
        if apply_stealth:
            apply_stealth(context)
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
            window.chrome = {runtime: {}};
        """)
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)
        page.set_default_timeout(cfg.navigation_timeout)

        total = 0
        for qi, q in enumerate(queries):
            print(f"  [{qi+1}/{len(queries)}] 1688: '{q[:50]}'")
            try:
                query_encoded = urllib.parse.quote(q)
                search_url = f"{BASE_URL}/selloffer/offer_search.htm?keywords={query_encoded}&charset=utf8"
                page.goto(search_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)

                wait_sels = [
                    "div.feeds-wrapper a[data-tracker='offer']",
                    "a[data-tracker='offer']",
                    ".offer-title-row",
                ]
                found = False
                for sel in wait_sels:
                    try:
                        page.wait_for_selector(sel, timeout=8_000)
                        if page.locator(sel).count() > 0:
                            found = True
                            break
                    except PlaywrightTimeoutError:
                        continue
                if not found:
                    page.evaluate("window.scrollBy(0, 800)")
                    page.wait_for_timeout(2_000)

                page.wait_for_timeout(max(cfg.parse_delay, 2_000))
                items = collect_offers(page, cfg)
                result_map[q] = items
                total += len(items)
                print(f"    -> {len(items)} items")
            except PlaywrightTimeoutError as e:
                print(f"    -> timeout: {e}", file=sys.stderr)
                result_map[q] = []
            except Exception as e:
                print(f"    -> error: {e}", file=sys.stderr)
                result_map[q] = []

        context.close()
        browser.close()

    print(f"✅ 1688 batch: {total} всего по {len(queries)} запросам")
    return result_map


def save_results(path: Path, results: List[TenderResult]) -> None:
    data = [asdict(r) for r in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: List[str]) -> SearchConfig:
    parser = argparse.ArgumentParser(description="Парсер товаров 1688.com по ключевым словам")
    parser.add_argument("query", help="Поисковый запрос (ключевые слова, например: перчатки медицинские)")
    parser.add_argument("-p", "--pages", type=int, default=1, help="Количество страниц (пока используется только 1)")
    parser.add_argument("-o", "--output", type=Path, default=Path("china_1688_results.json"))
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--timeout", type=int, default=60_000)
    parser.add_argument("--cny-to-rub", type=float, default=13.0, help="Курс юань → рубль")
    args = parser.parse_args(argv)
    return SearchConfig(
        query=args.query,
        pages=args.pages,
        output=args.output,
        headless=args.headless,
        navigation_timeout=args.timeout,
        cny_to_rub_rate=args.cny_to_rub,
    )


def main(argv: List[str]) -> int:
    cfg = parse_args(argv)
    print(f"1688.com. Запрос: {cfg.query}, страниц: {cfg.pages}")
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

