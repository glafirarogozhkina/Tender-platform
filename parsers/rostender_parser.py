from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Locator


# ---------------------------
# Конфигурация и структуры данных
# ---------------------------

@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("rostender_results.json")
    headless: bool = True
    navigation_timeout: int = 30_000  # ms
    parse_delay: int = 5_000  # ms


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "ROSTENDER"  # Источник данных
    customer: Optional[str] = None  # Заказчик
    price: Optional[str] = None
    status: Optional[str] = None
    publish_date: Optional[str] = None
    deadline: Optional[str] = None  # Срок подачи заявок
    region: Optional[str] = None
    tender_id: Optional[str] = None  # ID тендера
    law_type: Optional[str] = None  # 44-ФЗ, 223-ФЗ и т.д.
    purchase_type: Optional[str] = None
    platform: Optional[str] = None  # ЭТП


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
        return "https://rostender.info" + href
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
    """Поиск поля ввода для поискового запроса на rostender.info."""
    search_selectors = [
        # Специфичные для rostender.info (точное совпадение)
        "input#keywords",
        "input.search_form__input",
        "input[name='keywords']",
        # Общие селекторы
        "input[placeholder*='ключевые слова']",
        "input[placeholder*='Введите']",
        "input[type='text'][name*='search']",
        "input[type='text'][name*='query']",
        "input[type='search']",
        ".search-input",
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
# Сбор результатов со страницы РосТендер
# ---------------------------

def parse_rostender_cards(tender_items: Locator) -> List[TenderResult]:
    """
    Парсинг карточек тендеров РосТендер
    
    Структура карточки (по HTML):
    - div.tender-row__wrapper - основной контейнер
    - .tender__number - номер тендера (№88777670)
    - .tender__date-start - дата размещения (от 13.12.25)
    - .tender__class.b-44 - закон (44-ФЗ)
    - a.description.tender-info__description - название
    - .tender__countdown-text - дата окончания
    - .tender-address - адрес/регион
    - .starting-price__price - начальная цена
    - .list-branches__link - категория/отрасль
    """
    results: List[TenderResult] = []
    
    for i in range(tender_items.count()):
        card = tender_items.nth(i)
        try:
            # Номер тендера
            tender_id = None
            number_elem = card.locator(".tender__number").first
            if number_elem.count() > 0:
                number_text = number_elem.inner_text(timeout=1_000)
                # Извлекаем только цифры: "Тендер №88777670" -> "88777670"
                id_match = re.search(r'№?\s*(\d+)', number_text)
                if id_match:
                    tender_id = id_match.group(1)
            
            # Название и URL
            title = None
            url = ""
            title_elem = card.locator("a.description.tender-info__description, a.tender-info__link").first
            if title_elem.count() > 0:
                title_html = title_elem.inner_html(timeout=2_000)
                # Убираем HTML теги (например, <i class="shl">медицинских</i>)
                title = re.sub(r'<[^>]+>', '', title_html).strip()
                url = title_elem.get_attribute("href") or ""
            
            if not title or len(title) < 5:
                # Fallback: любая ссылка
                links = card.locator("a[href*='tender']")
                for j in range(min(links.count(), 3)):
                    try:
                        link = links.nth(j)
                        temp_title = link.inner_text(timeout=1_000).strip()
                        temp_url = link.get_attribute("href") or ""
                        if len(temp_title) > 10 and temp_url:
                            title = temp_title
                            url = temp_url
                            break
                    except Exception:
                        continue
            
            if not title or len(title) < 5:
                continue
            
            # Дата размещения
            publish_date = None
            date_start_elem = card.locator(".tender__date-start").first
            if date_start_elem.count() > 0:
                date_text = date_start_elem.inner_text(timeout=1_000)
                # "от 13.12.25" -> "13.12.25"
                date_match = re.search(r'(\d{2}\.\d{2}\.\d{2,4})', date_text)
                if date_match:
                    publish_date = date_match.group(1)
            
            # Закон (44-ФЗ, 223-ФЗ)
            law_type = None
            # Проверяем классы элементов
            if card.locator(".tender__class.b-44").count() > 0:
                law_type = "44-ФЗ"
            elif card.locator(".tender__class.b-223").count() > 0:
                law_type = "223-ФЗ"
            elif card.locator(".tender__class.b-615").count() > 0:
                law_type = "615-ФЗ"
            
            # Дата окончания
            deadline = None
            countdown_elem = card.locator(".tender__countdown-text").first
            if countdown_elem.count() > 0:
                countdown_text = countdown_elem.inner_text(timeout=1_000)
                # "Окончание (МСК) 13.12.2025 05:08" -> "13.12.2025 05:08"
                deadline_match = re.search(r'(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})', countdown_text)
                if deadline_match:
                    deadline = f"{deadline_match.group(1)} {deadline_match.group(2)}"
            
            # Регион/Адрес
            region = None
            address_elem = card.locator(".tender-address .line-clamp").first
            if address_elem.count() > 0:
                region = _clean(address_elem.inner_text(timeout=1_000))
            
            # Можно также взять регион из ссылок
            if not region:
                region_link = card.locator(".tender__region-link").first
                if region_link.count() > 0:
                    region = _clean(region_link.inner_text(timeout=1_000))
            
            # Цена
            price = None
            price_elem = card.locator(".starting-price__price").first
            if price_elem.count() > 0:
                price = _clean(price_elem.inner_text(timeout=1_000))
            
            # Категория/Отрасль (используем как purchase_type)
            purchase_type = None
            category_elem = card.locator(".list-branches__link").first
            if category_elem.count() > 0:
                purchase_type = _clean(category_elem.inner_text(timeout=1_000))
            
            # Определяем статус по типу закупки (если есть tender__pwh--mz, то "Малый объем")
            status = "Прием заявок"  # По умолчанию
            if card.locator(".tender__pwh--mz").count() > 0:
                status = "Закупка малого объема"
            
            # Фильтруем старые тендеры (2021-2023)
            if deadline and any(year in str(deadline) for year in ['2021', '2022', '2023']):
                continue
            if publish_date and any(year in str(publish_date) for year in ['2021', '2022', '2023']):
                continue
            
            results.append(
                TenderResult(
                    title=_clean(title) or title,
                    url=_normalize_url(url, "https://rostender.info"),
                    source="ROSTENDER",
                    tender_id=tender_id,
                    customer=None,  # Заказчик не отображается в списке
                    price=price,
                    law_type=law_type,
                    purchase_type=purchase_type,
                    status=status,
                    publish_date=publish_date,
                    deadline=deadline,
                    region=region,
                    platform="ЕИС" if law_type == "44-ФЗ" else None,
                )
            )
            
        except Exception as e:
            print(f"    ⚠️  Ошибка при парсинге карточки {i+1}: {e}")
            continue
    
    return results


def collect_page_results_rostender(page: Page) -> List[TenderResult]:
    """Извлечение результатов поиска со страницы rostender.info."""
    results: List[TenderResult] = []

    # Возможные селекторы для карточек тендеров
    possible_selectors = [
        ".tender-row__wrapper",  # Основной селектор для РосТендер
        ".tender-item",
        ".tender-card",
        ".tender-row",
        ".search-result-item",
        ".lot-item",
        ".tender",
        "div[class*='tender-row']",
        "div[class*='tender']",
        ".result-item",
        "article",
    ]
    
    tender_items = None
    for selector in possible_selectors:
        try:
            items = page.locator(selector)
            if items.count() > 0:
                print(f"    ✓ Найдены тендеры с селектором: {selector} ({items.count()} шт.)")
                tender_items = items
                break
        except Exception:
            continue
    
    if tender_items is None or tender_items.count() == 0:
        print("    ⚠️  Тендеры не найдены, пробуем универсальный подход...")
        return collect_page_results_fallback(page)
    
    return parse_rostender_cards(tender_items)


def collect_page_results_fallback(page: Page) -> List[TenderResult]:
    """Запасной вариант сбора результатов через поиск всех ссылок."""
    results: List[TenderResult] = []
    
    # Ищем все ссылки, которые могут вести на тендеры
    links = page.locator("a[href*='tender'], a[href*='lot'], a[href*='zakupka']")
    
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
                        source="ROSTENDER",
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
        "a:has-text('Вперед')",
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
    """Основная функция поиска тендеров на rostender.info."""
    
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
                print("Загрузка rostender.info...")
                page.goto("https://rostender.info/", wait_until="domcontentloaded")
                page.wait_for_timeout(3_000)
                
                # Шаг 2: Ищем поле поиска
                search_box = find_search_input(page)
                
                if search_box is None:
                    # Пробуем страницу поиска
                    alternative_urls = [
                        "https://rostender.info/search",
                        "https://rostender.info/tenders",
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
                    
                    # Ищем кнопку поиска или нажимаем Enter
                    search_button_selectors = [
                        "button[type='submit']",
                        "button.search-button",
                        "button:has-text('Искать')",
                        "button:has-text('Найти')",
                        ".search-btn",
                    ]
                    
                    button_found = False
                    for btn_selector in search_button_selectors:
                        try:
                            btn = page.locator(btn_selector).first
                            if btn.count() > 0 and btn.is_visible(timeout=1_000):
                                print(f"    Нажатие кнопки поиска ({btn_selector})...")
                                btn.click()
                                button_found = True
                                break
                        except Exception:
                            continue
                    
                    if not button_found:
                        print("    Кнопка не найдена, используем Enter...")
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
                page_info = f"Страница {page_idx + 1}/{cfg.pages}"
                print(f"┌{'─' * 58}┐")
                print(f"│ {page_info}{' ' * (56 - len(page_info))}│")
                print(f"└{'─' * 58}┘")
                
                page_results = collect_page_results_rostender(page)
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
        description="Парсер результатов поиска РосТендер (rostender.info) через Playwright.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  python rostender_parser.py "строительство"
  python rostender_parser.py "медицинское оборудование" -p 3
  python rostender_parser.py "ремонт дорог Новосибирск" --no-headless
        """
    )
    
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-p", "--pages", type=int, default=1, 
                       help="Количество страниц для парсинга (по умолчанию: 1)")
    parser.add_argument("-o", "--output", type=Path, default=Path("rostender_results.json"),
                       help="Файл для сохранения результатов (по умолчанию: rostender_results.json)")
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
    print("║" + " " * 10 + "ПАРСЕР РОСТЕНДЕР (PLAYWRIGHT)" + " " * 17 + "║")
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

