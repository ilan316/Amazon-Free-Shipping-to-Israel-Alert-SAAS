"""
Vacation mode helpers.

Vacation has two flavours and they must not be confused:

* **auto** — the inactivity scheduler parked a user who went quiet. Any sign of
  life (dashboard login, click on a tracked email link) resumes them.
* **manual** — the user asked for it, from the dashboard toggle or the pause link
  in an email. Only the user turns that off again.

Products paused *because of* vacation are tagged ``paused_reason="vacation"`` so
that resuming never un-pauses a product the user paused by hand.
"""
import logging

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import User, UserProduct

logger = logging.getLogger(__name__)

PAUSE_REASON = "vacation"


async def enter_vacation(db: AsyncSession, user: User, *, auto: bool) -> None:
    """Park a user. Products already paused keep their own reason and stay paused."""
    user.vacation_mode = True
    user.vacation_auto = auto
    await db.execute(
        update(UserProduct)
        .where(UserProduct.user_id == user.id, UserProduct.is_paused == False)
        .values(is_paused=True, paused_reason=PAUSE_REASON)
    )


async def exit_vacation(db: AsyncSession, user: User) -> None:
    """Wake a user up and release only the products vacation itself paused."""
    user.vacation_mode = False
    user.vacation_auto = False
    await db.execute(
        update(UserProduct)
        .where(UserProduct.user_id == user.id, UserProduct.paused_reason == PAUSE_REASON)
        .values(is_paused=False, paused_reason=None)
    )


async def resume_if_auto(db: AsyncSession, user: User) -> bool:
    """
    Called on any sign of life. Resumes only an auto-vacation; a vacation the user
    asked for is left alone. Returns True if the user was woken up.

    The caller owns the commit.
    """
    if not (user.vacation_mode and user.vacation_auto):
        return False
    await exit_vacation(db, user)
    logger.info(f"[vacation] User {user.id} auto-resumed — activity detected")
    return True
