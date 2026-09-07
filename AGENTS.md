# Amazon Free Shipping to Israel Alert — SaaS (Backend)

> ⚠️ קובץ זה זהה ל-`CLAUDE.md` / `AGENTS.md` באותה תיקייה. כל עדכון חייב להיכתב **בשני הקבצים**.

## תיאור
הבאקאנד והדשבורד של השירות: המשתמש מוסיף מוצרי Amazon למעקב, המערכת בודקת אותם בקצב קבוע, ושולחת **מייל סיכום יומי** כשמזוהה משלוח חינם לישראל. כולל פאנל אדמין, מעקב קליקים, קישורי אפיליאייט, השהיית מוצרים ומצב חופשה.

רץ על `app.amzfreeil.com`. אתר השיווק (`www.amzfreeil.com`) הוא פרויקט נפרד — `amzfreeil-www`.

**השירות web-only.** אין יותר אפליקציית Windows — כל מסר שמרמז אחרת הוא באג.

## Tech Stack
- **Backend:** Python, FastAPI, SQLAlchemy (async), asyncpg, Alembic, APScheduler
- **Frontend:** HTML/CSS/JS סטטי, מוגש מה-backend
- **DB:** PostgreSQL (Railway)
- **Scraping:** curl-cffi (ראשי) + Playwright (fallback) + BeautifulSoup4
- **Auth:** python-jose (JWT), passlib/bcrypt
- **Email:** Resend
- **Rate limiting:** slowapi

## מבנה קבצים (`backend/`)
| קובץ | תפקיד |
|---|---|
| `main.py` | כל ה-routes, ה-API והאדמין |
| `checker.py` | בדיקת המוצר באמזון — משלוח חינם, מחיר, כותרת, זמינות |
| `scheduler.py` | APScheduler — בדיקות תקופתיות, מייל סיכום יומי, אוטומציות, `hebrew_backfill` |
| `notifier.py` | בניית ושליחת המיילים ב-Resend |
| `models.py` / `schemas.py` | ORM ו-Pydantic |
| `auth.py` | JWT + הרשאות |
| `database.py` | חיבור async ל-Postgres |
| `vacation.py` | לוגיקת מצב חופשה והשהיה |
| `blog_utils.py` | יצירת דראפטים לבלוג |
| `image_overlay.py` | תמונות לפוסטים |

## מלכודות מוכרות ב-`checker.py`
- **עלות סופית לישראל** נלקחת מ-`amazonGlobal_feature_div` — הטבלה המפורטת, **לא** שורת הסיכום. ה-"FREE" של אמזון מותנה בסף הזמנה.
- מחירים ב-ILS ו-`exports_feature_div` דורשים טיפול נפרד.
- שגיאות ידועות בלוגים: `curl:56` = תקלת proxy/תשתית (לא מכסה), CAPTCHA, `productTitle` חסר.
- **תמונות מוצר:** להשתמש ב-`image_url` מה-DB. ה-URL הישן `images-na.../P/{ASIN}.01` מחזיר 200 עם פיקסל 43B ולא 404 — ולכן `onerror` לא נורה והמשתמש רואה ריבוע ריק.

## Git ו-Deploy
- **Remote:** https://github.com/ilan316/Amazon-Free-Shipping-to-Israel-Alert-SAAS.git · branch `main`
- **Deploy:** Railway (CLI מחובר ישירות) — פרויקט `aware-wisdom`
- **Start:** `uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 1`
- **Health check:** `GET /health`

## אבחון תקלות פרודקשן
- **נפילות UptimeRobot** = freeze של הקונטיינר בצד Railway (host suspend), לא באג. חתימה: `apscheduler "missed by 17m"` על כל הג'ובים יחד, בלי error/restart/OOM.
- **`SSL error: unexpected eof`** = דיפלוי/ריסטרט הפיל את ה-pool. לאמת מול `railway deployment list` לפני שמחפשים באג.
- **פאצ'י אבטחה של Railway נלחצים רק בדשבורד** — אין פקודת CLI; `redeploy` נותן downtime בלי הפאץ'.

## שפה מועדפת
עברית — כל התגובות והמסמכים בעברית.

## כללי עבודה
1. תמיד להיכנס ל-**Plan Mode** לפני שינויים
2. אחרי כל שינוי: `git status` → `git add` → `git commit` → `git push`
3. **אחרי כל deploy — לבדוק לוגים ב-Railway אוטומטית**, בלי לחכות לבקשה

## ⛔ אסור בלי אישור מפורש
- **CORS חייב לכלול תמיד את `www.amzfreeil.com`** — טופס צור קשר (`/api/contact`) נשבר בלעדיו.
- **הגדרות ה-Spam סגורות.** DKIM/SPF/DMARC `p=reject` אושרו ב-06/07/2026, mail-tester 10/10 — לא לגעת ולא לשאול שוב.
- **אין להסיר את ה-Playwright fallback מ-`checker.py`** — הוכרע סופית ב-14/07/26 שהוא נשאר, פעיל על `ERROR`/`UNKNOWN`.
- **אין להוסיף side effects, triggers או ריצות אוטומטיות שלא התבקשו** — תמיד לשאול קודם.
- **אין להריץ `FLUSHDB` ב-Upstash** — ה-DB משותף עם leptin-bot.
- אין לדחוף `.env` או מפתחות Resend/Anthropic/Decodo.
