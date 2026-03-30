from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Optional
import numpy as np

from playwright.sync_api import sync_playwright

# Проверка наличия OpenCV
try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    print("⚠️  ВНИМАНИЕ: OpenCV не установлен! Компьютерное зрение отключено.", file=sys.stderr)
    print("Для установки выполните: pip install opencv-python", file=sys.stderr)
    OPENCV_AVAILABLE = False

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, Page, Locator


# ---------------------------
# Конфигурация и структуры данных
# ---------------------------

@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("rutend_results.json")
    headless: bool = True
    navigation_timeout: int = 30_000  # ms
    parse_delay: int = 5_000  # ms (меньше чем для RTS)


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "RUTEND"  # Источник данных
    customer: Optional[str] = None
    organizer: Optional[str] = None
    price: Optional[str] = None
    status: Optional[str] = None
    publish_date: Optional[str] = None
    deadline: Optional[str] = None
    region: Optional[str] = None
    law_type: Optional[str] = None  # 44-ФЗ, 223-ФЗ
    purchase_type: Optional[str] = None  # Электронный аукцион, Запрос котировок
    payment_scheme: Optional[str] = None  # Схема оплаты


# ---------------------------
# Вспомогательные функции
# ---------------------------

def _safe_inner_text(scope: Locator, selector: str) -> Optional[str]:
    """Безопасное извлечение текста из элемента."""
    try:
        loc = scope.locator(selector)
        if loc.count() == 0:
            return None
        return loc.first.inner_text(timeout=2_000)
    except Exception:
        return None


def _clean(value: Optional[str]) -> Optional[str]:
    """Очистка строки от лишних пробелов."""
    if value is None:
        return None
    stripped = " ".join(value.split())
    return stripped or None


def _normalize_url(href: str, base_url: str) -> str:
    """Приведение относительных URL к абсолютным."""
    if not href:
        return ""
    href = href.strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("/"):
        return "https://rutend.ru" + href
    if base_url.endswith("/"):
        return base_url + href
    return base_url.rsplit("/", 1)[0] + "/" + href


def countdown_timer(seconds: int, message: str = "Ожидание") -> None:
    """Обратный отсчет с выводом в консоль."""
    for remaining in range(seconds, 0, -1):
        print(f"\r{message}: {remaining} сек... ", end="", flush=True)
        time.sleep(1)
    print(f"\r{message}: завершено!     ")


def find_search_input(page: Page) -> Optional[Locator]:
    """Поиск поля ввода для поискового запроса на rutend.ru."""
    search_selectors = [
        # Специфичные для rutend.ru
        "input[placeholder*='Поиск']",
        "input[name='search']",
        "input[name='q']",
        "input[type='search']",
        "#search",
        ".search-input",
        "input[placeholder*='поиск']",
        "input[placeholder*='Search']",
        ".search__input",
        "form input[type='text']",
    ]
    
    for selector in search_selectors:
        try:
            loc = page.locator(selector)
            if loc.count() > 0:
                first = loc.first
                if first.is_visible(timeout=1_000):
                    print(f"    ✓ Найдено поле поиска: {selector}")
                    return first
        except Exception:
            continue
    
    return None


# ---------------------------
# Сбор результатов со страницы RUTEND
# ---------------------------

