from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import UNLIMITED, SubscriptionPlan, User

logger = structlog.get_logger()

PLAN_PRICES = {
    SubscriptionPlan.BASIC: 990,
    SubscriptionPlan.PRO: 2490,
}

PLAN_DURATION_DAYS = 30


class SubscriptionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_user(
        self,
        telegram_id: int,
        first_name: str,
        username: str | None = None,
    ) -> User:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await self.session.execute(stmt)
        user = result.scalar_one_or_none()

        if user is not None:
            user.first_name = first_name
            if username:
                user.telegram_username = username
            return user

        user = User(
            telegram_id=telegram_id,
            first_name=first_name,
            telegram_username=username,
            subscription_plan=SubscriptionPlan.FREE,
            max_tracked_products=UNLIMITED,
        )
        self.session.add(user)
        await self.session.flush()

        logger.info("user_created", telegram_id=telegram_id, first_name=first_name)
        return user

    async def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def activate_subscription(
        self, user: User, plan: SubscriptionPlan
    ) -> None:
        """Legacy: plans are not enforced; kept for DB compatibility."""
        now = datetime.now(timezone.utc)

        if (
            user.subscription_expires_at
            and user.subscription_expires_at > now
            and user.subscription_plan == plan
        ):
            user.subscription_expires_at += timedelta(days=PLAN_DURATION_DAYS)
        else:
            user.subscription_expires_at = now + timedelta(days=PLAN_DURATION_DAYS)

        user.subscription_plan = plan
        user.max_tracked_products = UNLIMITED

        logger.info(
            "subscription_activated",
            user_id=user.id,
            plan=plan.value,
            expires_at=user.subscription_expires_at.isoformat(),
        )

    async def check_and_downgrade_expired(self, user: User) -> bool:
        """No-op: plan limits are disabled."""
        return False

    def get_plan_info(self, plan: SubscriptionPlan) -> dict:
        return {
            "name": plan.value,
            "price": PLAN_PRICES.get(plan, 0),
            "duration_days": PLAN_DURATION_DAYS,
            "max_products": UNLIMITED,
            "max_categories": UNLIMITED,
            "max_alerts": UNLIMITED,
            "max_marketplaces": 3,
            "history_days": 90,
            "csv_export": True,
        }

    def format_subscription_info(self, user: User) -> str:
        return (
            "Лимиты тарифов отключены.\n"
            "Можно добавлять товары и категории без ограничений."
        )
