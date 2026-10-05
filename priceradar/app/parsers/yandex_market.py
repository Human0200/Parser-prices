"""Парсер Яндекс Маркета: HTML-страница + JSON-LD через selectolax."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from selectolax.parser import HTMLParser

from app.parsers.base import (
    BaseParser,
    ParsedListing,
    ParsedListingItem,
    ParsedProduct,
    compute_listing_stats,
)
from app.parsers.utils import (
    BlockedError,
    NotFoundError,
    ParsingError,
    create_http_client,
    get_random_ua,
    retry_request,
)

logger = structlog.get_logger(__name__)

_PRODUCT_ID_RE = re.compile(
    r"market\.yandex\.ru/(?:product(?:--[^/]+)?|card/[^/]+)/(\d+)",
)
_LISTING_RE = re.compile(
    r"market\.yandex\.ru/(?:catalog(?:--[^/?#]*)?|category(?:/[^/?#]+)?|search|promo)(?:/|\?|$)",
    re.I,
)


class YandexMarketParser(BaseParser):
    marketplace: str = "yandex_market"

    def extract_product_id(self, url: str) -> str:
        match = _PRODUCT_ID_RE.search(url)
        if not match:
            raise ValueError(f"Cannot extract Yandex Market product ID from URL: {url}")
        return match.group(1)

    def build_url(self, product_id: str) -> str:
        return f"https://market.yandex.ru/card/product/{product_id}"

    def is_listing_url(self, url: str) -> bool:
        if _PRODUCT_ID_RE.search(url):
            return False
        return bool(_LISTING_RE.search(url))

    @retry_request
    async def parse_listing(self, url: str) -> ParsedListing:
        logger.info("yandex_market: fetching listing", url=url)
        desktop_ua = get_random_ua()
        headers: dict[str, str] = {
            "User-Agent": desktop_ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        async with create_http_client(headers=headers) as client:
            response = await client.get(url)

        if response.status_code == 403:
            raise BlockedError(f"Yandex Market blocked listing request for {url}")
        if response.status_code == 404:
            raise NotFoundError(f"Yandex Market listing not found: {url}")

        response.raise_for_status()
        html = response.text
        items = _items_from_yandex_html(html)
        if not items:
            raise ParsingError(f"No products found in Yandex Market listing: {url}")

        title = "Яндекс Маркет"
        title_match = re.search(r"<title>([^<]+)</title>", html, re.I)
        if title_match:
            title = title_match.group(1).strip()[:500]

        min_price, median_price = compute_listing_stats(items)
        return ParsedListing(
            title=title,
            items=items,
            min_price=min_price,
            median_price=median_price,
        )

    @retry_request
    async def parse_product(self, url_or_id: str) -> ParsedProduct:
        if url_or_id.startswith("http") or "market.yandex.ru" in url_or_id:
            product_id = self.extract_product_id(url_or_id)
        else:
            product_id = url_or_id.strip()

        page_url = self.build_url(product_id)
        logger.info("yandex_market: fetching product", product_id=product_id, url=page_url)

        desktop_ua = get_random_ua()
        headers: dict[str, str] = {
            "User-Agent": desktop_ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        async with create_http_client(headers=headers) as client:
            response = await client.get(page_url)

        if response.status_code == 403:
            raise BlockedError(
                f"Yandex Market blocked the request for product {product_id}"
            )
        if response.status_code == 404:
            raise NotFoundError(
                f"Yandex Market product {product_id} not found"
            )

        response.raise_for_status()
        html = response.text

        tree = HTMLParser(html)
        json_ld_nodes = tree.css('script[type="application/ld+json"]')

        if not json_ld_nodes:
            raise ParsingError(
                f"No JSON-LD blocks found on Yandex Market page for product {product_id}"
            )

        for node in json_ld_nodes:
            raw_text = node.text(strip=True)
            if not raw_text:
                continue

            try:
                data: Any = json.loads(raw_text)
            except json.JSONDecodeError:
                continue

            items: list[dict[str, Any]] = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") in ("Product", "IndividualProduct"):
                    parsed = self._extract_from_json_ld(item, product_id)
                    logger.info(
                        "yandex_market: product parsed",
                        product_id=product_id,
                        title=parsed.title,
                        price=str(parsed.price),
                        in_stock=parsed.in_stock,
                    )
                    return parsed

        raise ParsingError(
            f"No Product JSON-LD found on Yandex Market page for product {product_id}"
        )

    def _extract_from_json_ld(
        self,
        item: dict[str, Any],
        product_id: str,
    ) -> ParsedProduct:
        title: str = item.get("name", "")

        offers_raw: Any = item.get("offers", {})
        if isinstance(offers_raw, dict):
            offers_list: list[dict[str, Any]] = [offers_raw]
        elif isinstance(offers_raw, list):
            offers_list = offers_raw
        else:
            offers_list = []

        price: Decimal | None = None
        original_price: Decimal | None = None
        in_stock: bool = True
        currency: str = "RUB"

        for offer in offers_list:
            price = price or _parse_price_value(offer.get("price"))
            original_price = original_price or _parse_price_value(offer.get("highPrice"))
            if not original_price:
                original_price = _parse_price_value(offer.get("priceCurrency"))
                if original_price:
                    original_price = None

            availability: str = offer.get("availability", "")
            if availability and "InStock" not in availability:
                in_stock = False

            currency = offer.get("priceCurrency", currency)

        if price is None:
            raise ParsingError(
                f"Could not extract price from Yandex Market JSON-LD for product {product_id}"
            )

        image_url: str | None = None
        image_raw = item.get("image")
        if isinstance(image_raw, list) and image_raw:
            image_url = image_raw[0] if isinstance(image_raw[0], str) else image_raw[0].get("url")
        elif isinstance(image_raw, str):
            image_url = image_raw

        discount_percent: int | None = None
        if original_price and original_price > 0 and price < original_price:
            discount_percent = int(
                (original_price - price) / original_price * 100
            )

        return ParsedProduct(
            external_id=product_id,
            title=title,
            price=price,
            original_price=original_price,
            discount_percent=discount_percent,
            in_stock=in_stock,
            image_url=image_url,
        )


_PRICE_CLEAN_RE = re.compile(r"[^\d.,]")
_YM_PRODUCT_LINK_RE = re.compile(
    r"market\.yandex\.ru/(?:product(?:--[^/\"'\s]+)?|card/[^/\"'\s]+)/(\d+)",
    re.I,
)
_YM_SCHEMA_ITEM_RE = re.compile(
    r'\{\s*"sku"\s*:\s*"(?P<sku>\d+)"\s*,\s*"image"\s*:\s*"(?P<image>[^"]*)"\s*,'
    r'\s*"name"\s*:\s*"(?P<name>(?:\\.|[^"\\])*)"\s*,\s*"price"\s*:\s*(?P<price>\d+)'
    r'.*?"url"\s*:\s*"(?P<url>https?://market\.yandex\.ru/[^"]+)"',
    re.I | re.DOTALL,
)
_YM_SKU_PRICE_RE = re.compile(
    r'"sku"\s*:\s*"?(?P<sku>\d+)"?[^}]{0,800}?"price"\s*:\s*(?P<price>\d+)',
    re.I | re.DOTALL,
)
_YM_CARD_PRICE_RE = re.compile(
    r'https?://market\.yandex\.ru/card/[^/"\s]+/(?P<id>\d+)[^}]{0,200}?"price"\s*:\s*(?P<price>\d+)'
    r'|"price"\s*:\s*(?P<price2>\d+)[^}]{0,200}?https?://market\.yandex\.ru/card/[^/"\s]+/(?P<id2>\d+)',
    re.I | re.DOTALL,
)


def _parse_price_value(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))
    if not isinstance(raw, str):
        return None

    cleaned = _PRICE_CLEAN_RE.sub("", raw).replace(",", ".")
    if not cleaned:
        return None

    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _unescape_json_str(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value.replace('\\"', '"').replace("\\/", "/")


def _items_from_yandex_html(html: str) -> list[ParsedListingItem]:
    # Markdown/export dumps sometimes escape brackets
    html = html.replace("\\[", "[").replace("\\]", "]").replace("\\/", "/")
    items_by_id: dict[str, ParsedListingItem] = {}

    # 1) SearchSchemaOrg items: sku + name + price + url (/card/...)
    for match in _YM_SCHEMA_ITEM_RE.finditer(html):
        sku = match.group("sku")
        price = _parse_price_value(match.group("price"))
        if price is None:
            continue
        title = _unescape_json_str(match.group("name"))[:500]
        url = _unescape_json_str(match.group("url"))
        image = match.group("image") or None
        # Prefer numeric id from card URL as external_id when present
        card_id_match = _YM_PRODUCT_LINK_RE.search(url)
        external_id = card_id_match.group(1) if card_id_match else sku
        items_by_id[external_id] = ParsedListingItem(
            external_id=external_id,
            title=title or f"YM {external_id}",
            price=price,
            url=url if url.startswith("http") else f"https://market.yandex.ru/card/product/{external_id}",
            image_url=image,
        )

    if items_by_id:
        return list(items_by_id.values())

    # 2) Generic sku + price pairs
    for match in _YM_SKU_PRICE_RE.finditer(html):
        sku = match.group("sku")
        price = _parse_price_value(match.group("price"))
        if price is None:
            continue
        items_by_id[sku] = ParsedListingItem(
            external_id=sku,
            title=f"YM {sku}",
            price=price,
            url=f"https://market.yandex.ru/card/product/{sku}",
        )

    if items_by_id:
        return list(items_by_id.values())

    # 3) Legacy /product/ links
    for match in re.finditer(
        r'product(?:--[^/"\']+)?/(\d+)[^}]{0,400}?"price"\s*:\s*"?(\d+(?:[.,]\d+)?)"?',
        html,
        re.I,
    ):
        product_id, price_raw = match.group(1), match.group(2)
        price = _parse_price_value(price_raw)
        if price is None:
            continue
        items_by_id[product_id] = ParsedListingItem(
            external_id=product_id,
            title=f"YM {product_id}",
            price=price,
            url=f"https://market.yandex.ru/product/{product_id}",
        )

    if items_by_id:
        return list(items_by_id.values())

    # 4) ItemList JSON-LD
    tree = HTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        raw_text = node.text(strip=True)
        if not raw_text:
            continue
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            continue
        blocks = data if isinstance(data, list) else [data]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("@type") not in ("ItemList", "OfferCatalog"):
                continue
            for element in block.get("itemListElement", []) or []:
                item = element.get("item") if isinstance(element, dict) else None
                if not isinstance(item, dict):
                    continue
                url_val = item.get("url") or ""
                pid_match = _YM_PRODUCT_LINK_RE.search(str(url_val))
                if not pid_match:
                    continue
                product_id = pid_match.group(1)
                offers = item.get("offers") or {}
                price = _parse_price_value(
                    offers.get("price") if isinstance(offers, dict) else None
                )
                if price is None:
                    continue
                items_by_id[product_id] = ParsedListingItem(
                    external_id=product_id,
                    title=str(item.get("name") or f"YM {product_id}")[:500],
                    price=price,
                    url=str(url_val)
                    if str(url_val).startswith("http")
                    else f"https://market.yandex.ru/card/product/{product_id}",
                )

    return list(items_by_id.values())
