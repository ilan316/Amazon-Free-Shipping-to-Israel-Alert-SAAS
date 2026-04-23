import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

_url = os.environ.get("DATABASE_URL", "")
if not _url:
    raise RuntimeError("DATABASE_URL environment variable is not set")

# Railway provides postgresql:// — SQLAlchemy async requires postgresql+asyncpg://
if _url.startswith("postgresql://"):
    _url = _url.replace("postgresql://", "postgresql+asyncpg://", 1)

DATABASE_URL = _url

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migration: add is_admin column if it doesn't exist yet
        await conn.execute(
            __import__("sqlalchemy").text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE"
            )
        )
        await conn.execute(
            __import__("sqlalchemy").text(
                "ALTER TABLE user_products ADD COLUMN IF NOT EXISTS is_paused BOOLEAN DEFAULT FALSE"
            )
        )
        await conn.execute(
            __import__("sqlalchemy").text(
                "CREATE TABLE IF NOT EXISTS system_settings (key VARCHAR(100) PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
            )
        )
        await conn.execute(
            __import__("sqlalchemy").text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS vacation_mode BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        await conn.execute(
            __import__("sqlalchemy").text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS max_products INTEGER"
            )
        )
        await conn.execute(
            __import__("sqlalchemy").text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )
        await conn.execute(
            __import__("sqlalchemy").text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP WITH TIME ZONE"
            )
        )
        await conn.execute(
            __import__("sqlalchemy").text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR(100)"
            )
        )


async def seed_default_templates():
    import os
    from sqlalchemy import select, text
    from backend.models import EmailTemplate

    base_url = os.environ.get("APP_BASE_URL", "https://app.amzfreeil.com").rstrip("/")
    dashboard_url = f"{base_url}/dashboard"

    _BRAND = "#FF9900"
    _BRAND_DARK = "#c97800"
    _BG = "#fffaf1"
    _TEXT = "#222222"

    def _wrap(content: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {{ margin:0; padding:0; background:#f5f5f5; font-family: 'Segoe UI', Arial, sans-serif; }}
    .container {{ max-width:520px; margin:32px auto; background:#ffffff; border-radius:14px; overflow:hidden; box-shadow:0 2px 12px rgba(0,0,0,0.08); }}
    .header {{ background:#fef5e4; padding:28px 32px 20px; text-align:center; }}
    .header img {{ height:80px; }}
    .body {{ padding:32px; color:{_TEXT}; line-height:1.7; font-size:15px; }}
    .body h2 {{ margin:0 0 16px; font-size:20px; color:{_TEXT}; }}
    .cta {{ display:block; width:fit-content; margin:24px auto; background:{_BRAND}; color:#111 !important;
            text-decoration:none; padding:14px 36px; border-radius:10px;
            font-weight:700; font-size:16px; text-align:center; }}
    .cta:hover {{ background:{_BRAND_DARK}; }}
    .footer {{ padding:16px 32px 24px; text-align:center; font-size:12px; color:#999; border-top:1px solid #f0f0f0; }}
    .step {{ display:flex; gap:12px; align-items:flex-start; margin:12px 0; }}
    .step-num {{ background:{_BRAND}; color:#111; font-weight:700; border-radius:50%;
                 width:26px; height:26px; display:flex; align-items:center; justify-content:center;
                 flex-shrink:0; font-size:13px; }}
    @media (max-width:560px) {{ .body {{ padding:20px; }} }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <img src="https://app.amzfreeil.com/static/logo-new.png" alt="AMZFREEIL">
    </div>
    {content}
    <div class="footer">
      Amazon Free Shipping to Israel Alert<br>
      <a href="{{{{pause_url}}}}" style="color:#aaa;">הפסק לקבל עדכונים</a>
    </div>
  </div>
</body>
</html>"""

    template1_body = _wrap(f"""
    <div class="body">
      <h2>היי, עדיין לא הוספת מוצר 👋</h2>
      <p>נרשמת ל-<strong>AMZFREEIL</strong> — השירות שמתריע כשמוצרי אמזון מציעים <strong>משלוח חינם לישראל</strong>.</p>
      <p style="margin-bottom:6px;"><strong>איך זה עובד בשלושה שלבים:</strong></p>
      <div class="step"><div class="step-num">1</div><div>היכנס לאמזון, מצא מוצר שמעניין אותך</div></div>
      <div class="step"><div class="step-num">2</div><div>העתק את הקישור ● הדבק בדשבורד שלך<br><span style="font-size:13px;color:#888;">💡 מהיר יותר? <a href="https://chromewebstore.google.com/detail/amazon-israel-free-ship-a/mbickhgdhofaefhibfbgpacejhbelddn" style="color:{_BRAND};font-weight:600;">התקן את תוסף הכרום</a> — מוסיף מוצרים בלחיצה אחת ישירות מאמזון</span></div></div>
      <div class="step"><div class="step-num">3</div><div>ברגע שהמשלוח הופך לחינם — נשלח לך מייל 🎉</div></div>
      <a href="{dashboard_url}" class="cta">← הוסף את המוצר הראשון שלך<br><span style="font-size:13px;font-weight:400;opacity:0.8;">לוקח פחות מ-30 שניות</span></a>
    </div>""")

    template2_body = _wrap(f"""
    <div class="body">
      <h2>יש לך עוד מקומות פנויים 🛒</h2>
      <p>עקבת אחרי {{{{product_count}}}} מוצרים — אבל יש לך מקום ליותר.</p>
      <p>כל מוצר נוסף שתוסיף = עוד הזדמנות לקבל <strong>משלוח חינם לישראל</strong>.</p>
      <p style="background:{_BG};border-radius:10px;padding:14px 18px;font-size:14px;border-right:4px solid {_BRAND};">
        💡 <strong>טיפ:</strong> מוצרים שעוקבים אחריהם הרבה אנשים — בדרך כלל מקבלים הנחות ומשלוחים חינם יותר בתדירות גבוהה.
      </p>
      <a href="{dashboard_url}" class="cta">← הוסף עוד מוצרים</a>
    </div>""")

    async with AsyncSessionLocal() as session:
        defaults = [
            EmailTemplate(
                name="הפעלה_אפס_מוצרים",
                subject="הוספת מוצר ראשון ב-30 שניות ✨",
                body=template1_body,
            ),
            EmailTemplate(
                name="הוסף_עוד_מוצרים",
                subject="יש לך עוד מקומות פנויים — הגדל את הסיכויים שלך 🛒",
                body=template2_body,
            ),
        ]
        for t in defaults:
            existing = (await session.execute(
                select(EmailTemplate).where(EmailTemplate.name == t.name)
            )).scalar_one_or_none()
            if existing:
                existing.subject = t.subject
                existing.body = t.body
            else:
                session.add(t)
        await session.commit()
