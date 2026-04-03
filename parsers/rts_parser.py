from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
import numpy as np

# Импорт Playwright
from playwright.sync_api import sync_playwright

# Проверка наличия OpenCV
try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    print("⚠️  ВНИМАНИЕ: OpenCV не установлен! Компьютерное зрение отключено.", file=sys.stderr)
    print("Для установки выполните: pip install opencv-python", file=sys.stderr)
    OPENCV_AVAILABLE = False

# Опциональный OCR
try:
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, Page, Locator


# ---------------------------
# Конфигурация и структуры данных
# ---------------------------

@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("rts_results.json")
    headless: bool = True
    navigation_timeout: int = 30_000  # ms
    parse_delay: int = 20_000  # ms
    use_cv: bool = True  # ⭐ Использовать компьютерное зрение
    cv_timeout: int = 30  # Таймаут ожидания элемента через CV (секунды)
    cv_check_interval: float = 0.5  # Интервал проверки (секунды)
    cv_confidence: float = 0.7  # Порог уверенности для template matching (0.0-1.0)
    save_debug_images: bool = False  # Сохранять отладочные изображения
    # Фильтры по правилам проведения
    law_44fz: bool = True  # 44-ФЗ
    law_223fz: bool = True  # 223-ФЗ
    law_615pp: bool = True  # 615-ПП РФ
    law_small_volume: bool = True  # Закупки малого объёма / РТС - МАРКЕТ
    law_commercial: bool = True  # Коммерческие закупки (РТС-тендер)
    law_commercial_offers: bool = True  # Запросы коммерческих предложений


@dataclass
class TenderResult:
    title: str
    url: str
    customer: Optional[str] = None  # Заказчик
    organizer: Optional[str] = None  # Организатор
    price: Optional[str] = None  # Начальная цена
    status: Optional[str] = None  # Статус (например, "Прием заявок")
    publish_date: Optional[str] = None  # Дата публикации
    deadline: Optional[str] = None  # Срок подачи заявок
    region: Optional[str] = None  # Регион
    inn: Optional[str] = None  # ИНН
    kpp: Optional[str] = None  # КПП
    registration_number: Optional[str] = None  # Номер закупки в ЕИС
    law_type: Optional[str] = None  # Тип закона (223-ФЗ, 44-ФЗ и т.д.)
    purchase_type: Optional[str] = None  # Тип закупки (АУКЦИОН и т.д.)
    platform: Optional[str] = None  # Площадка (РТС-ТЕНДЕР и т.д.)


# ---------------------------
# Компьютерное зрение (OpenCV)
# ---------------------------

