#!/usr/bin/env python3
"""
Парсер Сбербанк-АСТ (https://www.sberbank-ast.ru/UnitedPurchaseList.aspx)
Поиск через POST на SearchQuery.aspx с xmlData (elasticrequest, mainSearchBar).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
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
    output: Path = Path("sberbank_ast_results.json")
    headless: bool = False
    navigation_timeout: int = 60_000  # ms
    parse_delay: int = 3_000  # ms
    page_size: int = 20  # результатов на страницу (как на сайте)


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "SBERBANK-AST"
    customer: Optional[str] = None
    organizer: Optional[str] = None
    price: Optional[str] = None
    status: Optional[str] = None
    publish_date: Optional[str] = None
    deadline: Optional[str] = None
    region: Optional[str] = None
    tender_id: Optional[str] = None
    law_type: Optional[str] = None
    purchase_type: Optional[str] = None


BASE_URL = "https://www.sberbank-ast.ru"
LIST_URL = "https://www.sberbank-ast.ru/UnitedPurchaseList.aspx"
SEARCH_URL = "https://www.sberbank-ast.ru/SearchQuery.aspx?name=Main"


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


def _build_xml_data(query: str, from_offset: int, size: int = 20) -> str:
    """Формирует xmlData для POST SearchQuery.aspx (минимальный elasticrequest)."""
    q_esc = _escape_xml(query)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<elasticrequest>
    <personid>0</personid>
    <buid>0</buid>
    <filters>
        <mainSearchBar>
            <value>{q_esc}</value>
            <type>phrase_prefix</type>
            <minimum_should_match>100%</minimum_should_match>
        </mainSearchBar>
        <purchAmount><minvalue></minvalue><maxvalue></maxvalue></purchAmount>
        <PublicDate><minvalue></minvalue><maxvalue></maxvalue></PublicDate>
        <PurchaseStageTerm><value></value><visiblepart></visiblepart></PurchaseStageTerm>
        <SourceTerm><value></value><visiblepart></visiblepart></SourceTerm>
        <RegionNameTerm><value></value><visiblepart></visiblepart></RegionNameTerm>
        <RequestStartDate><minvalue></minvalue><maxvalue></maxvalue></RequestStartDate>
        <RequestDate><minvalue></minvalue><maxvalue></maxvalue></RequestDate>
        <AuctionBeginDate><minvalue></minvalue><maxvalue></maxvalue></AuctionBeginDate>
        <okdp2MultiMatch><value></value><visiblepart></visiblepart></okdp2MultiMatch>
        <productField><value></value><visiblepart></visiblepart></productField>
        <branchField><value></value><visiblepart></visiblepart></branchField>
        <classifier><value></value><visiblepart></visiblepart></classifier>
        <orgCondition><value></value><visiblepart></visiblepart></orgCondition>
        <organizator><value></value><visiblepart></visiblepart></organizator>
        <CustomerCondition><value></value><visiblepart></visiblepart></CustomerCondition>
        <customer><value></value><visiblepart></visiblepart></customer>
        <PurchaseWayTerm><value></value><visiblepart></visiblepart></PurchaseWayTerm>
        <PurchaseTypeNameTerm><value></value><visiblepart></visiblepart></PurchaseTypeNameTerm>
        <BranchNameTerm><value></value><visiblepart></visiblepart></BranchNameTerm>
        <isSharedTerm><value></value><visiblepart></visiblepart></isSharedTerm>
        <isHasComplaint><value></value><visiblepart></visiblepart></isHasComplaint>
        <isPurchCostDetails><value></value><visiblepart></visiblepart></isPurchCostDetails>
        <notificationFeatures><value></value><visiblepart></visiblepart></notificationFeatures>
    </filters>
    <fields>
        <value>TradeSectionId</value><value>purchAmount</value><value>purchCurrency</value><value>purchCodeTerm</value>
        <value>PurchaseTypeName</value><value>purchStateName</value><value>BidStatusName</value><value>OrgName</value>
        <value>SourceTerm</value><value>PublicDate</value><value>RequestDate</value><value>RequestStartDate</value>
        <value>RequestAcceptDate</value><value>EndDate</value><value>CreateRequestHrefTerm</value><value>CreateRequestAlowed</value>
        <value>purchName</value><value>BidName</value><value>SourceHrefTerm</value><value>objectHrefTerm</value>
        <value>needPayment</value><value>IsSMP</value><value>isIncrease</value><value>isHasComplaint</value><value>isPurchCostDetails</value><value>purchType</value>
    </fields>
    <sort><value>default</value><direction></direction></sort>
    <aggregations><empty><filterType>filter_aggregation</filterType></empty></aggregations>
    <size>{size}</size>
    <from>{from_offset}</from>
</elasticrequest>"""


