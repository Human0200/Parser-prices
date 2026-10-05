from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import CategorySnapshot, TrackedCategory
from app.models.product import Marketplace
from app.models.user import User
from app.parsers import PARSERS, detect_url_kind, listing_external_key
from app.parsers.base import BaseParser, ParsedListing, pick_min_price_item

logger = structlog.get_logger()


class CategoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_user_categories(
        self, user_id: int, active_only: bool = True
    ) -> list[TrackedCategory]:
        stmt = select(TrackedCategory).where(TrackedCategory.user_id == user_id)
        if active_only:
            stmt = stmt.where(TrackedCategory.is_active.is_(True))
        stmt = stmt.order_by(TrackedCategory.created_at.desc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_category_by_id(self, category_id: int) -> TrackedCategory | None:
        return await self.session.get(TrackedCategory, category_id)

    async def get_user_category_count(self, user_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(TrackedCategory)
            .where(
                and_(
                    TrackedCategory.user_id == user_id,
                    TrackedCategory.is_active.is_(True),
                )
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def can_add_category(self, user: User) -> bool:
        return True

    async def add_category(self, user: User, url: str) -> TrackedCategory:
        if detect_url_kind(url) != "listing":
            raise ValueError("URL is not a category/search listing")

        marketplace = BaseParser.detect_marketplace(url)
        if marketplace is None:
            raise ValueError("Unsupported marketplace URL")

        parser_cls = PARSERS.get(marketplace)
        if parser_cls is None:
            raise ValueError(f"No parser available for {marketplace}")

        external_key = listing_external_key(url)
        existing = await self.session.execute(
            select(TrackedCategory).where(
                and_(
                    TrackedCategory.user_id == user.id,
                    TrackedCategory.marketplace == Marketplace(marketplace),
                    TrackedCategory.external_key == external_key,
                )
            )
        )
        found = existing.scalar_one_or_none()
        if found:
            if not found.is_active:
                found.is_active = True
                await self.refresh_category(found)
                return found
            raise ValueError("Category is already being tracked")

        parser = parser_cls()
        listing = await parser.parse_listing(url)
        cheapest = pick_min_price_item(listing.items)

        category = TrackedCategory(
            user_id=user.id,
            marketplace=Marketplace(marketplace),
            url=url.strip(),
            title=listing.title[:500],
            external_key=external_key,
            min_price=listing.min_price,
            previous_min_price=None,
            min_price_url=cheapest.url if cheapest else None,
            min_price_title=(cheapest.title[:500] if cheapest else None),
            item_count=len(listing.items),
            last_parsed_at=datetime.now(timezone.utc),
        )
        self.session.add(category)
        await self.session.flush()

        self._add_snapshot(category, listing)
        await self.session.flush()

        logger.info(
            "category_added",
            user_id=user.id,
            marketplace=marketplace,
            category_id=category.id,
            item_count=category.item_count,
            min_price=str(category.min_price) if category.min_price else None,
        )
        return category

    async def remove_category(self, category_id: int, user_id: int) -> bool:
        category = await self.get_category_by_id(category_id)
        if category is None or category.user_id != user_id:
            return False
        category.is_active = False
        return True

    async def get_latest_snapshot(
        self, category_id: int
    ) -> CategorySnapshot | None:
        stmt = (
            select(CategorySnapshot)
            .where(CategorySnapshot.category_id == category_id)
            .order_by(CategorySnapshot.recorded_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def refresh_category(
        self, category: TrackedCategory
    ) -> tuple[TrackedCategory, CategorySnapshot | None, set[str]]:
        """Re-parse listing, store snapshot, update min_price.

        Returns (category, previous_snapshot, new_sku_ids).
        """
        parser_cls = PARSERS.get(category.marketplace.value)
        if parser_cls is None:
            raise ValueError(f"No parser for {category.marketplace.value}")

        previous = await self.get_latest_snapshot(category.id)
        previous_skus = set(previous.sku_ids or []) if previous else set()

        try:
            listing = await parser_cls().parse_listing(category.url)
        except Exception:
            category.parse_errors_count += 1
            raise

        new_skus = {item.external_id for item in listing.items}
        appeared = new_skus - previous_skus
        cheapest = pick_min_price_item(listing.items)

        category.previous_min_price = category.min_price
        category.min_price = listing.min_price
        category.min_price_url = cheapest.url if cheapest else None
        category.min_price_title = cheapest.title[:500] if cheapest else None
        category.item_count = len(listing.items)
        category.title = listing.title[:500] or category.title
        category.parse_errors_count = 0
        category.last_parsed_at = datetime.now(timezone.utc)

        snapshot = self._add_snapshot(category, listing)
        await self.session.flush()
        return category, previous, appeared

    def _add_snapshot(
        self, category: TrackedCategory, listing: ParsedListing
    ) -> CategorySnapshot:
        snapshot = CategorySnapshot(
            category_id=category.id,
            sku_ids=[item.external_id for item in listing.items],
            min_price=listing.min_price,
            median_price=listing.median_price,
            item_count=len(listing.items),
        )
        self.session.add(snapshot)
        return snapshot

    @staticmethod
    def diff_snapshots(
        previous_sku_ids: list[str] | None,
        current_sku_ids: list[str],
        previous_min: Decimal | None,
        current_min: Decimal | None,
    ) -> dict:
        prev = set(previous_sku_ids or [])
        curr = set(current_sku_ids or [])
        return {
            "new_skus": sorted(curr - prev),
            "removed_skus": sorted(prev - curr),
            "min_dropped": (
                previous_min is not None
                and current_min is not None
                and current_min < previous_min
            ),
            "previous_min": previous_min,
            "current_min": current_min,
        }
