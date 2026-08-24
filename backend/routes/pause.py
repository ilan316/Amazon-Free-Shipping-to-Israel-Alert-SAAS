from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from jose import JWTError, jwt
from sqlalchemy import select, update

from backend.auth import SECRET_KEY, ALGORITHM
from backend.database import AsyncSessionLocal
from backend.models import User, UserProduct, Product

router = APIRouter()

_APP_URL = "https://app.amzfreeil.com"


def _explain_html(token: str) -> str:
    return f"""<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>הפסקת עדכונים</title>
  <style>
    body {{ margin:0; padding:0; background:#f3f3f3; font-family:Arial,'Segoe UI',sans-serif; }}
    .box {{ max-width:480px; margin:80px auto; background:#fff; border-radius:12px;
            padding:40px 32px; text-align:center; box-shadow:0 2px 12px rgba(0,0,0,.08); }}
    .icon {{ font-size:48px; margin-bottom:16px; }}
    h1 {{ color:#333; font-size:22px; margin:0 0 12px; }}
    p {{ color:#666; font-size:15px; line-height:1.6; margin:0 0 8px; }}
    .warning {{ background:#fff3cd; border:1px solid #ffc107; border-radius:8px;
                padding:14px 18px; font-size:13px; color:#856404; margin:20px 0 28px; text-align:right; }}
    .warning li {{ margin-bottom:6px; }}
    .btn-confirm {{ display:inline-block; background:#dc3545; color:#fff; font-weight:bold;
                   text-decoration:none; padding:12px 28px; border-radius:8px; font-size:15px; margin-left:12px; }}
    .btn-cancel  {{ display:inline-block; background:#FF9900; color:#111; font-weight:bold;
                   text-decoration:none; padding:12px 28px; border-radius:8px; font-size:15px; }}
  </style>
</head>
<body>
  <div class="box">
    <div class="icon">⚠️</div>
    <h1>רגע לפני שממשיכים</h1>
    <p>ביקשת להפסיק לקבל עדכונים ממערכת Amazon Free Shipping to Israel Alert.</p>
    <div class="warning">
      <strong>מה יקרה אם תאשר:</strong>
      <ul style="margin:8px 0 0; padding-right:18px;">
        <li>לא יישלחו אליך יותר סיכומים יומיים</li>
        <li>המוצרים שלך לא ייבדקו עד שתחזור</li>
        <li>תוכל לחזור בכל עת דרך הגדרות החשבון</li>
      </ul>
    </div>
    <a class="btn-confirm" href="{_APP_URL}/pause/confirm?token={token}">כן, הפסק עדכונים</a>
    <a class="btn-cancel"  href="{_APP_URL}/dashboard">חזרה, אני רוצה להמשיך לקבל</a>
  </div>
</body>
</html>"""


_SUCCESS_HTML = """<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>הפסקת קבלת עדכונים</title>
  <style>
    body {{ margin:0; padding:0; background:#f3f3f3; font-family:Arial,'Segoe UI',sans-serif; }}
    .box {{ max-width:480px; margin:80px auto; background:#fff; border-radius:12px;
            padding:40px 32px; text-align:center; box-shadow:0 2px 12px rgba(0,0,0,.08); }}
    .icon {{ font-size:48px; margin-bottom:16px; }}
    h1 {{ color:#333; font-size:22px; margin:0 0 12px; }}
    p {{ color:#666; font-size:15px; line-height:1.6; margin:0 0 24px; }}
    .note {{ background:#fff8e1; border-radius:8px; padding:14px 18px; font-size:13px;
             color:#7a5c00; margin-bottom:28px; border:1px solid #ffe082; }}
    a.btn {{ display:inline-block; background:#FF9900; color:#111; font-weight:bold;
             text-decoration:none; padding:12px 28px; border-radius:8px; font-size:15px; }}
  </style>
</head>
<body>
  <div class="box">
    <div class="icon">⏸️</div>
    <h1>לא תקבל עוד עדכונים</h1>
    <p>לא יישלחו אליך יותר סיכומים יומיים.<br>בנוסף, המוצרים שלך לא ייבדקו עד שתחזור.</p>
    <div class="note">
      כדי לחזור לקבל עדכונים — היכנס להגדרות וכבה את מצב החופשה.
    </div>
    <a class="btn" href="{app_url}/settings">הגדרות החשבון</a>
  </div>
</body>
</html>""".format(app_url=_APP_URL)