def _escape_xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _parse_response_data(response_body: str) -> List[TenderResult]:
    """Парсит ответ SearchQuery.aspx (JSON с data -> tableXml или rows)."""
    results: List[TenderResult] = []
    try:
        body = json.loads(response_body)
        if body.get("result") != "success":
            return results
        data = body.get("data")
        if data is None:
            return results
        # data может быть строкой с вложенным JSON
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return results
        # Таблица в tableXml (экранированный XML)
        table_xml = data.get("tableXml") if isinstance(data, dict) else None
        if table_xml:
            # Раскодируем Unicode-экранирование
            if "\\u003c" in table_xml:
                table_xml = table_xml.encode().decode("unicode_escape")
            table_xml = table_xml.replace("\\r\\n", "\n").replace("\\n", "\n")
            results = _parse_table_xml(table_xml)
        # Альтернатива: массив строк в data
        if not results and isinstance(data, dict) and "datarow" in str(data).lower():
            raw = str(data)
            for m in re.finditer(r'(?:objectHrefTerm|CreateRequestHrefTerm|SourceHrefTerm)[^>]*>([^<]+)', raw):
                url = m.group(1).strip()
                if url.startswith("http") or url.startswith("/"):
                    results.append(
                        TenderResult(
                            title="Процедура",
                            url=_normalize_url(url),
                        )
                    )
    except Exception as e:
        print(f"    ⚠️ Ошибка разбора ответа: {e}")
    return results


def _parse_table_xml(table_xml: str) -> List[TenderResult]:
    """Парсит tableXml (datarow элементы)."""
    results: List[TenderResult] = []
    try:
        # Убираем лишние пробелы и исправляем возможные обрезки тегов
        table_xml = table_xml.strip()
        if not table_xml.startswith("<"):
            return results
        root = ET.fromstring(table_xml)
        # Ищем datarow или строки таблицы
        for row in root.iter("datarow"):
            item = {}
            for child in row:
                tag = child.tag.strip() if child.tag else ""
                text = (child.text or "").strip()
                if tag and text:
                    item[tag] = text
            title = item.get("purchName") or item.get("BidName") or ""
            url = item.get("objectHrefTerm") or item.get("CreateRequestHrefTerm") or item.get("SourceHrefTerm") or ""
            if not title and not url:
                continue
            if not url:
                url = item.get("SourceHrefTerm") or ""
            price = item.get("purchAmount")
            if price and item.get("purchCurrency"):
                price = f"{price} {item.get('purchCurrency')}".strip()
            results.append(
                TenderResult(
                    title=title or "Без названия",
                    url=_normalize_url(url),
                    organizer=_clean(item.get("OrgName")),
                    price=price,
                    status=item.get("purchStateName") or item.get("BidStatusName"),
                    publish_date=item.get("PublicDate"),
                    deadline=item.get("EndDate") or item.get("RequestDate"),
                    tender_id=item.get("purchCodeTerm"),
                    law_type=_clean(item.get("SourceTerm")),
                    purchase_type=item.get("PurchaseTypeName"),
                )
            )
    except ET.ParseError:
        # Пробуем вытащить ссылки и названия регулярками
        for m in re.finditer(r'<purchName[^>]*>([^<]*)</purchName>', table_xml):
            title = m.group(1).strip()
            if title:
                results.append(TenderResult(title=title, url=BASE_URL))
        for m in re.finditer(r'<objectHrefTerm[^>]*>([^<]+)</objectHrefTerm>', table_xml):
            url = m.group(1).strip()
            if url and results:
                results[-1].url = _normalize_url(url)
    except Exception as e:
        print(f"    ⚠️ Ошибка разбора tableXml: {e}")
    return results


