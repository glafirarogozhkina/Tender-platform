"""API тендеров."""
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func

from database import get_db
from models import Tender
from ml_service import enrich_tender

router = APIRouter(prefix="/api/tenders", tags=["tenders"])


@router.get("")
def list_tenders(
    q: Optional[str] = Query(None, description="Поиск по названию, заказчику"),
    source: Optional[str] = Query(None, description="Фильтр по источнику"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Список тендеров с поиском и фильтрацией."""
    qb = db.query(Tender)
    if q:
        pattern = f"%{q}%"
        qb = qb.filter(
            or_(
                Tender.title.ilike(pattern),
                and_(Tender.customer.isnot(None), Tender.customer.ilike(pattern)),
            )
        )
    if source:
        qb = qb.filter(Tender.source.ilike(f"%{source}%"))
    total = qb.count()
    items = qb.order_by(Tender.id.desc()).offset(offset).limit(limit).all()

    # Подсчёт тендеров по заказчику для репутации
    customer_counts = {}
    for t in items:
        if t.customer:
            customer_counts[t.customer] = customer_counts.get(t.customer, 0) + 1

    result = []
    for t in items:
        d = t.to_dict()
        cnt = customer_counts.get(t.customer, 0) if t.customer else 0
        enrich_tender(d, cnt)
        result.append(d)

    return {"total": total, "items": result, "limit": limit, "offset": offset}

