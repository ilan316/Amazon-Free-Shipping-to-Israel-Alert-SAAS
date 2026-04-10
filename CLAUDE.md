# Amazon Free Shipping to Israel Alert SaaS

SaaS שמתריע למשתמשים כאשר מוצרים באמזון מציעים משלוח חינם לישראל.

## Tech Stack
- **Backend:** Python, FastAPI, SQLAlchemy (async), asyncpg, Alembic, APScheduler
- **Frontend:** HTML/CSS/JS (סטטי, מוגש מה-backend)
- **DB:** PostgreSQL (asyncpg)
- **Scraping:** Playwright, curl-cffi, BeautifulSoup4
- **Auth:** python-jose (JWT), passlib/bcrypt
- **Email:** Resend
- **Rate limiting:** slowapi

## Git
- **Remote:** https://github.com/ilan316/Amazon-Free-Shipping-to-Israel-Alert-SAAS.git
- **Branch strategy:** main → production

## Deploy
- **Platform:** Railway
- **Start command:** `uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 1`
- **Health check:** GET /health

## Response Language
תשובות בעברית בלבד.

## Workflow Rules
1. **תמיד היכנס ל-Plan Mode לפני שינויים**
2. אחרי כל שינוי: `git add` → `git commit` → `git push`
3. אחרי push לעולם אל תשכח לבדוק לוגים ב-Railway
