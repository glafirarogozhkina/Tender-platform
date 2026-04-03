"""Модели БД."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class Tender(Base):
    """Тендер."""
    __tablename__ = "tenders"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(String(128), index=True)  # ID на площадке
    title = Column(String(1024), nullable=False)
    url = Column(String(2048))
    source = Column(String(64), index=True)
    price_raw = Column(String(128))  # Оригинальная строка цены
    price_numeric = Column(Float)  # Числовое значение для ML
    customer = Column(String(512))
    organizer = Column(String(512))
    law_type = Column(String(64))
    purchase_type = Column(String(128))
    deadline = Column(String(128))
    status = Column(String(128))
    region = Column(String(256))
    platform = Column(String(256))
    publish_date = Column(String(128))
    extra = Column(Text)  # JSON с доп. полями
    created_at = Column(DateTime, default=datetime.utcnow)
    search_query = Column(String(256), index=True)  # Запрос, по которому найден

    # ML-поля
    predicted_price = Column(Float)
    risk_score = Column(Float)  # 0–1, риск «подставного» тендера
    customer_reputation = Column(Float)  # 0–1, репутация заказчика

    def to_dict(self):
        d = {
            "id": self.id,
            "tender_id": self.tender_id,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "price": self.price_raw,
            "price_numeric": self.price_numeric,
            "customer": self.customer,
            "organizer": self.organizer,
            "law_type": self.law_type,
            "purchase_type": self.purchase_type,
            "deadline": self.deadline,
            "status": self.status,
            "region": self.region,
            "platform": self.platform,
            "publish_date": self.publish_date,
        }
        if self.predicted_price is not None:
            d["predicted_price"] = round(self.predicted_price, 2)
        if self.risk_score is not None:
            d["risk_score"] = round(self.risk_score, 2)
        if self.customer_reputation is not None:
            d["customer_reputation"] = round(self.customer_reputation, 2)
        return d


class User(Base):
    """Пользователь."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(256), unique=True, index=True, nullable=False)
    hashed_password = Column(String(256), nullable=False)
    name = Column(String(256))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Favorite(Base):
    """Избранные тендеры пользователя."""
    __tablename__ = "favorites"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    tender_id = Column(Integer, ForeignKey("tenders.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
