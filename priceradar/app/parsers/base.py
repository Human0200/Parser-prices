from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import ClassVar, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


@dataclass(frozen=True, slots=True)
class ParsedProduct:
    external_id: str
    title: str
    price: Decimal
    original_price: Decimal | None = None
    discount_percent: int | None = None
    in_stock: bool = True
    image_url: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedListingItem:
    external_id: str
    title: str
    price: Decimal
    url: str
    image_url: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedListing:
    title: str
    items: list[ParsedListingItem] = field(default_factory=list)
    min_price: Decimal | None = None
    median_price: Decimal | None = None


_DOMAIN_MAP: dict[str, str] = {
    "wildberries.ru": "wildberries",
    "wb.ru": "wildberries",
    "ozon.ru": "ozon",
    "market.yandex.ru": "yandex_market",
}

UrlKind = Literal["product", "listing", "unknown"]


def normalize_listing_url(url: str) -> str:
    """Normalize listing URL for stable hashing (sorted query, drop fragment)."""
    parsed = urlparse(url.strip())
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, "")
    )


def listing_external_key(url: str) -> str:
    return hashlib.sha256(normalize_listing_url(url).encode("utf-8")).hexdigest()


def compute_listing_stats(
    items: list[ParsedListingItem],
) -> tuple[Decimal | None, Decimal | None]:
    if not items:
        return None, None
    prices = sorted(item.price for item in items)
    min_price = prices[0]
    mid = len(prices) // 2
    if len(prices) % 2:
        median = prices[mid]
    else:
        median = (prices[mid - 1] + prices[mid]) / Decimal(2)
    return min_price, median


def pick_min_price_item(
    items: list[ParsedListingItem],
) -> ParsedListingItem | None:
    """Cheapest item; ties broken by first occurrence."""
    if not items:
        return None
    return min(items, key=lambda item: item.price)


class BaseParser(ABC):
    marketplace: ClassVar[str]

    @abstractmethod
    async def parse_product(self, url_or_id: str) -> ParsedProduct: ...

    @abstractmethod
    def extract_product_id(self, url: str) -> str: ...

    @abstractmethod
    def build_url(self, product_id: str) -> str: ...

    @abstractmethod
    def is_listing_url(self, url: str) -> bool: ...

    @abstractmethod
    async def parse_listing(self, url: str) -> ParsedListing: ...

    def is_product_url(self, url: str) -> bool:
        try:
            self.extract_product_id(url)
            return True
        except ValueError:
            return False

    @staticmethod
    def detect_marketplace(url: str) -> str | None:
        url_lower = url.lower()
        for domain, marketplace in _DOMAIN_MAP.items():
            if re.search(rf"(?:^https?://(?:www\.)?|://)?" + re.escape(domain), url_lower):
                return marketplace
        return None

    @classmethod
    def detect_url_kind(cls, url: str) -> UrlKind:
        from app.parsers import PARSERS

        marketplace = cls.detect_marketplace(url)
        if marketplace is None:
            return "unknown"
        parser_cls = PARSERS.get(marketplace)
        if parser_cls is None:
            return "unknown"
        parser = parser_cls()
        if parser.is_listing_url(url):
            return "listing"
        if parser.is_product_url(url):
            return "product"
        return "unknown"