def parse_rutend_auctions(auction_items: Locator) -> List[TenderResult]:
    """
    Парсинг карточек тендеров RUTEND со структурой div.auction
    
    Структура:
    <div class="auction">
        <div class="auction__info">
            <p class="auction__number"><span>№ 223-ФЗ</span></p>
            <p class="auction__type"><span>Запрос предложений</span></p>
            <p class="auction__price"><span class="auction__total">8 618 400</span> руб.</p>
        </div>
        <a class="auction__title" href="/tenders/...">Название тендера</a>
        <p class="auction__address">Адрес</p>
        <div class="auction__conditions">
            <div class="auction__condition">Схема оплаты<br><span>...</span></div>
            <div class="auction__condition">Обновлено<br><time>...</time></div>
            <div class="auction__condition">Окончание подачи заявок<br><time>...</time></div>
        </div>
    </div>
    """
    results: List[TenderResult] = []
    
    for i in range(auction_items.count()):
        auction = auction_items.nth(i)
        try:
            # Название тендера (обязательное)
            title_elem = auction.locator("a.auction__title").first
            if title_elem.count() == 0:
                continue
            
            title = title_elem.inner_text(timeout=2_000).strip()
            if not title:
                continue
            
            # URL
            url = title_elem.get_attribute("href") or ""
            
            # Номер закона (223-ФЗ, 44-ФЗ, 615-ФЗ)
            law_type = None
            number_elem = auction.locator("p.auction__number span").first
            if number_elem.count() > 0:
                number_text = number_elem.inner_text(timeout=1_000)
                law_type = _clean(number_text)
            
            # Тип закупки
            purchase_type = None
            type_elem = auction.locator("p.auction__type span").first
            if type_elem.count() > 0:
                type_text = type_elem.inner_text(timeout=1_000)
                purchase_type = _clean(type_text)
            
            # Цена
            price = None
            price_total_elem = auction.locator("span.auction__total").first
            price_currency_elem = auction.locator("span.auction__currency").first
            
            if price_total_elem.count() > 0:
                total = price_total_elem.inner_text(timeout=1_000).strip()
                currency = ""
                if price_currency_elem.count() > 0:
                    currency = price_currency_elem.inner_text(timeout=1_000).strip()
                price = f"{total} {currency}".strip()
            
            # Адрес/регион
            region = None
            address_elem = auction.locator("p.auction__address").first
            if address_elem.count() > 0:
                address_text = address_elem.inner_text(timeout=1_000)
                # Пытаемся извлечь регион из адреса
                address_parts = address_text.split(',')
                if len(address_parts) > 1:
                    region = _clean(address_parts[1])  # Обычно регион во второй части
                else:
                    region = _clean(address_text[:50])  # Первые 50 символов
            
            # Схема оплаты
            payment_scheme = None
            scheme_elem = auction.locator("span.auction__scheme").first
            if scheme_elem.count() > 0:
                scheme_text = scheme_elem.inner_text(timeout=1_000)
                payment_scheme = _clean(scheme_text)
            
            # Дата обновления
            publish_date = None
            date_elem = auction.locator("span.auction__date time").first
            if date_elem.count() > 0:
                date_text = date_elem.inner_text(timeout=1_000)
                publish_date = _clean(date_text)
            
            # Дата окончания подачи заявок
            deadline = None
            finish_elem = auction.locator("span.auction__finish time").first
            if finish_elem.count() > 0:
                finish_text = finish_elem.inner_text(timeout=1_000)
                deadline = _clean(finish_text)
            
            # Фильтруем старые тендеры (2021-2023)
            if deadline and any(year in str(deadline) for year in ['2021', '2022', '2023']):
                continue
            if publish_date and any(year in str(publish_date) for year in ['2021', '2022', '2023']):
                continue
            
            results.append(
                TenderResult(
                    title=_clean(title) or title.strip(),
                    url=_normalize_url(url, "https://rutend.ru"),
                    source="RUTEND",
                    price=price,
                    law_type=law_type,
                    purchase_type=purchase_type,
                    payment_scheme=payment_scheme,
                    deadline=deadline,
                    publish_date=publish_date,
                    region=region,
                )
            )
        except Exception as e:
            print(f"    ⚠️  Ошибка при парсинге карточки {i+1}: {e}")
            continue
    
    return results


