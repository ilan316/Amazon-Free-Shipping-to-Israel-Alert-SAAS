from datetime import datetime
from pydantic import BaseModel, EmailStr


# ── Auth ──────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    notify_email: EmailStr
    language: str = "he"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ── User ──────────────────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    id: int
    email: str
    notify_email: str
    language: str
    created_at: datetime
    is_admin: bool = False
    vacation_mode: bool = False
    max_products: int | None = None
    effective_product_limit: int | None = None

    model_config = {"from_attributes": True}


class UpdateSettingsRequest(BaseModel):
    notify_email: EmailStr | None = None
    language: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class DeleteAccountRequest(BaseModel):
    password: str


# ── Products ──────────────────────────────────────────────────────────────────

class AddProductRequest(BaseModel):
    url_or_asin: str
    custom_name: str | None = None


class ProductResponse(BaseModel):
    asin: str
    name: str
    custom_name: str | None
    url: str
    last_status: str
    last_checked: datetime | None
    found_in_aod: bool
    last_notified: datetime | None
    added_at: datetime
    is_paused: bool = False
    raw_text: str = ""
    affiliate_url: str = ""

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    message: str