def run_search(cfg: SearchConfig) -> List[TenderResult]:
    """Поиск на Сбербанк-АСТ: открываем страницу (cookies), затем POST SearchQuery."""
    print(f"Запуск Playwright Chromium (headless={cfg.headless})...")
    collected: List[TenderResult] = []

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
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="ru-RU",
        )
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout)
        page.set_default_timeout(cfg.navigation_timeout)

        try:
            print("Загрузка UnitedPurchaseList.aspx (сессия)...")
            page.goto(LIST_URL, wait_until="domcontentloaded", timeout=cfg.navigation_timeout)
            page.wait_for_timeout(2_000)

            # Поиск через API SearchQuery.aspx — запрос по ключевому слову применяется гарантированно
            print("    Поиск через API по запросу...")
            seen: set[str] = set()
            for page_num in range(cfg.pages):
                from_offset = page_num * cfg.page_size
                xml_data = _build_xml_data(cfg.query, from_offset, size=cfg.page_size)
                try:
                    response = page.request.post(
                        SEARCH_URL,
                        data={"xmlData": xml_data},
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                            "Referer": LIST_URL,
                        },
                        timeout=30_000,
                    )
                except Exception as api_err:
                    print(f"    ⚠️ Ошибка API страницы {page_num + 1}: {api_err}")
                    break
                if response.status != 200:
                    print(f"    ⚠️ API вернул {response.status}")
                    break
                text = response.text()
                page_results = _parse_response_data(text)
                new_rows = [r for r in page_results if r.url and r.url not in seen]
                seen.update(r.url for r in new_rows)
                collected.extend(new_rows)
                print(f"  Страница {page_num + 1}/{cfg.pages}: собрано {len(new_rows)} (всего: {len(collected)})")
                if len(page_results) < cfg.page_size:
                    break
            # Если API не вернул данных — fallback на парсинг DOM после ввода в форму (как раньше)
            if not collected:
                print("    API не вернул результатов, попытка через форму на странице...")
                # Строго поле основной строки поиска (id=searchInput, content=leaf:value)
                search_input = page.locator(
                    "input#searchInput, "
                    "input.mainSearchBar-mainInput[content='leaf:value'], "
                    ".mainSearchBar input[type='search'][content='leaf:value']"
                ).first
                if search_input.count() > 0:
                    try:
                        search_input.evaluate(
                            """(el, q) => {
                                el.focus();
                                el.value = q;
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                            }""",
                            cfg.query,
                        )
                    except Exception:
                        search_input.fill(cfg.query, force=True)
                    page.wait_for_timeout(500)
                    page.keyboard.press("Enter")
                    page.wait_for_load_state("domcontentloaded", timeout=20_000)
                    page.wait_for_timeout(3_000)
                    page.wait_for_selector('div[content="node:hits"], tr:has(a[href*="PurchaseView"])', timeout=15_000)
                    page.wait_for_timeout(2_000)
                for page_num in range(cfg.pages):
                    page_results = _collect_from_dom(page)
                    for r in page_results:
                        if r.url and r.url not in seen:
                            seen.add(r.url)
                            collected.append(r)
                    if page_num + 1 >= cfg.pages:
                        break
                    next_btn = page.locator("a:has-text('>'), a:has-text('»'), .pager-next a").first
                    if next_btn.count() == 0 or not next_btn.is_visible(timeout=2_000):
                        break
                    next_btn.click()
                    page.wait_for_timeout(cfg.parse_delay)
        except PlaywrightTimeoutError as e:
            print(f"Таймаут: {e}")
        except Exception as e:
            print(f"Ошибка: {e}")
            import traceback
            traceback.print_exc()
        finally:
            context.close()
            browser.close()

    return collected


