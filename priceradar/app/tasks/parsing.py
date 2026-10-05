import random
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, and_, update

from app.config import settings
from app.database import async_session_factory
from app.models.product import Marketplace, PriceHistory, TrackedProduct
from app.parsers import PARSERS
from app.parsers.utils import ParserError
from app.services.price_service import PriceService
from app.tasks.async_utils import run_async
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


async def _parse_single_product(product_id: int) -> dict:
    async with async_session_factory() as session:
        product = await session.get(TrackedProduct, product_id)
        if product is None or not product.is_active:
            return {"product_id": product_id, "status": "skipped"}

        parser_cls = PARSERS.get(product.marketplace.value)
        if parser_cls is None:
            return {"product_id": product_id, "status": "no_parser"}

        parser = parser_cls()
        svc = PriceService(session)

        try:
            parsed = await parser.parse_product(product.external_id)
            price_changed = await svc.update_product_price(product, parsed)
            await session.commit()

            return {
                "product_id": product_id,
                "status": "ok",
                "price_changed": price_changed,
                "price": str(parsed.price),
            }

        except ParserError as e:
            await svc.increment_parse_error(product)
            await session.commit()

            logger.warning(
                "parse_error",
                product_id=product_id,
                marketplace=product.marketplace.value,
                error=str(e),
                error_count=product.parse_errors_count,
            )
            return {
                "product_id": product_id,
                "status": "error",
                "error": str(e),
            }

        except Exception as e:
            await svc.increment_parse_error(product)
            await session.commit()

            logger.error(
                "parse_unexpected_error",
                product_id=product_id,
                error=str(e),
            )
            return {
                "product_id": product_id,
                "status": "error",
                "error": str(e),
            }


@celery_app.task(name="app.tasks.parsing.parse_single_product")
def parse_single_product(product_id: int) -> dict:
    return run_async(_parse_single_product(product_id))


async def _get_products_to_parse() -> list[dict]:
    async with async_session_factory() as session:
        now = datetime.now(timezone.utc)
        interval = timedelta(seconds=settings.PARSE_INTERVAL)

        stmt = select(TrackedProduct).where(TrackedProduct.is_active.is_(True))
        result = await session.execute(stmt)
        products = result.scalars().all()

        products_to_parse = []
        for product in products:
            if product.price_updated_at and (now - product.price_updated_at) < interval:
                continue

            products_to_parse.append({
                "id": product.id,
                "marketplace": product.marketplace.value,
            })

        return products_to_parse


@celery_app.task(name="app.tasks.parsing.parse_all_products")
def parse_all_products() -> dict:
    products = run_async(_get_products_to_parse())

    by_marketplace: dict[str, list[int]] = {}
    for p in products:
        by_marketplace.setdefault(p["marketplace"], []).append(p["id"])

    total_scheduled = 0
    for marketplace, product_ids in by_marketplace.items():
        random.shuffle(product_ids)
        for i, pid in enumerate(product_ids):
            delay = i * random.uniform(3, 5)
            parse_single_product.apply_async(
                args=[pid],
                countdown=delay,
            )
            total_scheduled += 1

    logger.info(
        "parse_all_scheduled",
        total=total_scheduled,
        marketplaces=list(by_marketplace.keys()),
    )

    return {
        "scheduled": total_scheduled,
        "by_marketplace": {k: len(v) for k, v in by_marketplace.items()},
    }


async def _cleanup_old_prices() -> int:
    async with async_session_factory() as session:
        svc = PriceService(session)
        count = await svc.cleanup_old_prices(days=settings.PRICE_HISTORY_RETENTION_DAYS_PRO)
        await session.commit()
        return count


@celery_app.task(name="app.tasks.parsing.cleanup_old_prices")
def cleanup_old_prices() -> dict:
    deleted = run_async(_cleanup_old_prices())
    return {"deleted": deleted}


