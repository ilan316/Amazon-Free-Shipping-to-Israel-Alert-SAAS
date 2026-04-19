from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from jose import JWTError, jwt
from sqlalchemy import select

from backend.auth import SECRET_KEY, ALGORITHM
from backend.database import AsyncSessionLocal
from backend.models import User

router = APIRouter()

_APP_URL = "https://app.amzfreeil.com"

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
    <h1>הפסקת לקבל עדכונים</h1>
    <p>לא נשלחו אליך עוד מיילים על מוצרים.<br>בנוסף, המוצרים שלך לא יבדקו עד שתחזור.</p>
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


@router.get("/pause", response_class=HTMLResponse, include_in_schema=False)
async def pause_notifications(token: str = Query(...)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "pause":
            raise ValueError("Invalid token type")
        user_id = int(payload["sub"])
    except (JWTError, ValueError, KeyError):
        return HTMLResponse(_ERROR_HTML, status_code=400)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))
        user = result.scalar_one_or_none()
        if not user:
            return HTMLResponse(_ERROR_HTML, status_code=404)
        user.vacation_mode = True
        await db.commit()

    return HTMLResponse(_SUCCESS_HTML)