class ComputerVision:
    """Класс для работы с компьютерным зрением."""
    
    def __init__(self, debug_dir: Path = Path("cv_debug")):
        self.debug_dir = debug_dir
        if not OPENCV_AVAILABLE:
            raise RuntimeError("OpenCV не установлен. Установите: pip install opencv-python")
    
    def take_screenshot(self, page: Page, filename: str = "screenshot.png") -> np.ndarray:
        """Сделать скриншот страницы и вернуть как numpy array."""
        screenshot_path = self.debug_dir / filename
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        
        page.screenshot(path=str(screenshot_path))
        img = cv2.imread(str(screenshot_path))
        return img
    
    def find_text_on_screen(self, page: Page, target_texts: List[str], debug: bool = False) -> bool:
        """
        Поиск текста на экране с помощью OCR.
        
        Args:
            page: Объект страницы Playwright
            target_texts: Список текстов для поиска
            debug: Сохранять ли отладочные изображения
        
        Returns:
            True если хотя бы один текст найден
        """
        if not OCR_AVAILABLE:
            print("⚠️  pytesseract не установлен, OCR недоступен")
            return False
        
        img = self.take_screenshot(page, "ocr_check.png")
        
        # Предобработка для лучшего OCR
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        
        # Распознавание текста
        try:
            text = pytesseract.image_to_string(thresh, lang='rus+eng')
            text_lower = text.lower()
            
            if debug:
                print(f"    OCR распознанный текст: {text[:100]}...")
            
            for target in target_texts:
                if target.lower() in text_lower:
                    print(f"    ✓ Найден текст через OCR: '{target}'")
                    return True
            
        except Exception as e:
            print(f"    ⚠️  Ошибка OCR: {e}")
        
        return False
    
    def find_input_field(self, page: Page, debug: bool = False) -> bool:
        """
        Детекция поля ввода по визуальным признакам.
        
        Ищет прямоугольные области с характеристиками input полей:
        - Светлый фон
        - Прямоугольная форма
        - Определенное соотношение сторон
        """
        img = self.take_screenshot(page, "input_detection.png")
        
        # Конвертация в HSV для лучшей детекции
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        
        # Маска для белых/светлых областей (обычно input поля)
        lower_white = np.array([0, 0, 180])
        upper_white = np.array([180, 30, 255])
        mask = cv2.inRange(hsv, lower_white, upper_white)
        
        # Морфологические операции для улучшения
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        
        # Поиск контуров
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        found_inputs = 0
        for contour in contours:
            area = cv2.contourArea(contour)
            
            # Фильтрация по размеру (input поля обычно среднего размера)
            if 5000 < area < 100000:
                x, y, w, h = cv2.boundingRect(contour)
                aspect_ratio = w / h if h > 0 else 0
                
                # Input поля обычно широкие и низкие (aspect ratio > 3)
                if aspect_ratio > 3 and h < 100:
                    found_inputs += 1
                    
                    if debug:
                        # Рисуем найденные области
                        cv2.rectangle(img, (x, y), (x + w, y + h), (0, 255, 0), 2)
                        cv2.putText(img, f"Input? AR:{aspect_ratio:.1f}", 
                                  (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 
                                  0.5, (0, 255, 0), 2)
        
        if debug and found_inputs > 0:
            debug_path = self.debug_dir / "detected_inputs.png"
            cv2.imwrite(str(debug_path), img)
            print(f"    Отладочное изображение: {debug_path}")
        
        if found_inputs > 0:
            print(f"    ✓ Обнаружено {found_inputs} потенциальных input полей")
            return True
        
        return False
    
    def find_element_by_template(self, page: Page, template_path: Path, 
                                confidence: float = 0.7, debug: bool = False) -> Optional[Tuple[int, int]]:
        """
        Поиск элемента на странице по шаблону (template matching).
        
        Args:
            page: Объект страницы
            template_path: Путь к эталонному изображению
            confidence: Порог совпадения (0.0-1.0)
            debug: Сохранять отладочные изображения
        
        Returns:
            Координаты центра найденного элемента (x, y) или None
        """
        if not template_path.exists():
            print(f"⚠️  Шаблон не найден: {template_path}")
            return None
        
        # Получаем скриншот
        screenshot = self.take_screenshot(page, "template_matching.png")
        template = cv2.imread(str(template_path))
        
        if template is None:
            print(f"⚠️  Не удалось загрузить шаблон: {template_path}")
            return None
        
        # Конвертируем в grayscale
        screenshot_gray = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)
        template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
        
        # Template matching
        result = cv2.matchTemplate(screenshot_gray, template_gray, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)
        
        if max_val >= confidence:
            # Нашли совпадение
            h, w = template_gray.shape
            top_left = max_loc
            bottom_right = (top_left[0] + w, top_left[1] + h)
            center = (top_left[0] + w // 2, top_left[1] + h // 2)
            
            print(f"    ✓ Шаблон найден! Уверенность: {max_val:.2%}")
            
            if debug:
                # Рисуем прямоугольник вокруг найденного элемента
                debug_img = screenshot.copy()
                cv2.rectangle(debug_img, top_left, bottom_right, (0, 255, 0), 3)
                cv2.putText(debug_img, f"Match: {max_val:.2%}", 
                          (top_left[0], top_left[1] - 10),
                          cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                
                debug_path = self.debug_dir / "template_found.png"
                cv2.imwrite(str(debug_path), debug_img)
                print(f"    Отладочное изображение: {debug_path}")
            
            return center
        else:
            print(f"    ✗ Шаблон не найден. Макс. совпадение: {max_val:.2%} (порог: {confidence:.2%})")
            return None
    
    def wait_for_element_visual(self, page: Page, detection_method: str = "input",
                               template_path: Optional[Path] = None,
                               timeout: int = 30, check_interval: float = 0.5,
                               confidence: float = 0.7, debug: bool = False) -> bool:
        """
        Ожидание появления элемента с помощью компьютерного зрения.
        
        Args:
            page: Объект страницы
            detection_method: Метод детекции ("input", "ocr", "template")
            template_path: Путь к шаблону (для method="template")
            timeout: Максимальное время ожидания (секунды)
            check_interval: Интервал между проверками (секунды)
            confidence: Порог уверенности для template matching
            debug: Сохранять отладочные изображения
        
        Returns:
            True если элемент найден, False если таймаут
        """
        print(f"🔍 Ожидание элемента через компьютерное зрение ({detection_method})...")
        
        start_time = time.time()
        checks = 0
        
        while time.time() - start_time < timeout:
            checks += 1
            elapsed = time.time() - start_time
            
            print(f"\r  Проверка #{checks} ({elapsed:.1f}s/{timeout}s)...", end="", flush=True)
            
            try:
                # Выбор метода детекции
                if detection_method == "input":
                    if self.find_input_field(page, debug=debug):
                        print("\n  ✓ Поле ввода обнаружено!")
                        return True
                
                elif detection_method == "ocr":
                    search_keywords = ["поиск", "search", "найти", "искать"]
                    if self.find_text_on_screen(page, search_keywords, debug=debug):
                        print("\n  ✓ Текст найден через OCR!")
                        return True
                
                elif detection_method == "template":
                    if template_path and self.find_element_by_template(
                        page, template_path, confidence, debug
                    ):
                        print("\n  ✓ Элемент найден по шаблону!")
                        return True
                
                else:
                    print(f"\n⚠️  Неизвестный метод: {detection_method}")
                    return False
                
            except Exception as e:
                print(f"\n⚠️  Ошибка при детекции: {e}")
            
            time.sleep(check_interval)
        
        print(f"\n  ✗ Таймаут! Элемент не найден за {timeout}s")
        return False
    
    def check_cards_table(self, page: Page, debug: bool = False) -> bool:
        """
        Проверка наличия таблицы с карточками тендеров через компьютерное зрение.
        
        Ищет признаки карточек:
        - Множественные прямоугольные области (карточки)
        - Текст "НАЧАЛЬНАЯ ЦЕНА", "СТАТУС", "ОРГАНИЗАТОР" и т.д.
        - Структурированные блоки с информацией
        
        Args:
            page: Объект страницы
            debug: Сохранять отладочные изображения
        
        Returns:
            True если карточки найдены
        """
        try:
            img = self.take_screenshot(page, "cards_check.png")
            
            # Метод 1: OCR поиск ключевых слов карточек
            if OCR_AVAILABLE:
                card_keywords = [
                    "НАЧАЛЬНАЯ ЦЕНА",
                    "СТАТУС",
                    "ОРГАНИЗАТОР",
                    "ЗАКАЗЧИК",
                    "Опубликовано",
                    "Подать заявку",
                    "ПОДРОБНЕЕ",
                    "card-item",
                ]
                
                if self.find_text_on_screen(page, card_keywords, debug=debug):
                    print("    ✓ Карточки тендеров обнаружены через OCR")
                    return True
            
            # Метод 2: Поиск множественных прямоугольных областей (карточки)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 50, 150)
            contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            # Фильтруем контуры по размеру (карточки обычно достаточно большие)
            h, w = gray.shape
            min_area = (w * h) * 0.01  # Минимум 1% от размера экрана
            max_area = (w * h) * 0.5   # Максимум 50% от размера экрана
            
            card_contours = [c for c in contours 
                           if min_area < cv2.contourArea(c) < max_area]
            
            # Если найдено несколько подходящих контуров, вероятно это карточки
            if len(card_contours) >= 2:
                print(f"    ✓ Найдено {len(card_contours)} потенциальных карточек")
                if debug:
                    debug_img = img.copy()
                    cv2.drawContours(debug_img, card_contours, -1, (0, 255, 0), 2)
                    debug_path = self.debug_dir / "cards_detected.png"
                    cv2.imwrite(str(debug_path), debug_img)
                    print(f"    Отладочное изображение: {debug_path}")
                return True
            
            return False
            
        except Exception as e:
            print(f"    ⚠️  Ошибка при проверке карточек: {e}")
            return False


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
        return "https://www.rts-tender.ru" + href
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
    """Поиск поля ввода для поискового запроса."""
    search_selectors = [
        # Приоритет: точный placeholder с rts-tender.ru
        "input[placeholder*='Введите ключевое слово или номер извещения']",
        "input[placeholder*='ключевое слово']",
        "input[placeholder*='номер извещения']",
        # Общие селекторы
        "input[type='search']",
        "input[name='search']",
        "input[name='q']",
        "input[name='query']",
        "input[name='text']",
        "#search",
        "#searchInput",
        ".search__input",
        ".search-input",
        "input[placeholder*='оиск']",
        "input[placeholder*='Поиск']",
        "input[placeholder*='Search']",
        "[data-testid='search-input']",
        ".header-search input",
        ".search-form input",
        "form[action*='search'] input[type='text']",
        ".main-search input",
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


def find_search_button(page: Page) -> Optional[Locator]:
    """Поиск кнопки 'НАЙТИ СЕЙЧАС' для отправки запроса."""
    button_selectors = [
        # Приоритет: точный текст кнопки
        "button:has-text('НАЙТИ СЕЙЧАС')",
        "button:has-text('Найти сейчас')",
        "button:has-text('Найти')",
        "a:has-text('НАЙТИ СЕЙЧАС')",
        "a:has-text('Найти сейчас')",
        # По атрибутам
        "button[type='submit']",
        "input[type='submit']",
        "button.search-button",
        ".search-button",
        "[data-testid='search-button']",
        # По цвету/стилю (красная кнопка)
        "button.btn-danger",
        "button.btn-red",
        ".btn-search",
    ]
    
    for selector in button_selectors:
        try:
            loc = page.locator(selector)
            if loc.count() > 0:
                first = loc.first
                if first.is_visible(timeout=1_000):
                    print(f"    ✓ Найдена кнопка поиска: {selector}")
                    return first
        except Exception:
            continue
    
    return None


def set_law_filters(page: Page, cfg: SearchConfig) -> bool:
    """
    Установка фильтров по правилам проведения закупок.
    
    Args:
        page: Объект страницы
        cfg: Конфигурация с выбранными фильтрами
    
    Returns:
        True если фильтры установлены успешно
    """
    try:
        print("\n🔧 Установка фильтров по правилам проведения...")
        
        # Ищем кнопку "Настройка поиска"
        settings_button_selectors = [
            ".main-search__settings",
            ".main-search__settings-btn",
            "span:has-text('Настройка поиска')",
            "[class*='settings']",
        ]
        
        settings_button = None
        for selector in settings_button_selectors:
            try:
                loc = page.locator(selector)
                if loc.count() > 0:
                    # Берем первый видимый элемент
                    for i in range(loc.count()):
                        elem = loc.nth(i)
                        if elem.is_visible(timeout=1_000):
                            settings_button = elem
                            print(f"    ✓ Найдена кнопка настроек: {selector}")
                            break
                    if settings_button:
                        break
            except Exception:
                continue
        
        if not settings_button:
            print("    ⚠️ Кнопка 'Настройка поиска' не найдена, пропускаем фильтры")
            return False
        
        # Кликаем на кнопку настроек
        try:
            settings_button.click()
            page.wait_for_timeout(1_500)
            print("    ✓ Открыто модальное окно настроек")
        except Exception as e:
            print(f"    ⚠️ Не удалось открыть настройки: {e}")
            return False
        
        # Определяем соответствие между конфигурацией и текстом на странице
        filters_map = [
            (cfg.law_44fz, "44-ФЗ"),
            (cfg.law_223fz, "223-ФЗ"),
            (cfg.law_615pp, "615-ПП"),
            (cfg.law_small_volume, "малого объёма"),
            (cfg.law_commercial, "Коммерческие"),
            (cfg.law_commercial_offers, "коммерческих предложений"),
        ]
        
        # Устанавливаем чекбоксы
        changed_count = 0
        for enabled, label_text in filters_map:
            try:
                # Ищем все чекбоксы с похожим текстом
                labels = page.locator(f"label:has-text('{label_text}')")
                
                if labels.count() == 0:
                    print(f"    ⚠️ Фильтр '{label_text}' не найден")
                    continue
                
                # Берем первый найденный
                label = labels.first
                
                # Пробуем разные способы найти checkbox
                checkbox = None
                
                # Способ 1: предыдущий sibling
                try:
                    checkbox = label.locator("xpath=preceding-sibling::input[@type='checkbox'][1]").first
                    if checkbox.count() > 0:
                        pass
                    else:
                        checkbox = None
                except:
                    checkbox = None
                
                # Способ 2: внутри родителя
                if not checkbox:
                    try:
                        parent = label.locator("xpath=..").first
                        checkbox = parent.locator("input[type='checkbox']").first
                        if checkbox.count() == 0:
                            checkbox = None
                    except:
                        checkbox = None
                
                # Способ 3: кликаем по самому label (часто работает)
                if not checkbox:
                    try:
                        # Проверяем, есть ли input внутри label
                        checkbox = label.locator("input[type='checkbox']").first
                        if checkbox.count() == 0:
                            # Если нет, кликаем по label - он должен переключить связанный checkbox
                            is_checked = "checked" in (label.get_attribute("class") or "")
                            if enabled != is_checked:
                                label.click()
                                page.wait_for_timeout(300)
                                changed_count += 1
                                print(f"    ✓ {'Включен' if enabled else 'Выключен'} фильтр: {label_text}")
                            else:
                                print(f"    • Фильтр '{label_text}' уже в нужном состоянии")
                            continue
                    except:
                        pass
                
                if not checkbox or checkbox.count() == 0:
                    print(f"    ⚠️ Чекбокс для '{label_text}' не найден, пропускаем")
                    continue
                
                # Проверяем текущее состояние
                try:
                    is_checked = checkbox.is_checked()
                except:
                    is_checked = False
                
                # Если нужное состояние не совпадает с текущим, кликаем
                if enabled != is_checked:
                    try:
                        checkbox.click()
                        page.wait_for_timeout(300)
                        changed_count += 1
                        print(f"    ✓ {'Включен' if enabled else 'Выключен'} фильтр: {label_text}")
                    except Exception as e:
                        print(f"    ⚠️ Не удалось кликнуть на чекбокс '{label_text}': {e}")
                else:
                    print(f"    • Фильтр '{label_text}' уже в нужном состоянии")
                
            except Exception as e:
                print(f"    ⚠️ Ошибка при установке фильтра '{label_text}': {e}")
                continue
        
        if changed_count > 0:
            print(f"    ✓ Изменено фильтров: {changed_count}")
        
        # Ищем и нажимаем кнопку "Применить" или "Сохранить", если она есть
        try:
            apply_button = page.locator("button:has-text('Применить'), button:has-text('Сохранить'), button:has-text('OK')").first
            if apply_button.count() > 0 and apply_button.is_visible(timeout=1_000):
                apply_button.click()
                page.wait_for_timeout(500)
                print("    ✓ Нажата кнопка применения фильтров")
        except Exception:
            pass
        
        # Закрываем модальное окно
        try:
            # Пробуем ESC - самый надежный способ
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
            print("    ✓ Модальное окно закрыто")
        except Exception as e:
            print(f"    ⚠️ Не удалось закрыть модальное окно: {e}")
        
        print("    ✅ Фильтры установлены")
        return True
        
    except Exception as e:
        print(f"    ⚠️ Ошибка при установке фильтров: {e}")
        import traceback
        traceback.print_exc()
        return False


def find_first_matching(page: Page, selectors: List[str]) -> Optional[Locator]:
    """Найти первый элемент, соответствующий любому из селекторов."""
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


# ---------------------------
# Сбор результатов со страницы
# ---------------------------

def collect_page_results(page: Page) -> List[TenderResult]:
    """Извлечение результатов поиска со страницы rts-tender.ru."""
    results: List[TenderResult] = []

    # Приоритет: структура rts-tender.ru с классом "cards" и "card-item"
    card_items = page.locator("div.cards div.card-item")
    
    if card_items.count() > 0:
        print(f"    Найдена структура rts-tender.ru ({card_items.count()} карточек)")
        
        for i in range(card_items.count()):
            card = card_items.nth(i)
            try:
                # Название тендера
                title_elem = card.locator("div.card-item__title").first
                if title_elem.count() == 0:
                    title_elem = card.locator("meta[itemprop='name']").first
                    if title_elem.count() > 0:
                        title = title_elem.get_attribute("content") or ""
                    else:
                        continue
                else:
                    title = title_elem.inner_text(timeout=2_000).strip()
                
                if not title:
                    continue
                
                # URL - ищем ссылку "ПОДРОБНЕЕ" или ссылку на детали
                url = ""
                detail_link = card.locator("a.button-red:has-text('ПОДРОБНЕЕ'), a[href*='/poisk/id/']").first
                if detail_link.count() > 0:
                    url = detail_link.get_attribute("href") or ""
                else:
                    # Fallback: любая ссылка в карточке
                    any_link = card.locator("a[href]").first
                    if any_link.count() > 0:
                        url = any_link.get_attribute("href") or ""
                
                # Начальная цена
                price_elem = card.locator("div.card-item__properties-cell:has-text('НАЧАЛЬНАЯ ЦЕНА')").locator("..").locator("div.card-item__properties-desc").first
                price = None
                if price_elem.count() > 0:
                    price_text = price_elem.inner_text(timeout=1_000)
                    price = _clean(price_text)
                
                # Статус
                status_cell = card.locator("div.card-item__properties-cell:has-text('СТАТУС')")
                status = None
                if status_cell.count() > 0:
                    status_elem = status_cell.locator("..").locator("div.card-item__properties-desc").first
                    if status_elem.count() > 0:
                        status_text = status_elem.inner_text(timeout=1_000)
                        status = _clean(status_text)
                
                # Дата публикации
                publish_elem = card.locator("time[itemprop='availabilityStarts']").first
                publish_date = None
                if publish_elem.count() > 0:
                    publish_text = publish_elem.inner_text(timeout=1_000)
                    publish_date = _clean(publish_text)
                
                # Срок подачи заявок
                deadline_elem = card.locator("time[itemprop='availabilityEnds']").first
                deadline = None
                if deadline_elem.count() > 0:
                    deadline_text = deadline_elem.inner_text(timeout=1_000)
                    deadline = _clean(deadline_text)
                
                # Организатор
                organizer_section = card.locator("div.card-item__organization-title:has-text('ОРГАНИЗАТОР')").locator("..")
                organizer = None
                if organizer_section.count() > 0:
                    organizer_elem = organizer_section.locator("a.text--bold, p a").first
                    if organizer_elem.count() > 0:
                        organizer_text = organizer_elem.inner_text(timeout=1_000)
                        organizer = _clean(organizer_text)
                
                # Заказчик
                customer_section = card.locator("div.card-item__organization-title:has-text('ЗАКАЗЧИК')").locator("..")
                customer = None
                if customer_section.count() > 0:
                    customer_elem = customer_section.locator("span.text--bold, p span.text--bold").first
                    if customer_elem.count() > 0:
                        customer_text = customer_elem.inner_text(timeout=1_000)
                        customer = _clean(customer_text)
                
                # ИНН и КПП
                inn_kpp_elem = card.locator("div.card-item__organization-main p:has-text('ИНН')").first
                inn = None
                kpp = None
                if inn_kpp_elem.count() > 0:
                    inn_kpp_text = inn_kpp_elem.inner_text(timeout=1_000)
                    inn_match = re.search(r'ИНН\s+(\d+)', inn_kpp_text)
                    kpp_match = re.search(r'КПП\s+(\d+)', inn_kpp_text)
                    if inn_match:
                        inn = inn_match.group(1)
                    if kpp_match:
                        kpp = kpp_match.group(1)
                
                # Регион
                region_elem = card.locator("a[href*='/poisk/region/']").first
                region = None
                if region_elem.count() > 0:
                    region = _clean(region_elem.inner_text(timeout=1_000))
                
                # Номер закупки в ЕИС
                registration_elem = card.locator("a[href*='star-pro.ru'], a[href*='Notification']").first
                registration_number = None
                if registration_elem.count() > 0:
                    reg_text = registration_elem.inner_text(timeout=1_000)
                    reg_match = re.search(r'№(\d+)', reg_text) if reg_text else None
                    if reg_match:
                        registration_number = reg_match.group(1)
                
                # Тип закона (223-ФЗ, 44-ФЗ)
                law_type_elem = card.locator("span.plate__item:has-text('ФЗ')").first
                law_type = None
                if law_type_elem.count() > 0:
                    law_type = _clean(law_type_elem.inner_text(timeout=1_000))
                
                # Тип закупки (АУКЦИОН и т.д.)
                purchase_type_elem = card.locator("span.plate__item:has-text('АУКЦИОН'), span.plate__item:has-text('КОНКУРС')").first
                purchase_type = None
                if purchase_type_elem.count() > 0:
                    purchase_type = _clean(purchase_type_elem.inner_text(timeout=1_000))
                
                # Площадка (РТС-ТЕНДЕР и т.д.)
                platform_elem = card.locator("a.link[href*='223.rts-tender.ru'], a.link[href*='etp']").first
                platform = None
                if platform_elem.count() > 0:
                    platform = _clean(platform_elem.inner_text(timeout=1_000))
                
                # Фильтруем старые тендеры (2021-2023)
                if deadline and any(year in str(deadline) for year in ['2021', '2022', '2023']):
                    continue
                if publish_date and any(year in str(publish_date) for year in ['2021', '2022', '2023']):
                    continue
                
                results.append(
                    TenderResult(
                        title=_clean(title) or title.strip(),
                        url=_normalize_url(url, page.url),
                        customer=customer,
                        organizer=organizer,
                        price=price,
                        status=status,
                        publish_date=publish_date,
                        deadline=deadline,
                        region=region,
                        inn=inn,
                        kpp=kpp,
                        registration_number=registration_number,
                        law_type=law_type,
                        purchase_type=purchase_type,
                        platform=platform,
                    )
                )
            except Exception as e:
                print(f"    ⚠️  Ошибка при парсинге карточки {i+1}: {e}")
                continue
        
        return results

    # Fallback: другие структуры
    card_selectors = [
        "div.search-results__item",
        "div.tender-card",
        "div.lot-card",
        ".search-result-item",
        ".search-results-item",
        "[data-testid='tender-item']",
        ".lot-item",
        ".purchase-item",
        ".tender-item",
        ".result-item",
        "article.tender",
        ".tenders-list__item",
        ".purchases-list__item",
    ]
    
    card_locator = find_first_matching(page, card_selectors)
    row_locator = page.locator("table.results tbody tr, table.tenders tbody tr, .results-table tbody tr")

    # Карточный layout
    if card_locator and card_locator.count() > 0:
        print(f"    Найден карточный layout ({card_locator.count()} карточек)")
        for i in range(card_locator.count()):
            card = card_locator.nth(i)
            try:
                link_loc = card.locator("a[href]").first
                if link_loc.count() == 0:
                    continue
                    
                title = link_loc.inner_text(timeout=2_000)
                url = link_loc.get_attribute("href") or ""
                
                if not title.strip():
                    continue
                    
            except Exception:
                continue
            
            customer = _safe_inner_text(card, ".customer, .organization, .tender-card__customer, [data-field='customer']")
            price = _safe_inner_text(card, ".price, .sum, .amount, .tender-card__price, [data-field='price']")
            publish_date = _safe_inner_text(card, ".date, .publish-date, .tender-card__date, [data-field='date']")
            deadline = _safe_inner_text(card, ".deadline, .end-date, .tender-card__deadline, [data-field='deadline']")
            
            results.append(
                TenderResult(
                    title=_clean(title) or title.strip(),
                    url=_normalize_url(url, page.url),
                    customer=_clean(customer),
                    price=_clean(price),
                    publish_date=_clean(publish_date),
                    deadline=_clean(deadline),
                )
            )
        return results

    # Табличный layout
    if row_locator.count() > 0:
        print(f"    Найден табличный layout ({row_locator.count()} строк)")
        for i in range(row_locator.count()):
            row = row_locator.nth(i)
            try:
                link_loc = row.locator("a[href]").first
                if link_loc.count() == 0:
                    continue
                    
                title = link_loc.inner_text(timeout=2_000)
                url = link_loc.get_attribute("href") or ""
                
                if not title.strip():
                    continue
                    
            except Exception:
                continue
                
            customer = _safe_inner_text(row, "td:nth-child(2)")
            price = _safe_inner_text(row, "td:nth-child(3)")
            publish_date = _safe_inner_text(row, "td:nth-child(4)")
            deadline = _safe_inner_text(row, "td:nth-child(5)")
            
            results.append(
                TenderResult(
                    title=_clean(title) or title.strip(),
                    url=_normalize_url(url, page.url),
                    customer=_clean(customer),
                    price=_clean(price),
                    publish_date=_clean(publish_date),
                    deadline=_clean(deadline),
                )
            )
        return results

    # Fallback
    print("    Карточки/таблицы не найдены, пробуем fallback...")
    fallback_links = page.locator("a[href*='tender'], a[href*='lot'], a[href*='purchase'], a[href*='auction']")
    
    if fallback_links.count() > 0:
        seen_urls = set()
        for i in range(min(fallback_links.count(), 100)):
            try:
                link = fallback_links.nth(i)
                title = link.inner_text(timeout=1_000)
                url = link.get_attribute("href") or ""
                
                if title.strip() and url and url not in seen_urls:
                    seen_urls.add(url)
                    results.append(
                        TenderResult(
                            title=_clean(title) or title.strip(),
                            url=_normalize_url(url, page.url),
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
        "[data-testid='next-page']",
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
                page.wait_for_load_state("domcontentloaded", timeout=60000)
                page.wait_for_timeout(3_000)
                return True
        except Exception:
            continue
    
    return False


# ---------------------------
# Основная логика поиска
# ---------------------------

def run_search(cfg: SearchConfig) -> List[TenderResult]:
    """Основная функция поиска тендеров."""
    
    print(f"Запуск Playwright Chromium (headless={cfg.headless})...")
    
    # Инициализация компьютерного зрения
    cv = None
    if cfg.use_cv and OPENCV_AVAILABLE:
        cv = ComputerVision(debug_dir=Path("cv_debug"))
        print("✓ Компьютерное зрение (OpenCV) активировано")
    elif cfg.use_cv and not OPENCV_AVAILABLE:
        print("⚠️  OpenCV недоступен, компьютерное зрение отключено")
    
    # Используем обычный Playwright с Chromium
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
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        page = context.new_page()
        # Увеличиваем таймауты для медленных серверов
        page.set_default_navigation_timeout(60000)  # 60 секунд
        page.set_default_timeout(60000)

        try:
            # Шаг 1: Загружаем главную страницу
            print("Загрузка rts-tender.ru...")
            # Используем более быструю стратегию загрузки для медленных серверов
            page.goto("https://www.rts-tender.ru/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3_000)
            
            # ⭐ Шаг 2: Ожидание поисковой строки через компьютерное зрение
            if cv:
                print("")
                print("┌" + "─" * 58 + "┐")
                print(f"│ {'ДЕТЕКЦИЯ ПОИСКОВОЙ СТРОКИ (COMPUTER VISION)':^56} │")
                print("└" + "─" * 58 + "┘")
                
                # Попробуем несколько методов детекции
                found = False
                
                # Метод 1: Поиск input поля по визуальным признакам
                print("Метод 1: Детекция input полей...")
                if cv.wait_for_element_visual(
                    page, 
                    detection_method="input",
                    timeout=cfg.cv_timeout,
                    check_interval=cfg.cv_check_interval,
                    debug=cfg.save_debug_images
                ):
                    found = True
                
                # Метод 2: OCR (если доступен)
                if not found and OCR_AVAILABLE:
                    print("\nМетод 2: OCR поиск текста...")
                    if cv.wait_for_element_visual(
                        page,
                        detection_method="ocr",
                        timeout=10,
                        check_interval=cfg.cv_check_interval,
                        debug=cfg.save_debug_images
                    ):
                        found = True
                
                if not found:
                    print("\n⚠️  Компьютерное зрение не обнаружило элемент, переходим к обычному поиску...")
            
            # Шаг 3: Классический поиск поля ввода
            search_box = find_search_input(page)
            
            if search_box is None:
                alternative_urls = [
                    "https://www.rts-tender.ru/poisk",
                    "https://www.rts-tender.ru/search",
                    "https://www.rts-tender.ru/tenders",
                    "https://www.rts-tender.ru/purchases",
                ]
                
                for url in alternative_urls:
                    print(f"  Пробуем {url}...")
                    try:
                        page.goto(url, wait_until="domcontentloaded")
                        page.wait_for_timeout(2_000)
                        
                        # Используем CV для каждой альтернативной страницы
                        if cv:
                            cv.wait_for_element_visual(
                                page,
                                detection_method="input",
                                timeout=5,
                                check_interval=0.5,
                                debug=False
                            )
                        
                        search_box = find_search_input(page)
                        if search_box:
                            break
                    except Exception:
                        continue

            if search_box is None:
                screenshot_path = Path("debug_screenshot.png")
                page.screenshot(path=str(screenshot_path))
                print(f"  Скриншот сохранён: {screenshot_path}")
                raise RuntimeError(
                    "Не найдено поле поиска на rts-tender.ru. "
                    "Возможно, изменилась структура сайта. "
                    "Проверьте скриншот debug_screenshot.png"
                )

            # Шаг 4: Заполняем поисковый запрос
            print(f"\nВыполняем поиск: '{cfg.query}'")
            search_box.click()
            search_box.fill(cfg.query)
            page.wait_for_timeout(500)
            
            # Шаг 5: Устанавливаем фильтры по правилам проведения (ДО нажатия кнопки поиска)
            set_law_filters(page, cfg)
            
            # Шаг 6: Ищем кнопку "НАЙТИ СЕЙЧАС" и нажимаем её
            search_button = find_search_button(page)
            if search_button:
                print("    Нажатие кнопки 'НАЙТИ СЕЙЧАС'...")
                search_button.click()
            else:
                # Fallback: используем Enter, если кнопка не найдена
                print("    Кнопка не найдена, используем Enter...")
                search_box.press("Enter")
            
            # Ждём загрузки результатов
            print("    Ожидание загрузки результатов...")
            # Используем domcontentloaded для быстрой загрузки на медленных серверах
            page.wait_for_load_state("domcontentloaded", timeout=60000)
            page.wait_for_timeout(5_000)  # Дополнительное ожидание для динамических результатов
            print("    ✓ Результаты загружены")
            
        except PlaywrightTimeoutError as e:
            raise RuntimeError(f"Таймаут при загрузке страницы: {e}")

        # Таймаут перед парсингом
        print("")
        print("┌" + "─" * 58 + "┐")
        print(f"│ {'ОЖИДАНИЕ ПЕРЕД ПАРСИНГОМ (20 СЕКУНД)':^56} │")
        print("└" + "─" * 58 + "┘")
        countdown_timer(cfg.parse_delay // 1000, "⏱️  Таймаут")
        print("")
        
        # Шаг 7: Проверка наличия карточек через OpenCV (если включено)
        if cfg.use_cv and OPENCV_AVAILABLE:
            cv = ComputerVision(debug_dir=Path("cv_debug"))
            print("")
            print("┌" + "─" * 58 + "┐")
            print(f"│ {'ПРОВЕРКА КАРТОЧЕК ЧЕРЕЗ OPENCV':^56} │")
            print("└" + "─" * 58 + "┘")
            if cv.check_cards_table(page, debug=cfg.save_debug_images):
                print("  ✓ Карточки тендеров обнаружены через компьютерное зрение")
            else:
                print("  ⚠️  Карточки не обнаружены через OpenCV, продолжаем парсинг...")
            print("")
        
        # Шаг 8: Собираем результаты
        collected: List[TenderResult] = []

        for page_idx in range(cfg.pages):
            print(f"┌{'─' * 58}┐")
            print(f"│ Страница {page_idx + 1}/{cfg.pages}{' ' * (50 - len(f'Страница {page_idx + 1}/{cfg.pages}'))}│")
            print(f"└{'─' * 58}┘")
            
            page_results = collect_page_results(page)
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

        # Закрываем браузер
        context.close()
        browser.close()
        return collected


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
        description="Парсер результатов поиска rts-tender.ru через Playwright с компьютерным зрением.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  python rts_parser.py "строительство"
  python rts_parser.py "медицинское оборудование" -p 3 --use-cv
  python rts_parser.py "IT услуги" --no-headless --save-cv-debug
  python rts_parser.py "ремонт" --parse-delay 30 --cv-timeout 60
        """
    )
    
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-p", "--pages", type=int, default=1, 
                       help="Количество страниц для парсинга (по умолчанию: 1)")
    parser.add_argument("-o", "--output", type=Path, default=Path("rts_results.json"),
                       help="Файл для сохранения результатов (по умолчанию: rts_results.json)")
    parser.add_argument("--headless", action="store_true", default=True,
                       help="Запуск без интерфейса браузера (по умолчанию)")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                       help="Запуск с видимым окном браузера")
    parser.add_argument("--timeout", type=int, default=30_000,
                       help="Таймаут навигации в миллисекундах (по умолчанию: 30000)")
    parser.add_argument("--parse-delay", type=int, default=20,
                       help="Задержка перед парсингом каждой страницы в секундах (по умолчанию: 20)")
    
    # Параметры компьютерного зрения
    cv_group = parser.add_argument_group("Компьютерное зрение (OpenCV)")
    cv_group.add_argument("--use-cv", action="store_true", default=True,
                         help="⭐ Использовать компьютерное зрение для детекции (по умолчанию)")
    cv_group.add_argument("--no-cv", dest="use_cv", action="store_false",
                         help="Отключить компьютерное зрение")
    cv_group.add_argument("--cv-timeout", type=int, default=30,
                         help="Таймаут ожидания элемента через CV в секундах (по умолчанию: 30)")
    cv_group.add_argument("--cv-check-interval", type=float, default=0.5,
                         help="Интервал проверки CV в секундах (по умолчанию: 0.5)")
    cv_group.add_argument("--cv-confidence", type=float, default=0.7,
                         help="Порог уверенности для template matching 0-1 (по умолчанию: 0.7)")
    cv_group.add_argument("--save-cv-debug", action="store_true",
                         help="Сохранять отладочные изображения CV в папку cv_debug/")
    
    args = parser.parse_args(argv)
    
    return SearchConfig(
        query=args.query,
        pages=args.pages,
        output=args.output,
        headless=args.headless,
        navigation_timeout=args.timeout,
        parse_delay=args.parse_delay * 1000,
        use_cv=args.use_cv,
        cv_timeout=args.cv_timeout,
        cv_check_interval=args.cv_check_interval,
        cv_confidence=args.cv_confidence,
        save_debug_images=args.save_cv_debug,
    )


def main(argv: List[str]) -> int:
    """Точка входа в программу."""
    cfg = parse_args(argv)
    start = time.time()
    
    print("")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 6 + "ПАРСЕР RTS-TENDER.RU (PLAYWRIGHT + OPENCV)" + " " * 10 + "║")
    print("╚" + "═" * 58 + "╝")
    print(f"  Запрос:          {cfg.query}")
    print(f"  Страниц:         {cfg.pages}")
    print(f"  Headless:        {cfg.headless}")
    print(f"  Задержка:        {cfg.parse_delay // 1000} секунд")
    print(f"  🔍 CV детекция:  {'ВКЛ' if cfg.use_cv else 'ВЫКЛ'}")
    if cfg.use_cv:
        print(f"  CV таймаут:      {cfg.cv_timeout} сек")
        print(f"  CV отладка:      {'ВКЛ' if cfg.save_debug_images else 'ВЫКЛ'}")
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