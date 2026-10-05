from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.models.alert import RuleType
from app.models.user import User
from app.parsers import detect_url_kind
from app.parsers.base import ParsedListing, ParsedListingItem, compute_listing_stats
from app.parsers.ozon import OzonParser
from app.parsers.wildberries import WildberriesParser
from app.parsers.yandex_market import YandexMarketParser
from app.services.alert_service import AlertService
from app.services.category_service import CategoryService


class TestUrlKindDetection:
    def test_ozon_category_is_listing(self):
        url = (
            "https://www.ozon.ru/category/videokarty-15721/"
            "?gpuseries=101874698&opened=gpuseries"
        )
        assert detect_url_kind(url) == "listing"
        assert OzonParser().is_listing_url(url) is True

    def test_ozon_product_is_product(self):
        url = "https://www.ozon.ru/product/videokarta-123456789/"
        assert detect_url_kind(url) == "product"

    def test_wb_search_is_listing(self):
        url = "https://www.wildberries.ru/catalog/0/search.aspx?search=rtx%204070"
        assert detect_url_kind(url) == "listing"
        assert WildberriesParser().is_listing_url(url) is True

    def test_wb_product_is_product(self):
        url = "https://www.wildberries.ru/catalog/12345678/detail.aspx"
        assert detect_url_kind(url) == "product"
        assert WildberriesParser().is_listing_url(url) is False

    def test_yandex_catalog_is_listing(self):
        url = "https://market.yandex.ru/catalog--videokarty/54425/list"
        assert detect_url_kind(url) == "listing"
        assert YandexMarketParser().is_listing_url(url) is True

    def test_yandex_search_query_is_listing(self):
        url = (
            "https://market.yandex.ru/search?text=%D0%B2%D0%B8%D0%B4%D0%B5%D0%BE%D0%BA%D0%B0%D1%80%D1%82%D0%B0"
            "&hid=91031&glfilter=36036031%3A70004659"
        )
        assert detect_url_kind(url) == "listing"
        assert YandexMarketParser().is_listing_url(url) is True

    def test_yandex_category_slug_is_listing(self):
        url = (
            "https://market.yandex.ru/category/materinskiye-platy"
            "?hid=91020&hid=91077&glfilter=45129251%3A50198759"
        )
        assert detect_url_kind(url) == "listing"
        assert YandexMarketParser().is_listing_url(url) is True

    def test_yandex_product_is_product(self):
        url = "https://market.yandex.ru/product--videokarta/123456"
        assert detect_url_kind(url) == "product"

    def test_unknown_url(self):
        assert detect_url_kind("https://example.com/foo") == "unknown"


class TestListingStats:
    def test_min_and_median(self):
        items = [
            ParsedListingItem("1", "a", Decimal("100"), "u"),
            ParsedListingItem("2", "b", Decimal("200"), "u"),
            ParsedListingItem("3", "c", Decimal("300"), "u"),
        ]
        mn, md = compute_listing_stats(items)
        assert mn == Decimal("100")
        assert md == Decimal("200")

    def test_empty(self):
        assert compute_listing_stats([]) == (None, None)


class TestYandexListingExtract:
    def test_search_schema_org_items(self):
        from app.parsers.yandex_market import _items_from_yandex_html

        html = '''
        {"items":[{"sku":"111","image":"https://img","name":"GPU A","price":50000,
        "priceCurrency":"RUB","url":"https://market.yandex.ru/card/gpu-a/111"},
        {"sku":"222","image":"https://img","name":"GPU B","price":40000,
        "priceCurrency":"RUB","url":"https://market.yandex.ru/card/gpu-b/222"}]}
        '''
        items = _items_from_yandex_html(html)
        assert len(items) == 2
        assert min(i.price for i in items) == Decimal("40000")


