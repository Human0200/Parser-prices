from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.alert import AlertLog, AlertRule, RuleType
from app.models.category import TrackedCategory
from app.models.product import TrackedProduct

logger = structlog.get_logger()

CATEGORY_RULE_TYPES = {
    RuleType.CATEGORY_MIN_DROP,
    RuleType.CATEGORY_PRICE_BELOW,
    RuleType.CATEGORY_NEW_SKU,
}


class AlertService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_alert_rule(
        self,
        user_id: int,
        rule_type: RuleType,
        product_id: int | None = None,
        category_id: int | None = None,
        threshold_value: Decimal | None = None,
    ) -> AlertRule:
        rule = AlertRule(
            user_id=user_id,
            product_id=product_id,
            category_id=category_id,
            rule_type=rule_type,
            threshold_value=threshold_value,
        )
        self.session.add(rule)
        await self.session.flush()

        logger.info(
            "alert_rule_created",
            rule_id=rule.id,
            user_id=user_id,
            rule_type=rule_type.value,
            product_id=product_id,
            category_id=category_id,
        )
        return rule

    async def get_user_alert_rules(
        self, user_id: int, active_only: bool = True
    ) -> list[AlertRule]:
        stmt = select(AlertRule).where(AlertRule.user_id == user_id)
        if active_only:
            stmt = stmt.where(AlertRule.is_active.is_(True))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_category_alert_rules(
        self, user_id: int, category_id: int
    ) -> list[AlertRule]:
        stmt = select(AlertRule).where(
            and_(
                AlertRule.user_id == user_id,
                AlertRule.is_active.is_(True),
                AlertRule.category_id == category_id,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    def check_category_alert(
        self,
        rule: AlertRule,
        *,
        previous_min: Decimal | None,
        current_min: Decimal | None,
        new_sku_ids: set[str] | list[str],
    ) -> bool:
        new_skus = set(new_sku_ids)
        match rule.rule_type:
            case RuleType.CATEGORY_MIN_DROP:
                if previous_min is None or current_min is None:
                    return False
                if current_min >= previous_min:
                    return False
                if previous_min <= 0:
                    return False
                drop_pct = (previous_min - current_min) / previous_min * 100
                threshold = rule.threshold_value or Decimal("0")
                return drop_pct >= threshold

            case RuleType.CATEGORY_PRICE_BELOW:
                if rule.threshold_value is None or current_min is None:
                    return False
                return current_min <= rule.threshold_value

            case RuleType.CATEGORY_NEW_SKU:
                return bool(new_skus)

        return False

    async def process_category_alerts(
        self,
        category: TrackedCategory,
        *,
        previous_min: Decimal | None,
        current_min: Decimal | None,
        new_sku_ids: set[str],
    ) -> list[AlertLog]:
        rules = await self.get_category_alert_rules(category.user_id, category.id)
        triggered_logs: list[AlertLog] = []

        for rule in rules:
            if rule.rule_type not in CATEGORY_RULE_TYPES:
                continue
            if not self.check_category_alert(
                rule,
                previous_min=previous_min,
                current_min=current_min,
                new_sku_ids=new_sku_ids,
            ):
                continue

            if rule.rule_type == RuleType.CATEGORY_PRICE_BELOW:
                if (
                    previous_min is not None
                    and rule.threshold_value is not None
                    and previous_min <= rule.threshold_value
                ):
                    continue

            log = AlertLog(
                alert_rule_id=rule.id,
                product_id=None,
                category_id=category.id,
                old_price=previous_min or Decimal("0"),
                new_price=current_min or Decimal("0"),
                message_sent=False,
            )
            # Attach in-memory so format_* does not trigger async lazy-load
            # (MissingGreenlet / greenlet_spawn under Celery).
            log.alert_rule = rule
            self.session.add(log)
            triggered_logs.append(log)
            logger.info(
                "category_alert_triggered",
                rule_id=rule.id,
                category_id=category.id,
                rule_type=rule.rule_type.value,
                new_skus=len(new_sku_ids),
            )

        if triggered_logs:
            await self.session.flush()
        return triggered_logs

    def format_category_alert_message(
        self,
        log: AlertLog,
        category: TrackedCategory,
        new_sku_count: int = 0,
    ) -> str:
        rule_type = log.alert_rule.rule_type if log.alert_rule else None
        marketplace_labels = {
            "wildberries": "Wildberries",
            "ozon": "Ozon",
            "yandex_market": "Яндекс Маркет",
        }
        mp_label = marketplace_labels.get(
            category.marketplace.value, category.marketplace.value
        )

        if rule_type == RuleType.CATEGORY_NEW_SKU:
            header = "🆕 Новые товары в выдаче!"
            detail = f"Появилось новых SKU: {new_sku_count}"
        elif rule_type == RuleType.CATEGORY_PRICE_BELOW:
            header = "🎯 Мин. цена ниже порога!"
            detail = f"Мин. цена: {log.new_price:,.0f} ₽"
        else:
            header = "📉 Падение минимальной цены!"
            if log.old_price > 0:
                change_pct = (log.new_price - log.old_price) / log.old_price * 100
                detail = (
                    f"Мин. цена: {log.new_price:,.0f} ₽ "
                    f"(было {log.old_price:,.0f} ₽, {change_pct:+.1f}%)"
                )
            else:
                detail = f"Мин. цена: {log.new_price:,.0f} ₽"

        lines = [
            header,
            "",
            f"📂 {category.title}",
            f"🏪 {mp_label}",
            f"📦 В выдаче: {category.item_count}",
            detail,
        ]
        if category.min_price_url:
            if category.min_price_title:
                lines.append(f"🏷 {category.min_price_title}")
            lines.append(f"🔗 {category.min_price_url}")
        lines.append(f"📂 Выдача: {category.url}")
        return "\n".join(lines)

    async def get_product_alert_rules(
        self, user_id: int, product_id: int
    ) -> list[AlertRule]:
        """Returns rules for this specific product plus global product rules."""
        stmt = (
            select(AlertRule)
            .where(
                and_(
                    AlertRule.user_id == user_id,
                    AlertRule.is_active.is_(True),
                    AlertRule.category_id.is_(None),
                    (
                        (AlertRule.product_id == product_id)
                        | (AlertRule.product_id.is_(None))
                    ),
                )
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def delete_alert_rule(self, rule_id: int, user_id: int) -> bool:
        rule = await self.session.get(AlertRule, rule_id)
        if rule is None or rule.user_id != user_id:
            return False
        await self.session.delete(rule)
        return True

    async def check_alert(
        self,
        rule: AlertRule,
        product: TrackedProduct,
    ) -> bool:
        if product.current_price is None or product.previous_price is None:
            if rule.rule_type == RuleType.BACK_IN_STOCK:
                return False  # handled via in_stock flag
            return False

        old_price = product.previous_price
        new_price = product.current_price

        if old_price == new_price:
            return False

        match rule.rule_type:
            case RuleType.PRICE_DROP:
                if old_price <= 0:
                    return False
                drop_pct = (old_price - new_price) / old_price * 100
                threshold = rule.threshold_value or Decimal("0")
                return drop_pct >= threshold

            case RuleType.PRICE_RISE:
                if old_price <= 0:
                    return False
                rise_pct = (new_price - old_price) / old_price * 100
                threshold = rule.threshold_value or Decimal("0")
                return rise_pct >= threshold

            case RuleType.PRICE_BELOW:
                if rule.threshold_value is None:
                    return False
                return new_price <= rule.threshold_value

            case RuleType.PRICE_ABOVE:
                if rule.threshold_value is None:
                    return False
                return new_price >= rule.threshold_value

            case RuleType.BACK_IN_STOCK:
                return False  # handled via stock status, not price

        return False

    async def process_product_alerts(
        self, product: TrackedProduct
    ) -> list[AlertLog]:
        rules = await self.get_product_alert_rules(product.user_id, product.id)
        triggered_logs: list[AlertLog] = []

        for rule in rules:
            if await self.check_alert(rule, product):
                log = AlertLog(
                    alert_rule_id=rule.id,
                    product_id=product.id,
                    old_price=product.previous_price or Decimal("0"),
                    new_price=product.current_price or Decimal("0"),
                    message_sent=False,
                )
                log.alert_rule = rule
                self.session.add(log)
                triggered_logs.append(log)

                logger.info(
                    "alert_triggered",
                    rule_id=rule.id,
                    product_id=product.id,
                    rule_type=rule.rule_type.value,
                    old_price=str(product.previous_price),
                    new_price=str(product.current_price),
                )

        if triggered_logs:
            await self.session.flush()

        return triggered_logs

    async def mark_alert_sent(self, alert_log: AlertLog) -> None:
        alert_log.message_sent = True
        alert_log.sent_at = datetime.now(timezone.utc)

    async def get_unsent_alerts(self) -> list[AlertLog]:
        stmt = (
            select(AlertLog)
            .where(AlertLog.message_sent.is_(False))
            .options(
                selectinload(AlertLog.alert_rule),
                selectinload(AlertLog.product),
                selectinload(AlertLog.category),
            )
            .order_by(AlertLog.created_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    def format_alert_message(
        self,
        log: AlertLog,
        product: TrackedProduct,
        min_price_30d: Decimal | None = None,
    ) -> str:
        old_price = log.old_price
        new_price = log.new_price

        if old_price > 0:
            change_pct = (new_price - old_price) / old_price * 100
            change_sign = "+" if change_pct > 0 else ""
            change_str = f"{change_sign}{change_pct:.1f}%"
        else:
            change_str = "N/A"

        emoji = "\U0001f514"  # bell
        if new_price < old_price:
            header = f"{emoji} \u0421\u043d\u0438\u0436\u0435\u043d\u0438\u0435 \u0446\u0435\u043d\u044b!"
        else:
            header = f"{emoji} \u0418\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0435 \u0446\u0435\u043d\u044b!"

        marketplace_labels = {
            "wildberries": "Wildberries",
            "ozon": "Ozon",
            "yandex_market": "\u042f\u043d\u0434\u0435\u043a\u0441 \u041c\u0430\u0440\u043a\u0435\u0442",
        }
        mp_label = marketplace_labels.get(product.marketplace.value, product.marketplace.value)

        lines = [
            header,
            "",
            f"\U0001f4e6 {product.title}",
            f"\U0001f3ea {mp_label}",
            f"\U0001f4b0 \u041d\u043e\u0432\u0430\u044f \u0446\u0435\u043d\u0430: {new_price:,.0f} \u20bd",
            f"\U0001f4c9 \u0411\u044b\u043b\u043e: {old_price:,.0f} \u20bd ({change_str})",
        ]

        if min_price_30d is not None:
            lines.append(
                f"\U0001f4ca \u041c\u0438\u043d. \u0437\u0430 30 \u0434\u043d\u0435\u0439: {min_price_30d:,.0f} \u20bd"
            )

        return "\n".join(lines)
