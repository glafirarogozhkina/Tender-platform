#!/usr/bin/env python3
"""
Парсер ЭТП ГПБ (https://new.etpgpb.ru/)
Поиск: API GET /api/v2/procedures/?search=<запрос>&page=1&per=20&sort=by_relevance&procedure[stage][0]=accepting.
Поле ввода на сайте: input.n-input__input-el, placeholder «Поиск».
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Any


# ---------------------------
# Конфигурация и структуры данных
# ---------------------------

@dataclass
class SearchConfig:
    query: str
    pages: int = 1
    output: Path = Path("etpgpb_results.json")
    headless: bool = True
    navigation_timeout: int = 60_000
    parse_delay: int = 3_000
    per_page: int = 20


@dataclass
class TenderResult:
    title: str
    url: str
    source: str = "ETPGPB"
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


BASE_URL = "https://new.etpgpb.ru"
API_URL = "https://new.etpgpb.ru/api/v2/procedures/"


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return " ".join(str(value).split()).strip() or None


def _format_date(iso_str: Optional[str]) -> Optional[str]:
    """2026-02-02T15:40:00.000+03:00 -> 02.02.2026 15:40"""
    if not iso_str:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", str(iso_str))
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)} {m.group(4)}:{m.group(5)}"
    return _clean(iso_str)


def _build_api_url(query: str, page: int = 1, per: int = 20) -> str:
    params = {
        "page": str(page),
        "per": str(per),
        "search": query,
        "sort": "by_relevance",
        "procedure[stage][0]": "accepting",
    }
    return API_URL + "?" + urllib.parse.urlencode(params, encoding="utf-8")


def _fetch_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ru,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_api_response(data: Any) -> List[TenderResult]:
    results: List[TenderResult] = []
    items = data.get("data") if isinstance(data, dict) else []
    if not isinstance(items, list):
        return results
    for item in items:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue
        title = _clean(attrs.get("title")) or "Процедура"
        url = _clean(attrs.get("platform_url"))
        if not url and attrs.get("rebranding_truncated_path"):
            url = BASE_URL + attrs.get("rebranding_truncated_path", "")
        if not url:
            url = BASE_URL + "/procedures/"
        organizer = _clean(attrs.get("company_name"))
        amount = attrs.get("amount")
        currency = attrs.get("currency_name") or "RUB"
        price = None
        if amount is not None:
            price = f"{amount} {currency}".strip()
        publish_date = _format_date(attrs.get("date_published"))
        deadline = _format_date(attrs.get("end_registration"))
        tender_id = _clean(attrs.get("registry_number"))
        purchase_type = _clean(attrs.get("procedure_type_name") or attrs.get("custom_procedure_type_name"))
        lot_regions = attrs.get("lot_regions")
        region = None
        if isinstance(lot_regions, list) and lot_regions:
            region = _clean(lot_regions[0])
        stage = attrs.get("stage")
        status = None
        if stage == "accepting":
            status = "Приём заявок"
        elif stage:
            status = str(stage)
        results.append(
            TenderResult(
                title=title,
                url=url,
                organizer=organizer,
                price=price,
                status=status,
                publish_date=publish_date,
                deadline=deadline,
                region=region,
                tender_id=tender_id,
                purchase_type=purchase_type,
            )
        )
    return results


def run_search(cfg: SearchConfig) -> List[TenderResult]:
    """Поиск на ЭТП ГПБ через API v2 (JSON)."""
    print("Запрос к API new.etpgpb.ru...")
    collected: List[TenderResult] = []
    for page_num in range(1, cfg.pages + 1):
        url = _build_api_url(cfg.query, page=page_num, per=cfg.per_page)
        try:
            data = _fetch_json(url)
            page_results = _parse_api_response(data)
            collected.extend(page_results)
            print(f"  Страница {page_num}/{cfg.pages}: собрано {len(page_results)} (всего: {len(collected)})")
            if len(page_results) < cfg.per_page:
                break
        except Exception as e:
            print(f"  Ошибка страницы {page_num}: {e}")
            break
    return collected


def save_results(path: Path, results: List[TenderResult]) -> None:
    data = [asdict(r) for r in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: List[str]) -> SearchConfig:
    parser = argparse.ArgumentParser(
        description="Парсер ЭТП ГПБ (new.etpgpb.ru), поиск по API, поле «Поиск» на сайте."
    )
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-p", "--pages", type=int, default=1)
    parser.add_argument("-o", "--output", type=Path, default=Path("etpgpb_results.json"))
    parser.add_argument("--timeout", type=int, default=60_000)
    args = parser.parse_args(argv)
    return SearchConfig(
        query=args.query,
        pages=args.pages,
        output=args.output,
        navigation_timeout=args.timeout,
    )


def main(argv: List[str]) -> int:
    cfg = parse_args(argv)
    print(f"ЭТП ГПБ. Запрос: {cfg.query}, страниц: {cfg.pages}")
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