class TestSnapshotDiff:
    def test_new_skus_and_min_drop(self):
        diff = CategoryService.diff_snapshots(
            previous_sku_ids=["1", "2"],
            current_sku_ids=["2", "3", "4"],
            previous_min=Decimal("1000"),
            current_min=Decimal("800"),
        )
        assert diff["new_skus"] == ["3", "4"]
        assert diff["removed_skus"] == ["1"]
        assert diff["min_dropped"] is True

    def test_no_min_drop(self):
        diff = CategoryService.diff_snapshots(
            previous_sku_ids=["1"],
            current_sku_ids=["1"],
            previous_min=Decimal("100"),
            current_min=Decimal("120"),
        )
        assert diff["min_dropped"] is False
        assert diff["new_skus"] == []


@pytest.mark.asyncio
async def test_category_add_no_limit(db_session, test_user: User):
    listing = ParsedListing(
        title="Test Category",
        items=[
            ParsedListingItem("111", "A", Decimal("1000"), "https://ozon.ru/product/111/"),
            ParsedListingItem("222", "B", Decimal("900"), "https://ozon.ru/product/222/"),
        ],
        min_price=Decimal("900"),
        median_price=Decimal("950"),
    )

    svc = CategoryService(db_session)
    url = "https://www.ozon.ru/category/videokarty-15721/?gpuseries=101874698"

    with patch.object(OzonParser, "parse_listing", new=AsyncMock(return_value=listing)):
        cat = await svc.add_category(test_user, url)
        await db_session.flush()
        assert cat.min_price == Decimal("900")
        assert cat.min_price_url == "https://ozon.ru/product/222/"
        assert cat.min_price_title == "B"
        assert cat.item_count == 2
        assert await svc.can_add_category(test_user) is True

        cat2 = await svc.add_category(
            test_user,
            "https://www.ozon.ru/category/other-1/?x=1",
        )
        await db_session.flush()
        assert cat2.id != cat.id


@pytest.mark.asyncio
async def test_category_alerts(db_session, test_user: User):
    listing = ParsedListing(
        title="GPUs",
        items=[ParsedListingItem("1", "A", Decimal("50000"), "u1")],
        min_price=Decimal("50000"),
        median_price=Decimal("50000"),
    )
    svc = CategoryService(db_session)
    url = "https://www.ozon.ru/category/videokarty-15721/"

    with patch.object(OzonParser, "parse_listing", new=AsyncMock(return_value=listing)):
        category = await svc.add_category(test_user, url)
        await db_session.flush()

    alert_svc = AlertService(db_session)
    await alert_svc.create_alert_rule(
        user_id=test_user.id,
        rule_type=RuleType.CATEGORY_MIN_DROP,
        category_id=category.id,
        threshold_value=Decimal("0"),
    )
    await alert_svc.create_alert_rule(
        user_id=test_user.id,
        rule_type=RuleType.CATEGORY_NEW_SKU,
        category_id=category.id,
    )
    await alert_svc.create_alert_rule(
        user_id=test_user.id,
        rule_type=RuleType.CATEGORY_PRICE_BELOW,
        category_id=category.id,
        threshold_value=Decimal("40000"),
    )
    await db_session.flush()

    listing2 = ParsedListing(
        title="GPUs",
        items=[
            ParsedListingItem("1", "A", Decimal("50000"), "u1"),
            ParsedListingItem("9", "New", Decimal("35000"), "u9"),
        ],
        min_price=Decimal("35000"),
        median_price=Decimal("42500"),
    )
    with patch.object(OzonParser, "parse_listing", new=AsyncMock(return_value=listing2)):
        category, previous, appeared = await svc.refresh_category(category)
        await db_session.flush()

    assert "9" in appeared
    logs = await alert_svc.process_category_alerts(
        category,
        previous_min=previous.min_price if previous else None,
        current_min=category.min_price,
        new_sku_ids=appeared,
    )
    types = {log.alert_rule_id for log in logs}
    assert len(logs) == 3
    assert types  # all three rules fired
