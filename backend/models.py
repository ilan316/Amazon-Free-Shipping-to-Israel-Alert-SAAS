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
    # True = vacation was set by the inactivity scheduler, so a login/click wakes the
    # user up automatically. False = the user asked for it — never auto-resume.
    vacation_auto: Mapped[bool] = mapped_column(Boolean, default=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    google_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    max_products: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = use global default
    automation_activation_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    automation_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    automation_expansion_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    automation_reengagement_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    automation_winback_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    last_price: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Extra cost Amazon charges on top of last_price for delivery to Israel.
    # kind: 'combined' (paid shipping + import merged by Amazon, cannot be split),
    #       'import_only' (free shipping, this is the import deposit),
    #       'free' (nothing extra), or NULL when unknown.
    israel_extra_cost: Mapped[str | None] = mapped_column(String(50), nullable=True)
    israel_cost_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_urls: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of up to 4 URLs
    name_he: Mapped[str | None] = mapped_column(String(300), nullable=True)
    amazon_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    free_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="user")

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
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    no_click_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    template_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("email_templates.id", ondelete="CASCADE"), nullable=True)
    template_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_suspicious: Mapped[bool] = mapped_column(Boolean, default=False)


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


class TelegramSent(Base):
    __tablename__ = "telegram_sent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asin: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class FacebookSent(Base):
    __tablename__ = "facebook_sent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asin: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class CategoryTranslation(Base):
    __tablename__ = "category_translations"

    english_name: Mapped[str] = mapped_column(String(200), primary_key=True)
    hebrew_name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class BlogPublishedAsin(Base):
    __tablename__ = "blog_published_asins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asin: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=True)
    marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    # Set by the blog-social drain when the post is actually broadcast (NULL = not sent / not tracked)
    telegram_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    facebook_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class BlogDismissedAsin(Base):
    __tablename__ = "blog_dismissed_asins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asin: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    dismissed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class BlogDraft(Base):
    __tablename__ = "blog_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asin: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    title_short: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    israel_price: Mapped[float] = mapped_column(nullable=True)
    amazon_price: Mapped[float] = mapped_column(nullable=True)
    image_url: Mapped[str] = mapped_column(String(512), nullable=True)
    min_order_49: Mapped[bool] = mapped_column(default=False, nullable=False)
    voltage_warning: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class BlogDraftJob(Base):
    """One queued draft inside a batch run.

    A batch POST creates these rows as `pending` and returns immediately; a
    background worker flips each to `running` and then `done`/`failed`. The admin
    UI polls by `batch_id` instead of holding a 1-3 minute request open.
    """
    __tablename__ = "blog_draft_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    asin: Mapped[str] = mapped_column(String(10), nullable=False)
    israel_price: Mapped[float] = mapped_column(nullable=True)
    amazon_price: Mapped[float] = mapped_column(nullable=True)
    min_order_49: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    voltage_warning: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BlogSocialQueue(Base):
    __tablename__ = "blog_social_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # NULL for editorial guides — they have no product behind them
    asin: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # "review" (product post) or "guide" (editorial guide) — drives the caption wording
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="review")
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    israel_price: Mapped[float] = mapped_column(nullable=True)
    amazon_price: Mapped[float] = mapped_column(nullable=True)
    image_url: Mapped[str] = mapped_column(String(512), nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    telegram_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    facebook_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