def collect_page_results_rutend(page: Page) -> List[TenderResult]:
    """Извлечение результатов поиска со страницы rutend.ru."""
    results: List[TenderResult] = []

    # Приоритет: структура RUTEND с классом "auction"
    auction_items = page.locator("div.auction")
    
    if auction_items.count() > 0:
        print(f"    ✓ Найдена структура RUTEND (div.auction): {auction_items.count()} карточек")
        return parse_rutend_auctions(auction_items)
    
    # Пробуем альтернативные варианты структуры
    possible_selectors = [
        "div.tender-item",
        "div.lot-item",
        "article.tender",
        ".tender-card",
        ".lot-card",
        "div[class*='tender']",
        "div[class*='lot']",
        ".card",
    ]
    
    tender_items = None
    for selector in possible_selectors:
        try:
            items = page.locator(selector)
            if items.count() > 0:
                print(f"    Найдены тендеры с селектором: {selector} ({items.count()} шт.)")
                tender_items = items
                break
        except Exception:
            continue
    
    if tender_items is None or tender_items.count() == 0:
        print("    ⚠️  Тендеры не найдены, пробуем универсальный подход...")
        return collect_page_results_fallback(page)
    
    # Парсим найденные элементы
    for i in range(tender_items.count()):
        item = tender_items.nth(i)
        try:
            # Заголовок
            title = None
            title_selectors = [
                "h3", "h4", ".title", ".tender-title", 
                "a[href*='tender']", "a[href*='lot']",
                ".card-title"
            ]
            for sel in title_selectors:
                title = _safe_inner_text(item, sel)
                if title:
                    break
            
            if not title:
                continue
            
            # URL
            url = ""
            link_elem = item.locator("a[href]").first
            if link_elem.count() > 0:
                url = link_elem.get_attribute("href") or ""
            
            # Цена
            price = None
            price_selectors = [
                ".price", ".sum", ".amount", 
                "*:has-text('руб')", "*:has-text('₽')",
                "[class*='price']", "[class*='sum']"
            ]
            for sel in price_selectors:
                price_text = _safe_inner_text(item, sel)
                if price_text and ('руб' in price_text or '₽' in price_text):
                    price = price_text
                    break
            
            # Тип закона (44-ФЗ, 223-ФЗ)
            law_type = None
            law_text = item.inner_text()
            if '44-ФЗ' in law_text:
                law_type = '44-ФЗ'
            elif '223-ФЗ' in law_text:
                law_type = '223-ФЗ'
            elif '615-ФЗ' in law_text:
                law_type = '615-ФЗ'
            
            # Тип закупки
            purchase_type = None
            if 'аукцион' in law_text.lower():
                purchase_type = 'Электронный аукцион'
            elif 'котировок' in law_text.lower():
                purchase_type = 'Запрос котировок'
            elif 'конкурс' in law_text.lower():
                purchase_type = 'Конкурс'
            
            # Схема оплаты
            payment_scheme = None
            payment_elem = _safe_inner_text(item, "*:has-text('Схема оплаты')")
            if payment_elem:
                payment_scheme = _clean(payment_elem)
            elif 'аванс' in law_text.lower():
                payment_scheme = 'Аванс'
            
            # Даты
            deadline = None
            publish_date = None
            
            # Ищем дату окончания
            deadline_text = _safe_inner_text(item, "*:has-text('Окончание')")
            if deadline_text:
                # Извлекаем дату из текста
                date_match = re.search(r'(\d{1,2}\s+\w+\s+\d{4})', deadline_text)
                if date_match:
                    deadline = date_match.group(1)
            
            # Ищем дату публикации/обновления
            update_text = _safe_inner_text(item, "*:has-text('Обновлено')")
            if update_text:
                date_match = re.search(r'(\d{1,2}\s+\w+\s+\d{4})', update_text)
                if date_match:
                    publish_date = date_match.group(1)
            
            # Регион/адрес
            region = None
            address_elem = _safe_inner_text(item, "*:has-text('Российская Федерация')")
            if address_elem:
                # Пытаемся извлечь регион из адреса
                region_match = re.search(r'Российская Федерация[,\s]+\d+[,\s]+([^,]+)', address_elem)
                if region_match:
                    region = region_match.group(1).strip()
            
            results.append(
                TenderResult(
                    title=_clean(title) or title.strip(),
                    url=_normalize_url(url, page.url),
                    source="RUTEND",
                    price=_clean(price),
                    law_type=law_type,
                    purchase_type=purchase_type,
                    payment_scheme=payment_scheme,
                    deadline=deadline,
                    publish_date=publish_date,
                    region=region,
                )
            )
        except Exception as e:
            print(f"    ⚠️  Ошибка при парсинге тендера {i+1}: {e}")
            continue
    
    return results