_ERROR_HTML = """<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>שגיאה</title>
  <style>
    body {{ margin:0; padding:0; background:#f3f3f3; font-family:Arial,sans-serif; }}
    .box {{ max-width:420px; margin:80px auto; background:#fff; border-radius:12px;
            padding:40px 32px; text-align:center; box-shadow:0 2px 12px rgba(0,0,0,.08); }}
    h1 {{ color:#dc3545; font-size:20px; }}
    p {{ color:#666; font-size:14px; }}
    a {{ color:#FF9900; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>הקישור לא תקין</h1>
    <p>הקישור פג תוקף או אינו תקין.<br>
       <a href="{app_url}/settings">היכנס להגדרות</a> כדי לנהל את ההתראות שלך.</p>
  </div>
</body>
</html>""".format(app_url=_APP_URL)


def _decode_pause_token(token: str) -> int:
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    if payload.get("type") != "pause":
        raise ValueError("Invalid token type")
    return int(payload["sub"])


def _decode_product_pause_token(token: str) -> tuple[int, str]:
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    if payload.get("type") != "pause_product":
        raise ValueError("Invalid token type")
    return int(payload["sub"]), payload["asin"]


@router.get("/pause", response_class=HTMLResponse, include_in_schema=False)
async def pause_explain(token: str = Query(...)):
    """Show explanation page — no action taken yet."""
    try:
        _decode_pause_token(token)
    except (JWTError, ValueError, KeyError):
        return HTMLResponse(_ERROR_HTML, status_code=400)
    return HTMLResponse(_explain_html(token))


_PRODUCT_PAUSED_HTML = """<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>מוצר הושהה</title>
  <style>
    body {{ margin:0; padding:0; background:#f3f3f3; font-family:Arial,'Segoe UI',sans-serif; }}
    .box {{ max-width:480px; margin:80px auto; background:#fff; border-radius:12px;
            padding:40px 32px; text-align:center; box-shadow:0 2px 12px rgba(0,0,0,.08); }}
    .icon {{ font-size:48px; margin-bottom:16px; }}
    h1 {{ color:#333; font-size:22px; margin:0 0 12px; }}
    p {{ color:#666; font-size:15px; line-height:1.6; margin:0 0 24px; }}
    a.btn {{ display:inline-block; background:#FF9900; color:#111; font-weight:bold;
             text-decoration:none; padding:12px 28px; border-radius:8px; font-size:15px; }}
  </style>
</head>
<body>
  <div class="box">
    <div class="icon">⏸️</div>
    <h1>הבדיקות למוצר הושהו</h1>
    <p>לא נבדוק יותר מוצר זה עד שתפעיל אותו מחדש דרך הדשבורד.</p>
    <a class="btn" href="{app_url}/dashboard">לניהול המוצרים שלי</a>
  </div>
</body>
</html>""".format(app_url=_APP_URL)


@router.get("/pause-product", response_class=HTMLResponse, include_in_schema=False)
async def pause_product(token: str = Query(...)):
    """Pause a single product from a no-click reminder email."""
    try:
        user_id, asin = _decode_product_pause_token(token)
    except (JWTError, ValueError, KeyError):
        return HTMLResponse(_ERROR_HTML, status_code=400)

    async with AsyncSessionLocal() as db:
        product = (await db.execute(select(Product).where(Product.asin == asin))).scalar_one_or_none()
        if not product:
            return HTMLResponse(_ERROR_HTML, status_code=404)
        up = (await db.execute(
            select(UserProduct).where(UserProduct.user_id == user_id, UserProduct.product_id == product.id)
        )).scalar_one_or_none()
        if not up:
            return HTMLResponse(_ERROR_HTML, status_code=404)
        up.is_paused = True
        up.paused_reason = "manual"
        await db.commit()

    return HTMLResponse(_PRODUCT_PAUSED_HTML)


@router.get("/pause/confirm", response_class=HTMLResponse, include_in_schema=False)
async def pause_confirm(token: str = Query(...)):
    """User confirmed — activate vacation mode."""
    try:
        user_id = _decode_pause_token(token)
    except (JWTError, ValueError, KeyError):
        return HTMLResponse(_ERROR_HTML, status_code=400)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))
        user = result.scalar_one_or_none()
        if not user:
            return HTMLResponse(_ERROR_HTML, status_code=404)
        from backend.vacation import enter_vacation
        await enter_vacation(db, user, auto=False)
        await db.commit()

    return HTMLResponse(_SUCCESS_HTML)