def _collect_from_dom(page) -> List[TenderResult]:
    """Сбор результатов из DOM: структура Сбербанк-АСТ div[content='node:datarow'] → div[content='node:hits'] с leaf-полями."""
    results: List[TenderResult] = []
    # Приоритет: блоки с content="node:hits" (официальная разметка Сбербанк-АСТ)
    blocks = page.locator('div[content="node:hits"]')
    n = blocks.count()
    if n > 0:
        for i in range(min(n, 500)):
            block = blocks.nth(i)
            try:
                item = _extract_one_hit(block)
                if item and item.url and ("PurchaseList" not in item.url and "purchaseList.aspx" not in item.url):
                    results.append(item)
            except Exception:
                continue
        return results
    # Fallback: старые селекторы (таблица/карточки)
    blocks = page.locator(
        ".purchase-item, .search-result-item, .purch-reestr-tbl-div, "
        "table tbody tr:has(a[href*='PurchaseView']), table tbody tr:has(a[href*='RequestCreate'])"
    )
    n = blocks.count()
    if n == 0:
        blocks = page.locator("tr:has(a[href*='PurchaseView']), tr:has(a[href*='RequestCreate'])")
        n = blocks.count()
    for i in range(min(n, 500)):
        block = blocks.nth(i)
        try:
            item = _extract_one_hit_fallback(block)
            if item and item.url:
                results.append(item)
        except Exception:
            continue
    return results


def _text_or_null(loc, timeout: int = 500) -> Optional[str]:
    """Текст элемента или None если нет/пусто."""
    if loc.count() == 0:
        return None
    try:
        t = _clean(loc.first.inner_text(timeout=timeout))
        return t or None
    except Exception:
        return None


def _attr_or_null(loc, attr: str, timeout: int = 500) -> Optional[str]:
    """Атрибут элемента или None."""
    if loc.count() == 0:
        return None
    try:
        v = loc.first.get_attribute(attr)
        return _clean(v) if v else None
    except Exception:
        return None


def _extract_one_hit(block) -> Optional[TenderResult]:
    """Извлекает один тендер из блока div[content='node:hits'] по leaf-селекторам."""
    # URL из скрытого поля (надёжно)
    url_elem = block.locator('input[content="leaf:objectHrefTerm"]')
    url = _attr_or_null(url_elem, "value")
    if not url:
        url_elem = block.locator('input[content="leaf:CreateRequestHrefTerm"]')
        url = _attr_or_null(url_elem, "value")
    url = _normalize_url(url or "")
    if not url:
        return None
    # Название
    title = _text_or_null(block.locator('span[content="leaf:purchName"], .es-el-name'))
    if not title:
        title = _text_or_null(block.locator('span[content="leaf:bidName"]'))
    title = title or "Процедура"
    # Номер процедуры
    tender_id = _text_or_null(block.locator('span[content="leaf:purchCodeTerm"], .es-el-code-term'))
    if tender_id:
        tender_id = re.sub(r"^№\s*", "", tender_id).strip()
    # Организатор
    organizer = _text_or_null(block.locator('div[content="leaf:OrgName"], .es-el-org-name'))
    # Цена + валюта
    amount = _text_or_null(block.locator('span[content="leaf:purchAmount"], .es-el-amount'))
    currency = _text_or_null(block.locator('span[content="leaf:purchCurrency"], .es-el-currency'))
    price = None
    if amount:
        price = f"{amount} {currency}".strip() if currency else amount
    # Статус: purchStateName или BidStatusName (один может быть скрыт)
    status = _text_or_null(block.locator('div[content="leaf:purchStateName"]'))
    if not status:
        status = _text_or_null(block.locator('div[content="leaf:BidStatusName"]'))
    # Тип закупки и закон
    purchase_type = _text_or_null(block.locator('div[content="leaf:PurchaseTypeName"], .es-el-type-name'))
    law_type = _text_or_null(block.locator('span[content="leaf:SourceTerm"], .es-el-source-term'))
    # Даты
    publish_date = _text_or_null(block.locator('span[content="leaf:PublicDate"]'))
    deadline = _text_or_null(block.locator('span[content="leaf:EndDate"]'))
    if not deadline:
        deadline = _text_or_null(block.locator('span[content="leaf:RequestDate"]'))
    return TenderResult(
        title=title,
        url=url,
        organizer=organizer,
        price=price,
        status=status,
        publish_date=publish_date,
        deadline=deadline,
        tender_id=tender_id,
        law_type=law_type,
        purchase_type=purchase_type,
    )


