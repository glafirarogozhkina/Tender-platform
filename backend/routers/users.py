"""API пользователей."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import User, Favorite, Tender
from passlib.context import CryptContext
from jose import jwt
import os
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/users", tags=["users"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_EXPIRE = 60 * 60 * 24  # 24 часа


class UserCreate(BaseModel):
    email: str
    password: str
    name: Optional[str] = None


class UserLogin(BaseModel):
    email: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


def _hash(password: str) -> str:
    return pwd_context.hash(password)


def _verify(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def _create_token(sub: str) -> str:
    expire = datetime.utcnow() + timedelta(seconds=ACCESS_EXPIRE)
    return jwt.encode({"sub": sub, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


@router.post("/register", response_model=Token)
def register(data: UserCreate, db: Session = Depends(get_db)):
    """Регистрация."""
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(400, "Пользователь с таким email уже есть")
    user = User(email=data.email, hashed_password=_hash(data.password), name=data.name)
    db.add(user)
    db.commit()
    db.refresh(user)
    return Token(access_token=_create_token(str(user.id)))


@router.post("/login", response_model=Token)
def login(data: UserLogin, db: Session = Depends(get_db)):
    """Вход."""
    user = db.query(User).filter(User.email == data.email).first()
    if not user or not _verify(data.password, user.hashed_password):
        raise HTTPException(401, "Неверный email или пароль")
    if not user.is_active:
        raise HTTPException(403, "Аккаунт заблокирован")
    return Token(access_token=_create_token(str(user.id)))


@router.post("/favorites/{tender_id}")
def add_favorite(tender_id: int, user_id: int = 1, db: Session = Depends(get_db)):
    """Добавить тендер в избранное (упрощённо: user_id=1)."""
    fav = Favorite(user_id=user_id, tender_id=tender_id)
    db.add(fav)
    db.commit()
    return {"ok": True}


@router.delete("/favorites/{tender_id}")
def remove_favorite(tender_id: int, user_id: int = 1, db: Session = Depends(get_db)):
    """Удалить из избранного."""
    db.query(Favorite).filter(Favorite.user_id == user_id, Favorite.tender_id == tender_id).delete()
    db.commit()
    return {"ok": True}


@router.get("/me")
def me(user_id: int = 1, db: Session = Depends(get_db)):
    """Профиль (упрощённо)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"error": "Not found"}, 404
    return {"id": user.id, "email": user.email, "name": user.name}
