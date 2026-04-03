#!/usr/bin/env python3
"""
Парсер B2B-Center Маркет (https://www.b2b-center.ru/market/)
Поиск по ключевым словам: input#f_keyword, GET ?f_keyword=...&searching=1&trade=all
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ---------------------------
# Конфигурация и структуры данных
# ---------------------------

@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("b2b_results.json")
    headless: bool = True
    navigation_timeout: int = 60_000  # ms
    parse_delay: int = 3_000  # ms


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "B2B-CENTER"
    customer: Optional[str] = None
    organizer: Optional[str] = None
    price: Optional[str] = None
    status: Optional[str] = None
    publish_date: Optional[str] = None  # Опубликовано
    deadline: Optional[str] = None      # Актуально до
    region: Optional[str] = None
    tender_id: Optional[str] = None
    law_type: Optional[str] = None
    purchase_type: Optional[str] = None


BASE_URL = "https://www.b2b-center.ru"
MARKET_URL = "https://www.b2b-center.ru/market/"


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return " ".join(value.split()).strip() or None


def _normalize_url(href: str) -> str:
    if not href or not href.strip():
        return ""
    href = href.strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return BASE_URL + href
    return BASE_URL + "/" + href


def collect_page_results(page) -> List[TenderResult]:
    """Сбор результатов со страницы поиска B2B-Center."""
    results: List[TenderResult] = []
    # Таблица: table.table.table-hover.table-filled.search-results
    table = page.locator("table.search-results, table.table.search-results")
    if table.count() == 0:
        return results

    rows = table.locator("tbody tr")
    n = rows.count()
    for i in range(n):
        row = rows.nth(i)
        try:
            # Ссылка на процедуру — обычно в первой колонке (Название процедуры)
            link = row.locator("a[href*='/market/'], a[href*='/app/market']").first
            if link.count() == 0:
                continue
            url = link.get_attribute("href") or ""
            title = _clean(link.inner_text(timeout=2_000))
            if not title or len(title) < 3:
                continue
            url = _normalize_url(url)

            # Номер процедуры из текста, например "Запрос предложений № 4325229"
            tender_id = None
            id_match = re.search(r"№\s*(\d+)", title)
            if id_match:
                tender_id = id_match.group(1)

            # Организатор — вторая колонка, ссылка на /firms/ или текст
            organizer = None
            org_cell = row.locator("td").nth(1)
            if org_cell.count() > 0:
                org_link = org_cell.locator("a").first
                if org_link.count() > 0:
                    organizer = _clean(org_link.inner_text(timeout=1_000))
                else:
                    organizer = _clean(org_cell.inner_text(timeout=1_000))

            # Опубликовано — третья колонка
            publish_date = None
            pub_cell = row.locator("td").nth(2)
            if pub_cell.count() > 0:
                publish_date = _clean(pub_cell.inner_text(timeout=1_000))

            # Актуально до — четвёртая колонка
            deadline = None
            dead_cell = row.locator("td").nth(3)
            if dead_cell.count() > 0:
                deadline = _clean(dead_cell.inner_text(timeout=1_000))

            results.append(
                TenderResult(
                    title=title,
                    url=url,
                    organizer=organizer,
                    publish_date=publish_date,
                    deadline=deadline,
                    tender_id=tender_id,
                )
            )
        except Exception as e:
            continue
    return results


def go_next_page(page) -> bool:
    """Переход на следующую страницу (пагинация)."""
    next_links = page.locator("a:has-text('›'), a:has-text('»'), .pagination a.next, a[rel='next']")
    for i in range(next_links.count()):
        try:
            el = next_links.nth(i)
            if el.is_visible(timeout=1_000):
                el.click()
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
                page.wait_for_timeout(2_000)
                return True
        except Exception:
            continue
    return False


def run_search(cfg: SearchConfig) -> List[TenderResult]:
    """Основная функция поиска на B2B-Center Маркет."""
    print(f"Запуск Playwright Chromium (headless={cfg.headless})...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=cfg.headless,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
            ]
        )
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            locale='ru-RU',
        )
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)
        page.set_default_timeout(cfg.navigation_timeout)

        collected: List[TenderResult] = []

        try:
            # Поиск через GET с параметрами f_keyword, searching=1, trade=all
            query_encoded = urllib.parse.quote(cfg.query)
            search_url = f"{MARKET_URL}?f_keyword={query_encoded}&searching=1&trade=all"
            print(f"Загрузка: {search_url}")
            page.goto(search_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
            page.wait_for_timeout(3_000)

            for page_num in range(cfg.pages):
                print(f"Страница {page_num + 1}/{cfg.pages}")
                page_results = collect_page_results(page)
                collected.extend(page_results)
                print(f"  Собрано: {len(page_results)} (всего: {len(collected)})")

                if page_num + 1 >= cfg.pages:
                    break
                if not go_next_page(page):
                    break
                time.sleep(cfg.parse_delay / 1000)

        except PlaywrightTimeoutError as e:
            print(f"Таймаут: {e}")
        finally:
            context.close()
            browser.close()

        return collected


def save_results(path: Path, results: List[TenderResult]) -> None:
    """Сохранение в JSON."""
    data = [asdict(r) for r in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: List[str]) -> SearchConfig:
    parser = argparse.ArgumentParser(description="Парсер B2B-Center Маркет")
    parser.add_argument("query", help="Поисковый запрос (ключевые слова)")
    parser.add_argument("-p", "--pages", type=int, default=1, help="Количество страниц")
    parser.add_argument("-o", "--output", type=Path, default=Path("b2b_results.json"))
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
    print(f"B2B-Center Маркет. Запрос: {cfg.query}, страниц: {cfg.pages}")
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