def collect_page_results_fallback(page: Page) -> List[TenderResult]:
    """Запасной вариант сбора результатов через поиск всех ссылок."""
    results: List[TenderResult] = []
    
    # Ищем все ссылки, которые могут вести на тендеры
    links = page.locator("a[href*='tender'], a[href*='lot'], a[href*='zakupk']")
    
    seen_urls = set()
    for i in range(min(links.count(), 50)):  # Ограничиваем 50 результатами
        try:
            link = links.nth(i)
            title = link.inner_text(timeout=1_000).strip()
            url = link.get_attribute("href") or ""
            
            if title and url and url not in seen_urls and len(title) > 10:
                seen_urls.add(url)
                results.append(
                    TenderResult(
                        title=_clean(title) or title,
                        url=_normalize_url(url, page.url),
                        source="RUTEND",
                    )
                )
        except Exception:
            continue
    
    return results


def go_next_page(page: Page) -> bool:
    """Переход на следующую страницу результатов."""
    next_selectors = [
        "a[rel='next']",
        "button[aria-label='Следующая']",
        "a[aria-label='Следующая']",
        ".pagination__next:not(.disabled)",
        ".pagination .next:not(.disabled)",
        ".next-page:not(.disabled)",
        "a:has-text('Следующая'):not(.disabled)",
        "button:has-text('Следующая'):not(:disabled)",
        "a:has-text('»')",
        "a:has-text('→')",
        ".pager__next a",
        ".pagination li:last-child a",
    ]
    
    for selector in next_selectors:
        try:
            next_elem = page.locator(selector).first
            if next_elem.count() > 0 and next_elem.is_visible(timeout=1_000):
                is_disabled = next_elem.get_attribute("disabled")
                class_attr = next_elem.get_attribute("class") or ""
                
                if is_disabled or "disabled" in class_attr:
                    continue
                    
                next_elem.click()
                page.wait_for_load_state("networkidle", timeout=15_000)
                page.wait_for_timeout(1_500)
                return True
        except Exception:
            continue
    
    return False


# ---------------------------
# Основная логика поиска
# ---------------------------

