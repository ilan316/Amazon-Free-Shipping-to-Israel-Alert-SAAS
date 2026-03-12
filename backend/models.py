from datetime import datetime
from sqlalchemy import String, Integer, Boolean, Text, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    notify_email: Mapped[str] = mapped_column(String(255), nullable=False)
    language: Mapped[str] = mapped_column(String(2), nullable=False, default="he")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    user_products: Mapped[list["UserProduct"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    notifications: Mapped[list["NotificationLog"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asin: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    last_status: Mapped[str] = mapped_column(String(20), nullable=False, default="UNKNOWN")
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    found_in_aod: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    consecutive_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    user_products: Mapped[list["UserProduct"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    notifications: Mapped[list["NotificationLog"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class UserProduct(Base):
    __tablename__ = "user_products"
    __table_args__ = (UniqueConstraint("user_id", "product_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    custom_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="user_products")
    product: Mapped["Product"] = relationship(back_populates="user_products")


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    email_to: Mapped[str] = mapped_column(String(255), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="notifications")
    product: Mapped["Product"] = relationship(back_populates="notifications")
