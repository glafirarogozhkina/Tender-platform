"""API поиска: запуск парсера, сохранение в БД, логи, фильтры по дате."""
import io
import sys
import threading
import time
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import BaseModel

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PARSERS_DIR = _PROJECT_ROOT / "parsers"
for _path in (_PROJECT_ROOT, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from database import SessionLocal
from models import Tender
from ml_service import _parse_price, enrich_tender

router = APIRouter(prefix="/api/search", tags=["search"])

_search_tasks = {}
_search_counter = 0


class SearchRequest(BaseModel):
    query: str
    pages: int = 1
    sources: list[str] = ["rts", "rutend"]
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    min_days_left: Optional[int] = None


class _TeeOutput(io.TextIOBase):
    def __init__(self, original, logs: list):
        self._original = original
        self._logs = logs

    def write(self, text):
        if text and text.strip():
            self._logs.append(text.rstrip())
        if self._original:
            self._original.write(text)
        return len(text) if text else 0

    def flush(self):
        if self._original:
            self._original.flush()


_DATE_PATTERNS = [
    (r'(\d{2})\.(\d{2})\.(\d{4})', '%d.%m.%Y'),
    (r'(\d{4})-(\d{2})-(\d{2})', '%Y-%m-%d'),
    (r'(\d{2})/(\d{2})/(\d{4})', '%d/%m/%Y'),
]