async def _deactivate_broken() -> int:
    async with async_session_factory() as session:
        stmt = (
            update(TrackedProduct)
            .where(
                and_(
                    TrackedProduct.is_active.is_(True),
                    TrackedProduct.parse_errors_count >= settings.MAX_PARSE_ERRORS,
                )
            )
            .values(is_active=False)
            .returning(TrackedProduct.id)
        )
        result = await session.execute(stmt)
        deactivated_ids = list(result.scalars().all())
        await session.commit()

        if deactivated_ids:
            logger.warning(
                "products_deactivated_broken",
                count=len(deactivated_ids),
                product_ids=deactivated_ids,
            )

        return len(deactivated_ids)


@celery_app.task(name="app.tasks.parsing.deactivate_broken")
def deactivate_broken() -> dict:
    count = run_async(_deactivate_broken())
    return {"deactivated": count}


async def _parse_single_category(category_id: int) -> dict:
    from app.models.category import TrackedCategory
    from app.models.user import User
    from app.services.alert_service import AlertService
    from app.services.category_service import CategoryService
    from app.tasks.alerts import _send_telegram_alert

    async with async_session_factory() as session:
        category = await session.get(TrackedCategory, category_id)
        if category is None or not category.is_active:
            return {"category_id": category_id, "status": "skipped"}

        cat_svc = CategoryService(session)
        alert_svc = AlertService(session)

        try:
            category, previous, appeared = await cat_svc.refresh_category(category)
            previous_min = (
                previous.min_price if previous is not None else category.previous_min_price
            )
            # Prefer previous snapshot min; fall back to category.previous_min_price
            if previous is not None:
                previous_min = previous.min_price

            logs = await alert_svc.process_category_alerts(
                category,
                previous_min=previous_min,
                current_min=category.min_price,
                new_sku_ids=appeared,
            )

            user = await session.get(User, category.user_id)
            sent = 0
            if user is not None:
                for log in logs:
                    message = alert_svc.format_category_alert_message(
                        log, category, new_sku_count=len(appeared)
                    )
                    ok = await _send_telegram_alert(
                        user.telegram_id, message, category.url
                    )
                    if ok:
                        await alert_svc.mark_alert_sent(log)
                        sent += 1

            await session.commit()
            return {
                "category_id": category_id,
                "status": "ok",
                "triggered": len(logs),
                "sent": sent,
                "new_skus": len(appeared),
                "min_price": str(category.min_price) if category.min_price else None,
            }
        except ParserError as e:
            category.parse_errors_count += 1
            await session.commit()
            logger.warning(
                "category_parse_error",
                category_id=category_id,
                error=str(e),
            )
            return {"category_id": category_id, "status": "error", "error": str(e)}
        except Exception as e:
            category.parse_errors_count += 1
            await session.commit()
            logger.error(
                "category_parse_unexpected_error",
                category_id=category_id,
                error=str(e),
            )
            return {"category_id": category_id, "status": "error", "error": str(e)}


@celery_app.task(name="app.tasks.parsing.parse_single_category")
def parse_single_category(category_id: int) -> dict:
    return run_async(_parse_single_category(category_id))


async def _get_categories_to_parse() -> list[int]:
    from app.models.category import TrackedCategory

    async with async_session_factory() as session:
        now = datetime.now(timezone.utc)
        interval = timedelta(seconds=settings.PARSE_INTERVAL)

        stmt = select(TrackedCategory).where(TrackedCategory.is_active.is_(True))
        result = await session.execute(stmt)
        category_ids: list[int] = []
        for category in result.scalars().all():
            if category.last_parsed_at and (now - category.last_parsed_at) < interval:
                continue
            category_ids.append(category.id)
        return category_ids


@celery_app.task(name="app.tasks.parsing.parse_all_categories")
def parse_all_categories() -> dict:
    category_ids = run_async(_get_categories_to_parse())
    for i, cid in enumerate(category_ids):
        parse_single_category.apply_async(
            args=[cid],
            countdown=i * random.uniform(5, 8),
        )
    logger.info("parse_all_categories_scheduled", total=len(category_ids))
    return {"scheduled": len(category_ids)}
