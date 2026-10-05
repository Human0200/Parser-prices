"""Парсер Ozon: мобильный API + fallback на HTML с JSON-LD."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

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


def _ozon_cookie_header() -> str:
    try:
        from app.config import settings

        return (settings.OZON_COOKIES or "").strip()
    except Exception:
        return ""


def _parse_cookie_header(raw: str) -> list[dict[str, str]]:
    """Convert Cookie header string into Playwright cookie dicts."""
    cookies: list[dict[str, str]] = []
    if not raw:
        return cookies
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".ozon.ru",
                "path": "/",
            }
        )
    return cookies

_PRODUCT_ID_RE = re.compile(r"ozon\.ru/product/.*?-(\d+)/?")
_LISTING_PATH_RE = re.compile(r"ozon\.ru/(?:category|search)/", re.IGNORECASE)

_MOBILE_API_URL = (
    "https://api.ozon.ru/composer-api.bx/page/json/v2"
    "?url=/product/{product_id}/"
)

_LISTING_API_CANDIDATES: tuple[str, ...] = (
    "https://api.ozon.ru/composer-api.bx/page/json/v2?url={path}",
    "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url={path}",
    "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url={path}",
)

_MOBILE_HEADERS: dict[str, str] = {
    "x-o3-app-name": "ozonapp_android",
    "x-o3-app-version": "17.35.0",
}

_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)

_SKU_IN_TEXT_RE = re.compile(r"(?:product/[^\"'\s]*?-)?(\d{6,})")
_PRICE_IN_OBJ_KEYS = ("price", "finalPrice", "cardPrice", "accentPrice")
_EMBEDDED_STATE_RE = re.compile(
    r'<script[^>]*id=["\'](?:state|__NEXT_DATA__|apollo-state)["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.I,
)
_PRODUCT_HREF_PRICE_RE = re.compile(
    r'href="(/product/[^"]*?-(\d+)/?)"[^>]{0,400}?(?:price|Price)[^0-9]{0,40}(\d[\d\s\u00a0]*)',
    re.I | re.DOTALL,
)


class OzonParser(BaseParser):
    marketplace: str = "ozon"

    def extract_product_id(self, url: str) -> str:
        match = _PRODUCT_ID_RE.search(url)
        if not match:
            raise ValueError(f"Cannot extract Ozon product ID from URL: {url}")
        return match.group(1)

    def build_url(self, product_id: str) -> str:
        return f"https://www.ozon.ru/product/{product_id}/"

    def is_listing_url(self, url: str) -> bool:
        return bool(_LISTING_PATH_RE.search(url))

    async def parse_listing(self, url: str) -> ParsedListing:
        from urllib.parse import urlparse, quote

        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        logger.info("ozon: fetching listing", path=path)

        errors: list[str] = []

        # 1) JSON composer/entrypoint APIs
        for template in _LISTING_API_CANDIDATES:
            api_url = template.format(path=quote(path, safe="/?=&%"))
            try:
                payload = await self._fetch_listing_json(api_url)
                listing = self._listing_from_payload(payload)
                if listing.items:
                    logger.info(
                        "ozon: listing via API",
                        api=api_url.split("?")[0],
                        items=len(listing.items),
                    )
                    return listing
                errors.append(f"{api_url}: empty items")
            except Exception as exc:
                errors.append(f"{api_url}: {exc}")
                logger.warning("ozon: listing API failed", api=api_url, error=str(exc))

        # 2) HTML page scrape
        try:
            listing = await self._parse_listing_via_html(url)
            if listing.items:
                logger.info("ozon: listing via HTML", items=len(listing.items))
                return listing
            errors.append("html: empty items")
        except Exception as exc:
            errors.append(f"html: {exc}")
            logger.warning("ozon: listing HTML failed", error=str(exc))

        # 3) Playwright (real browser) as last resort
        try:
            listing = await self._parse_listing_via_playwright(url)
            if listing.items:
                logger.info("ozon: listing via Playwright", items=len(listing.items))
                return listing
            errors.append("playwright: empty items")
        except Exception as exc:
            errors.append(f"playwright: {exc}")
            logger.warning("ozon: listing Playwright failed", error=str(exc))

        detail = "; ".join(errors[-5:])
        if any("403" in e or "blocked" in e.lower() for e in errors):
            raise BlockedError(
                f"Ozon blocked the listing request for {url}. Tried: {detail}"
            )
        raise ParsingError(f"Could not parse Ozon listing {url}. Tried: {detail}")

    async def _fetch_listing_json(self, api_url: str) -> dict[str, Any]:
        mobile_ua = (
            "Mozilla/5.0 (Linux; Android 14; SM-S918B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Mobile Safari/537.36"
        )
        headers: dict[str, str] = {
            "User-Agent": mobile_ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": "https://www.ozon.ru/",
            "Origin": "https://www.ozon.ru",
            **_MOBILE_HEADERS,
        }
        cookie = _ozon_cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        async with create_http_client(headers=headers) as client:
            response = await client.get(api_url)

        if response.status_code == 403:
            raise BlockedError(f"403 from {api_url}")
        if response.status_code == 404:
            raise NotFoundError(f"404 from {api_url}")
        response.raise_for_status()
        return response.json()

    def _listing_from_payload(self, payload: dict[str, Any]) -> ParsedListing:
        title = _extract_listing_title(payload) or "Ozon категория"
        items = _extract_listing_items(payload)
        min_price, median_price = compute_listing_stats(items)
        return ParsedListing(
            title=title,
            items=items,
            min_price=min_price,
            median_price=median_price,
        )

    async def _parse_listing_via_html(self, url: str) -> ParsedListing:
        headers = {
            "User-Agent": get_random_ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.ozon.ru/",
            "Upgrade-Insecure-Requests": "1",
        }
        cookie = _ozon_cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        async with create_http_client(headers=headers) as client:
            response = await client.get(url)

        if response.status_code == 403:
            raise BlockedError(f"Ozon blocked HTML listing for {url}")
        if response.status_code == 404:
            raise NotFoundError(f"Ozon listing not found: {url}")
        response.raise_for_status()
        return _listing_from_html(response.text, fallback_title="Ozon категория")

    async def _parse_listing_via_playwright(self, url: str) -> ParsedListing:
        from pathlib import Path
        from urllib.parse import quote, urlparse

        from playwright.async_api import async_playwright

        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        async with async_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
            try:
                browser = await p.chromium.launch(
                    headless=True,
                    channel="chrome",
                    args=launch_args,
                )
            except Exception:
                browser = await p.chromium.launch(headless=True, args=launch_args)

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="ru-RU",
                viewport={"width": 1440, "height": 900},
                extra_http_headers={
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )
            pw_cookies = _parse_cookie_header(_ozon_cookie_header())
            if pw_cookies:
                await context.add_cookies(pw_cookies)

            page = await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            captured: list[dict[str, Any]] = []

            async def on_response(response) -> None:
                try:
                    req_url = response.url.lower()
                    interesting = any(
                        token in req_url
                        for token in (
                            "page/json",
                            "composer-api",
                            "entrypoint-api",
                            "tilejson",
                            "searchapi",
                            "category",
                        )
                    )
                    if not interesting:
                        return
                    if response.status != 200:
                        return
                    ctype = (response.headers.get("content-type") or "").lower()
                    if "json" not in ctype and "javascript" not in ctype:
                        return
                    data = await response.json()
                    if isinstance(data, dict):
                        captured.append(data)
                    elif isinstance(data, list):
                        captured.append({"items": data})
                except Exception:
                    return

            page.on("response", on_response)
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Give antibot / SPA time to settle
            for _ in range(8):
                await page.wait_for_timeout(1000)
                product_count = await page.locator('a[href*="/product/"]').count()
                if product_count >= 3 or captured:
                    break
                await page.mouse.wheel(0, 1200)

            try:
                await page.wait_for_selector('a[href*="/product/"]', timeout=10000)
            except Exception:
                pass

            await page.wait_for_timeout(2000)
            html = await page.content()

            # Retry composer API inside browser context (has cookies)
            for template in _LISTING_API_CANDIDATES:
                api_url = template.format(path=quote(path, safe="/?=&%"))
                try:
                    data = await page.evaluate(
                        """async (apiUrl) => {
                            const r = await fetch(apiUrl, {
                              credentials: 'include',
                              headers: { 'Accept': 'application/json' }
                            });
                            if (!r.ok) return {__status: r.status};
                            return await r.json();
                        }""",
                        api_url,
                    )
                    if isinstance(data, dict) and "__status" not in data:
                        captured.append(data)
                except Exception:
                    continue

            await browser.close()

        for payload in captured:
            listing = self._listing_from_payload(payload)
            if listing.items:
                return listing

        listing = _listing_from_html(html, fallback_title="Ozon категория")
        if not listing.items:
            dump = Path("/tmp/ozon_listing_debug.html")
            try:
                dump.write_text(html[:500_000], encoding="utf-8")
                logger.warning(
                    "ozon: playwright empty, dumped html",
                    path=str(dump),
                    html_len=len(html),
                    captured=len(captured),
                )
            except Exception:
                pass
        return listing

    @retry_request
    async def parse_product(self, url_or_id: str) -> ParsedProduct:
        if url_or_id.startswith("http") or "ozon.ru" in url_or_id:
            product_id = self.extract_product_id(url_or_id)
        else:
            product_id = url_or_id.strip()

        logger.info("ozon: fetching product", product_id=product_id)

        # сначала пробуем мобильный API
        try:
            result = await self._parse_via_api(product_id)
            logger.info("ozon: parsed via mobile API", product_id=product_id)
            return result
        except NotFoundError:
            raise
        except Exception as exc:
            logger.warning(
                "ozon: mobile API failed, falling back to HTML",
                product_id=product_id,
                error=str(exc),
            )

        # fallback на HTML + JSON-LD
        result = await self._parse_via_html(product_id)
        logger.info("ozon: parsed via HTML fallback", product_id=product_id)
        return result

    async def _parse_via_api(self, product_id: str) -> ParsedProduct:
        api_url = _MOBILE_API_URL.format(product_id=product_id)

        mobile_ua = (
            "Mozilla/5.0 (Linux; Android 14; SM-S918B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Mobile Safari/537.36"
        )
        headers: dict[str, str] = {
            "User-Agent": mobile_ua,
            **_MOBILE_HEADERS,
        }
        cookie = _ozon_cookie_header()
        if cookie:
            headers["Cookie"] = cookie

        async with create_http_client(headers=headers) as client:
            response = await client.get(api_url)

        if response.status_code == 403:
            raise BlockedError(f"Ozon blocked the API request for product {product_id}")
        if response.status_code == 404:
            raise NotFoundError(f"Ozon product {product_id} not found via API")

        response.raise_for_status()

        try:
            payload: dict[str, Any] = response.json()
        except Exception as exc:
            raise ParsingError(f"Failed to decode Ozon API JSON for product {product_id}") from exc

        return self._extract_from_api_payload(payload, product_id)

    def _extract_from_api_payload(
        self,
        payload: dict[str, Any],
        product_id: str,
    ) -> ParsedProduct:
        title: str = ""
        price: Decimal | None = None
        original_price: Decimal | None = None
        image_url: str | None = None
        in_stock: bool = True

        widget_states: dict[str, Any] = payload.get("widgetStates", {})

        for _key, raw_value in widget_states.items():
            if isinstance(raw_value, str):
                try:
                    value = json.loads(raw_value)
                except (json.JSONDecodeError, TypeError):
                    continue
            else:
                value = raw_value

            if not isinstance(value, dict):
                continue

            web_price = value.get("webPrice") or value.get("price")
            if isinstance(web_price, dict):
                price = price or _parse_price_string(web_price.get("price"))
                original_price = original_price or _parse_price_string(
                    web_price.get("originalPrice")
                )

            if not title:
                title = value.get("title", "") or value.get("productTitle", "")

            if not image_url:
                covers = value.get("coverImage") or value.get("images") or value.get("gallery")
                if isinstance(covers, list) and covers:
                    first = covers[0]
                    image_url = first if isinstance(first, str) else first.get("src") or first.get("url")
                elif isinstance(covers, str):
                    image_url = covers

            if value.get("isOutOfStock") is True:
                in_stock = False

        if price is None:
            raise ParsingError(
                f"Could not extract price from Ozon API response for product {product_id}"
            )

        discount_percent = _calc_discount(price, original_price)

        return ParsedProduct(
            external_id=product_id,
            title=title,
            price=price,
            original_price=original_price,
            discount_percent=discount_percent,
            in_stock=in_stock,
            image_url=image_url,
        )

    async def _parse_via_html(self, product_id: str) -> ParsedProduct:
        page_url = self.build_url(product_id)

        headers: dict[str, str] = {}
        cookie = _ozon_cookie_header()
        if cookie:
            headers["Cookie"] = cookie

        async with create_http_client(headers=headers) as client:
            response = await client.get(page_url)

        if response.status_code == 403:
            raise BlockedError(f"Ozon blocked the HTML request for product {product_id}")
        if response.status_code == 404:
            raise NotFoundError(f"Ozon product {product_id} not found")

        response.raise_for_status()
        html = response.text

        json_ld_blocks = _JSON_LD_RE.findall(html)
        if not json_ld_blocks:
            raise ParsingError(
                f"No JSON-LD blocks found on Ozon page for product {product_id}"
            )

        for block in json_ld_blocks:
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue

            items: list[dict[str, Any]] = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") in ("Product", "IndividualProduct"):
                    return self._extract_from_json_ld(item, product_id)

        raise ParsingError(
            f"No Product JSON-LD found on Ozon page for product {product_id}"
        )

    def _extract_from_json_ld(
        self,
        item: dict[str, Any],
        product_id: str,
    ) -> ParsedProduct:
        title: str = item.get("name", "")

        offers: dict[str, Any] = item.get("offers", {})
        price = _parse_price_string(offers.get("price"))
        if price is None:
            price = _parse_price_string(offers.get("lowPrice"))
        if price is None:
            raise ParsingError(
                f"Could not extract price from Ozon JSON-LD for product {product_id}"
            )

        original_price = _parse_price_string(offers.get("highPrice"))

        availability: str = offers.get("availability", "")
        in_stock = "InStock" in availability if availability else True

        image_url: str | None = None
        image_raw = item.get("image")
        if isinstance(image_raw, list) and image_raw:
            image_url = image_raw[0]
        elif isinstance(image_raw, str):
            image_url = image_raw

        discount_percent = _calc_discount(price, original_price)

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


def _parse_price_string(raw: Any) -> Decimal | None:
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


def _calc_discount(
    price: Decimal,
    original_price: Decimal | None,
) -> int | None:
    if original_price and original_price > 0 and price < original_price:
        return int((original_price - price) / original_price * 100)
    return None


def _extract_listing_title(payload: dict[str, Any]) -> str:
    seo = payload.get("seo") or payload.get("pageInfo") or {}
    if isinstance(seo, dict):
        for key in ("title", "h1", "name"):
            value = seo.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _walk_json(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_json(item)


def _coerce_widget(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


def _price_from_obj(obj: dict[str, Any]) -> Decimal | None:
    for key in _PRICE_IN_OBJ_KEYS:
        if key in obj:
            price = _parse_price_string(obj.get(key))
            if price is not None:
                return price
    for nested_key in ("price", "prices", "mainState", "cellTrackingInfo"):
        nested = obj.get(nested_key)
        if isinstance(nested, dict):
            price = _price_from_obj(nested)
            if price is not None:
                return price
        elif isinstance(nested, str):
            price = _parse_price_string(nested)
            if price is not None:
                return price
    return None


def _id_from_obj(obj: dict[str, Any]) -> str | None:
    for key in ("sku", "productId", "id", "itemId"):
        value = obj.get(key)
        if value is None:
            continue
        text = str(value)
        if text.isdigit() and len(text) >= 6:
            return text
    link = obj.get("link") or obj.get("deepLink")
    action = obj.get("action")
    if isinstance(action, dict):
        link = link or action.get("link")
    if isinstance(link, str):
        match = _PRODUCT_ID_RE.search(link) or _SKU_IN_TEXT_RE.search(link)
        if match:
            return match.group(1)
    return None


def _extract_listing_items(payload: dict[str, Any]) -> list[ParsedListingItem]:
    widget_states = payload.get("widgetStates", {})
    items_by_id: dict[str, ParsedListingItem] = {}

    roots: list[Any] = [payload]
    if isinstance(widget_states, dict):
        for raw in widget_states.values():
            roots.append(_coerce_widget(raw))

    for root in roots:
        for obj in _walk_json(root):
            if not isinstance(obj, dict):
                continue
            sku = _id_from_obj(obj)
            price = _price_from_obj(obj)
            if not sku or price is None:
                continue
            title = (
                obj.get("title")
                or obj.get("name")
                or obj.get("productTitle")
                or f"Ozon {sku}"
            )
            if not isinstance(title, str):
                title = f"Ozon {sku}"
            image_url = None
            for img_key in ("image", "imageUrl", "coverImageUrl", "picture"):
                raw_img = obj.get(img_key)
                if isinstance(raw_img, str):
                    image_url = raw_img
                    break
                if isinstance(raw_img, list) and raw_img:
                    first = raw_img[0]
                    image_url = first if isinstance(first, str) else None
                    break
            items_by_id[sku] = ParsedListingItem(
                external_id=sku,
                title=title.strip()[:500],
                price=price,
                url=f"https://www.ozon.ru/product/{sku}/",
                image_url=image_url,
            )

    return list(items_by_id.values())


def _listing_from_html(html: str, fallback_title: str = "Ozon категория") -> ParsedListing:
    title = fallback_title
    title_match = re.search(r"<title>([^<]+)</title>", html, re.I)
    if title_match:
        title = title_match.group(1).strip()[:500]

    # Prefer embedded JSON blobs (widgetStates / state scripts)
    for match in _EMBEDDED_STATE_RE.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            items = _extract_listing_items(data)
            if items:
                min_price, median_price = compute_listing_stats(items)
                return ParsedListing(
                    title=_extract_listing_title(data) or title,
                    items=items,
                    min_price=min_price,
                    median_price=median_price,
                )

    # Scan whole HTML for JSON-ish product structures
    items_by_id: dict[str, ParsedListingItem] = {}
    for match in re.finditer(
        r'"(?:sku|productId|id)"\s*:\s*"?(\d{6,})"?[^}]{0,500}?"(?:price|finalPrice|cardPrice)"\s*:\s*"?(\d[\d\s]*)"?',
        html,
        re.I,
    ):
        sku, price_raw = match.group(1), match.group(2)
        price = _parse_price_string(price_raw)
        if price is None:
            continue
        items_by_id[sku] = ParsedListingItem(
            external_id=sku,
            title=f"Ozon {sku}",
            price=price,
            url=f"https://www.ozon.ru/product/{sku}/",
        )

    if not items_by_id:
        for match in _PRODUCT_HREF_PRICE_RE.finditer(html):
            sku = match.group(2)
            price = _parse_price_string(match.group(3))
            if price is None:
                continue
            items_by_id[sku] = ParsedListingItem(
                external_id=sku,
                title=f"Ozon {sku}",
                price=price,
                url=f"https://www.ozon.ru{match.group(1)}",
            )

    items = list(items_by_id.values())
    min_price, median_price = compute_listing_stats(items)
    return ParsedListing(
        title=title,
        items=items,
        min_price=min_price,
        median_price=median_price,
    )
