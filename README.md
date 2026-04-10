# Amazon Free Shipping to Israel Alert SaaS

SaaS שמאפשר למשתמשים לעקוב אחרי מוצרים באמזון ולקבל התראות כשמשלוח חינם לישראל זמין.

## Features
- מעקב אחרי מוצרי אמזון
- בדיקה אוטומטית של זמינות משלוח חינם לישראל
- התראות במייל (Resend)
- דשבורד למשתמש
- פאנל אדמין
- Chrome Extension

## Tech Stack
- **Backend:** FastAPI + PostgreSQL + Alembic
- **Scraping:** Playwright + curl-cffi
- **Deploy:** Railway

## Getting Started

```bash
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

## Deploy
Railway — push to `main` triggers automatic deploy.
