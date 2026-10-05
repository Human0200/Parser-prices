from app.parsers.base import (
    BaseParser,
    ParsedListing,
    ParsedListingItem,
    ParsedProduct,
    listing_external_key,
    normalize_listing_url,
)
from app.parsers.ozon import OzonParser
from app.parsers.wildberries import WildberriesParser
from app.parsers.yandex_market import YandexMarketParser

PARSERS = {
    "wildberries": WildberriesParser,
    "ozon": OzonParser,
    "yandex_market": YandexMarketParser,
}


def detect_url_kind(url: str) -> str:
    return BaseParser.detect_url_kind(url)


__all__ = [
    "BaseParser",
    "ParsedProduct",
    "ParsedListing",
    "ParsedListingItem",
    "WildberriesParser",
    "OzonParser",
    "YandexMarketParser",
    "PARSERS",
    "detect_url_kind",
    "listing_external_key",
    "normalize_listing_url",
]
