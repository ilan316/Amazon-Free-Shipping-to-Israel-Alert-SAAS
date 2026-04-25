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
    vacation_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    google_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    max_products: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = use global default
    automation_activation_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    automation_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    automation_expansion_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notify_email_bounced: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_email_bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notify_email_bounce_type: Mapped[str | None] = mapped_column(String(20), nullable=True)

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
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
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


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")


class EmailClick(Base):
    __tablename__ = "email_clicks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    asin: Mapped[str] = mapped_column(String(10), nullable=False)
    clicked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dest_url: Mapped[str] = mapped_column(String(512), nullable=False)


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class EmailOpen(Base):
    __tablename__ = "email_opens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    template_id: Mapped[int] = mapped_column(Integer, ForeignKey("email_templates.id", ondelete="CASCADE"), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class EmailSendLog(Base):
    __tablename__ = "email_send_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("email_templates.id", ondelete="SET NULL"), nullable=True)
    template_name: Mapped[str] = mapped_column(String(100), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    audience: Mapped[str] = mapped_column(String(50), nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)


class EmailSendRecipient(Base):
    __tablename__ = "email_send_recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    send_log_id: Mapped[int] = mapped_column(Integer, ForeignKey("email_send_logs.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