def run_search(cfg: SearchConfig) -> List[TenderResult]:
    """Основная функция поиска тендеров на rutend.ru."""
    
    print(f"Запуск Playwright Chromium (headless={cfg.headless})...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=cfg.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
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
            try:
                # Шаг 1: Загружаем главную страницу
                print("Загрузка rutend.ru...")
                page.goto("https://rutend.ru/", wait_until="domcontentloaded")
                page.wait_for_timeout(3_000)
                
                # Шаг 2: Ищем поле поиска
                search_box = find_search_input(page)
                
                if search_box is None:
                    # Пробуем страницу поиска/каталога
                    alternative_urls = [
                        "https://rutend.ru/tenders",
                        "https://rutend.ru/search",
                        "https://rutend.ru/catalog",
                    ]
                    
                    for url in alternative_urls:
                        print(f"  Пробуем {url}...")
                        try:
                            page.goto(url, wait_until="domcontentloaded")
                            page.wait_for_timeout(2_000)
                            search_box = find_search_input(page)
                            if search_box:
                                break
                        except Exception:
                            continue

                if search_box:
                    # Шаг 3: Выполняем поиск
                    print(f"\nВыполняем поиск: '{cfg.query}'")
                    search_box.click()
                    search_box.fill(cfg.query)
                    page.wait_for_timeout(500)
                    search_box.press("Enter")
                    
                    # Ждём загрузки результатов
                    print("    Ожидание загрузки результатов...")
                    page.wait_for_load_state("networkidle", timeout=cfg.navigation_timeout)
                    page.wait_for_timeout(3_000)  # Дополнительное ожидание для динамических результатов
                    print("    ✓ Результаты загружены")
                else:
                    print("⚠️  Поле поиска не найдено, пробуем парсить главную страницу...")
            
            except PlaywrightTimeoutError as e:
                raise RuntimeError(f"Таймаут при загрузке страницы: {e}")

            # Таймаут перед парсингом
            print("")
            countdown_timer(cfg.parse_delay // 1000, "⏱️  Таймаут")
            print("")
            
            # Шаг 4: Собираем результаты
            collected: List[TenderResult] = []

            for page_idx in range(cfg.pages):
                print(f"┌{'─' * 58}┐")
                print(f"│ Страница {page_idx + 1}/{cfg.pages}{' ' * (50 - len(f'Страница {page_idx + 1}/{cfg.pages}'))}│")
                print(f"└{'─' * 58}┘")
                
                page_results = collect_page_results_rutend(page)
                collected.extend(page_results)
                print(f"  ✓ Собрано: {len(page_results)} результатов (всего: {len(collected)})")

                if page_idx + 1 >= cfg.pages:
                    break

                print("  → Переход на следующую страницу...")
                if not go_next_page(page):
                    print("  ⚠ Следующая страница не найдена, завершаем.")
                    break
                
                print("")
                countdown_timer(cfg.parse_delay // 1000, "⏱️  Таймаут перед парсингом следующей страницы")
                print("")

            return collected
        finally:
            context.close()
            browser.close()


def save_results(path: Path, results: Iterable[TenderResult]) -> None:
    """Сохранение результатов в JSON-файл."""
    data = [asdict(res) for res in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------
# CLI
# ---------------------------

def parse_args(argv: List[str]) -> SearchConfig:
    """Парсинг аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description="Парсер результатов поиска rutend.ru через Playwright.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  python rutend_parser.py "строительство"
  python rutend_parser.py "медицинское оборудование" -p 3
  python rutend_parser.py "IT услуги" --no-headless
        """
    )
    
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-p", "--pages", type=int, default=1, 
                       help="Количество страниц для парсинга (по умолчанию: 1)")
    parser.add_argument("-o", "--output", type=Path, default=Path("rutend_results.json"),
                       help="Файл для сохранения результатов (по умолчанию: rutend_results.json)")
    parser.add_argument("--headless", action="store_true", default=True,
                       help="Запуск без интерфейса браузера (по умолчанию)")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                       help="Запуск с видимым окном браузера")
    parser.add_argument("--timeout", type=int, default=30_000,
                       help="Таймаут навигации в миллисекундах (по умолчанию: 30000)")
    parser.add_argument("--parse-delay", type=int, default=5,
                       help="Задержка перед парсингом каждой страницы в секундах (по умолчанию: 5)")
    
    args = parser.parse_args(argv)
    
    return SearchConfig(
        query=args.query,
        pages=args.pages,
        output=args.output,
        headless=args.headless,
        navigation_timeout=args.timeout,
        parse_delay=args.parse_delay * 1000,
    )


def main(argv: List[str]) -> int:
    """Точка входа в программу."""
    cfg = parse_args(argv)
    start = time.time()
    
    print("")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 10 + "ПАРСЕР RUTEND.RU (PLAYWRIGHT)" + " " * 19 + "║")
    print("╚" + "═" * 58 + "╝")
    print(f"  Запрос:          {cfg.query}")
    print(f"  Страниц:         {cfg.pages}")
    print(f"  Headless:        {cfg.headless}")
    print(f"  Задержка:        {cfg.parse_delay // 1000} секунд")
    print(f"  Выходной файл:   {cfg.output}")
    print("─" * 60)
    
    try:
        results = run_search(cfg)
    except KeyboardInterrupt:
        print("\n[!] Прервано пользователем")
        return 130
    except Exception as exc:
        print(f"\n[!] Ошибка: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    save_results(cfg.output, results)
    
    duration = time.time() - start
    print("")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 20 + "РЕЗУЛЬТАТЫ" + " " * 28 + "║")
    print("╠" + "═" * 58 + "╣")
    print(f"║  ✓ Собрано результатов:  {len(results):<30} ║")
    print(f"║  ✓ Время выполнения:     {duration:.1f} сек{' ' * (28 - len(f'{duration:.1f} сек'))} ║")
    print(f"║  ✓ Сохранено в:          {str(cfg.output)[:28]:<30} ║")
    print("╚" + "═" * 58 + "╝")
    print("")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