def _extract_one_hit_fallback(block) -> Optional[TenderResult]:
    """Извлечение из строки таблицы/карточки (старая разметка)."""
    view_link = block.locator("a[href*='PurchaseView'], a[href*='RequestCreate'], a[href*='PurchaseRequest']").first
    if view_link.count() == 0:
        return None
    url = _normalize_url(view_link.get_attribute("href") or "")
    if "PurchaseList" in url or "purchaseList.aspx" in url:
        return None
    title = _clean(view_link.inner_text(timeout=1_000))
    if not title or len(title) < 10:
        title = _text_or_null(block.locator(".purchase-name, .purch-name, .es-el-name, [class*='title']")) or "Процедура"
    if not title or len(title) < 5:
        title = "Процедура"
    tender_id = _text_or_null(block.locator("text=/№\\s*[\\d\\w-]+/"))
    if tender_id:
        tender_id = re.sub(r"^№\s*", "", tender_id).strip()
    organizer = _text_or_null(block.locator(".es-el-org-name, [class*='org-name'], [class*='organizator']"))
    if not organizer:
        try:
            full_text = block.inner_text(timeout=1_000)
            for line in full_text.split("\n"):
                line = line.strip()
                if len(line) > 20 and re.match(r"^[А-ЯA-Z]", line) and "RUB" not in line and "№" not in line[:5]:
                    organizer = _clean(line)
                    break
        except Exception:
            pass
    price = _text_or_null(block.locator(".es-el-amount"))
    currency = _text_or_null(block.locator(".es-el-currency"))
    if price and currency:
        price = f"{price} {currency}".strip()
    status = None
    for st in ["Подача заявок", "Прием заявок", "Приём заявок", "Рассмотрение", "Завершен", "Отменен"]:
        se = block.locator(f"text=/{re.escape(st)}/").first
        if se.count() > 0:
            status = _clean(se.inner_text(timeout=300))
            break
    purchase_type = _text_or_null(block.locator(".es-el-type-name, [class*='purchase-type']"))
    law_type = _text_or_null(block.locator(".es-el-source-term, text=/44-ФЗ|223-ФЗ|Госзакупки|Закупки по/"))
    publish_date = None
    deadline = None
    date_elems = block.locator("text=/\\d{2}\\.\\d{2}\\.\\d{4}/")
    for j in range(date_elems.count()):
        dt = _clean(date_elems.nth(j).inner_text(timeout=300))
        if dt:
            publish_date = publish_date or dt
            deadline = dt
    return TenderResult(
        title=title,
        url=url,
        organizer=organizer,
        price=price,
        status=status,
        publish_date=publish_date,
        deadline=deadline,
        tender_id=tender_id,
        law_type=law_type,
        purchase_type=purchase_type,
    )


def save_results(path: Path, results: List[TenderResult]) -> None:
    data = [asdict(r) for r in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: List[str]) -> SearchConfig:
    parser = argparse.ArgumentParser(description="Парсер Сбербанк-АСТ UnitedPurchaseList")
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-p", "--pages", type=int, default=1)
    parser.add_argument("-o", "--output", type=Path, default=Path("sberbank_ast_results.json"))
    parser.add_argument("--headless", action="store_true", default=False, help="Запуск без окна браузера")
    parser.add_argument("--no-headless", dest="headless", action="store_false", help="Окно браузера видно (по умолчанию)")
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
    print(f"Сбербанк-АСТ. Запрос: {cfg.query}, страниц: {cfg.pages}")
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
