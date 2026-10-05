from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, quote, unquote_plus, urlencode, urlparse

import structlog

from app.config import settings
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
    retry_request,
)

logger = structlog.get_logger(__name__)

_WB_API_URL = (
    "https://card.wb.ru/cards/v2/detail"
    "?appType=1&curr=rub&dest=-1257786&spp=30&nm={article_id}"
)

_WB_SEARCH_API = (
    "https://search.wb.ru/exactmatch/ru/common/v7/search"
    "?appType=1&curr=rub&dest=-1257786&query={query}&resultset=catalog&page=1"
)

_WB_MENU_URL = (
    "https://static-basket-01.wbbasket.ru/vol0/data/main-menu-ru-ru-v3.json"
)

_PRODUCT_ID_RE = re.compile(r"wildberries\.ru/catalog/(\d+)(?:/detail\.aspx)?", re.I)
_PRODUCT_DETAIL_RE = re.compile(r"wildberries\.ru/catalog/\d+/detail\.aspx", re.I)
_LISTING_RE = re.compile(
    r"wildberries\.ru/(?:catalog/|search\.aspx|seller/)",
    re.I,
)
_SEARCH_QUERY_RE = re.compile(r"[?&]search=([^&]+)", re.I)

# Module-level menu cache (path -> {id, name, shard, query})
_MENU_BY_PATH: dict[str, dict[str, Any]] | None = None


class WildberriesParser(BaseParser):
    marketplace: str = "wildberries"

    def extract_product_id(self, url: str) -> str:
        if self.is_listing_url(url):
            raise ValueError(f"Cannot extract Wildberries product ID from URL: {url}")
        match = _PRODUCT_ID_RE.search(url)
        if not match:
            raise ValueError(f"Cannot extract Wildberries product ID from URL: {url}")
        return match.group(1)

    def build_url(self, product_id: str) -> str:
        return f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx"

    def is_listing_url(self, url: str) -> bool:
        if not _LISTING_RE.search(url):
            return False
        if "/detail" in url.lower():
            return False
        # numeric-only catalog path is a product page shortcut
        if re.search(r"wildberries\.ru/catalog/\d+/?(?:[?#]|$)", url, re.I):
            return False
        return True

    @retry_request
    async def parse_listing(self, url: str) -> ParsedListing:
        query_match = _SEARCH_QUERY_RE.search(url)
        items: list[ParsedListingItem] = []
        title = "Wildberries каталог"
        errors: list[str] = []

        headers = _wb_headers(referer=url)

        async with create_http_client(headers=headers) as client:
            cat: dict[str, Any] | None = None
            if query_match:
                query = unquote_plus(query_match.group(1))
                title = f"Поиск: {query}"
                api_url = _WB_SEARCH_API.format(query=quote(query))
                logger.info(
                    "wb: fetching search listing",
                    query=query,
                    cookies=bool(_wb_cookie_header()),
                )
                response = await client.get(api_url)
                if response.status_code == 200:
                    data = response.json()
                    items = _items_from_wb_products(_extract_wb_products(data))
                    if not items:
                        errors.append("search API empty products")
                else:
                    errors.append(f"search API HTTP {response.status_code}")
                    logger.warning(
                        "wb: search API blocked, trying HTML/Playwright fallback",
                        status=response.status_code,
                    )
            else:
                cat = await _resolve_wb_category(client, url)
                if cat and cat.get("shard"):
                    params = _catalog_params_from_listing_url(url, cat)
                    api_url = (
                        f"https://catalog.wb.ru/catalog/{cat['shard']}/v2/catalog"
                    )
                    logger.info(
                        "wb: fetching category catalog API",
                        shard=cat["shard"],
                        name=cat.get("name"),
                        cookies=bool(_wb_cookie_header()),
                    )
                    response = await client.get(api_url, params=params)
                    if response.status_code == 200:
                        data = response.json()
                        items = _items_from_wb_products(_extract_wb_products(data))
                        title = str(cat.get("name") or title)[:500]
                    else:
                        errors.append(f"catalog API HTTP {response.status_code}")
                        logger.warning(
                            "wb: catalog API blocked, trying HTML fallback",
                            status=response.status_code,
                        )

            if not items:
                logger.info("wb: fetching listing HTML", url=url)
                response = await client.get(url)
                if response.status_code in (403, 498, 429):
                    errors.append(f"HTML HTTP {response.status_code}")
                    logger.warning(
                        "wb: HTML blocked, will try Playwright",
                        status=response.status_code,
                    )
                elif response.status_code == 404:
                    raise NotFoundError(f"Wildberries listing not found: {url}")
                else:
                    response.raise_for_status()
                    items = _items_from_wb_html(response.text)
                    title_match = re.search(
                        r"<title>([^<]+)</title>", response.text, re.I
                    )
                    if title_match:
                        title = title_match.group(1).strip()[:500]
                    elif cat and cat.get("name"):
                        title = str(cat["name"])[:500]
                    if not items:
                        errors.append("HTML has no product cards")

            if not items:
                logger.info("wb: fetching listing via Playwright", url=url)
                try:
                    listing = await self._parse_listing_via_playwright(url)
                    if listing.items:
                        return listing
                    errors.append("Playwright empty items")
                except Exception as exc:
                    errors.append(f"Playwright: {exc}")
                    logger.warning("wb: playwright listing failed", error=str(exc))

        if not items:
            detail = f" ({'; '.join(errors)})" if errors else ""
            raise ParsingError(
                f"No products found in Wildberries listing: {url}{detail}"
            )

        min_price, median_price = compute_listing_stats(items)
        return ParsedListing(
            title=title,
            items=items,
            min_price=min_price,
            median_price=median_price,
        )

    async def _parse_listing_via_playwright(self, url: str) -> ParsedListing:
        from playwright.async_api import async_playwright

        dom_items: list[dict[str, Any]] = []
        html = ""
        page_title = ""
        captured_products: list[dict[str, Any]] = []

        async with async_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
            browser = await p.chromium.launch(headless=True, args=launch_args)

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/150.0.0.0 Safari/537.36"
                ),
                locale="ru-RU",
                viewport={"width": 1440, "height": 900},
                extra_http_headers={
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    **(
                        {"Authorization": _wb_authorization_header()}
                        if _wb_authorization_header()
                        else {}
                    ),
                },
            )
            pw_cookies = _parse_wb_cookie_header(_wb_cookie_header())
            if pw_cookies:
                await context.add_cookies(pw_cookies)

            page = await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            async def on_response(response) -> None:
                try:
                    req_url = response.url.lower()
                    if response.status != 200:
                        return
                    if not any(
                        token in req_url
                        for token in (
                            "catalog.wb.ru",
                            "search.wb.ru",
                            "exactmatch",
                            "u-search",
                            "/catalog/",
                        )
                    ):
                        return
                    # WB often returns catalog JSON as text/plain
                    text = await response.text()
                    if not text or text[0] not in "{[":
                        return
                    import json

                    data = json.loads(text)
                    products = _extract_wb_products(data)
                    if products:
                        captured_products.extend(products)
                except Exception:
                    return

            page.on("response", on_response)
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_selector(
                    "article.product-card, [data-nm-id]",
                    timeout=15000,
                )
            except Exception:
                try:
                    await page.wait_for_timeout(5000)
                except Exception:
                    pass

            html = await page.content()
            page_title = await page.title()
            if not captured_products:
                try:
                    await page.wait_for_timeout(3000)
                except Exception:
                    pass
            dom_items = await page.evaluate(
                """() => [...document.querySelectorAll('article.product-card[data-nm-id]')]
                .map(c => {
                  const a = c.querySelector('a[href*="/catalog/"]');
                  const text = (c.innerText || '').replace(/\\u00a0/g, ' ');
                  const m = text.match(/(\\d[\\d\\s]*)\\s*₽/);
                  return {
                    id: c.getAttribute('data-nm-id'),
                    title: (a && a.getAttribute('aria-label')) || '',
                    price_text: m ? m[1] : '',
                  };
                })"""
            )
            await browser.close()

        # Prefer rendered cards: they respect page filters better than merger API dumps.
        items: list[ParsedListingItem] = []
        if isinstance(dom_items, list):
            items = _items_from_wb_dom_cards(dom_items)
        if not items:
            items = _items_from_wb_products(captured_products)
        if not items:
            items = _items_from_wb_html(html)
        if not items:
            raise ParsingError(f"Playwright found no WB products for {url}")

        min_price, median_price = compute_listing_stats(items)
        return ParsedListing(
            title=(page_title or "Wildberries каталог")[:500],
            items=items,
            min_price=min_price,
            median_price=median_price,
        )

    @retry_request
    async def parse_product(self, url_or_id: str) -> ParsedProduct:
        if url_or_id.startswith("http") or "wildberries.ru" in url_or_id:
            product_id = self.extract_product_id(url_or_id)
        else:
            product_id = url_or_id.strip()

        api_url = _WB_API_URL.format(article_id=product_id)
        logger.info("wb: fetching product", product_id=product_id, url=api_url)

        async with create_http_client(headers=_wb_headers()) as client:
            response = await client.get(api_url)

        if response.status_code in (403, 498, 429):
            raise BlockedError(f"Wildberries blocked the request for product {product_id}")
        if response.status_code == 404:
            raise NotFoundError(f"Wildberries product {product_id} not found")

        response.raise_for_status()
        try:
            data: dict[str, Any] = response.json()
        except Exception as exc:
            raise ParsingError(f"Failed to decode JSON for WB product {product_id}") from exc

        products: list[dict[str, Any]] = (
            data.get("data", {}).get("products", [])
        )
        if not products:
            raise NotFoundError(
                f"Wildberries API returned no products for article {product_id}"
            )

        product: dict[str, Any] = products[0]

        title: str = product.get("name", "")

        # цены в API приходят в копейках
        sale_price_raw: int = product.get("salePriceU", 0)
        price = Decimal(sale_price_raw) / Decimal(100)

        original_price_raw: int = product.get("priceU", 0)
        original_price: Decimal | None = (
            Decimal(original_price_raw) / Decimal(100) if original_price_raw else None
        )

        discount_percent: int | None = product.get("sale")
        if discount_percent is None and original_price and original_price > 0 and price < original_price:
            discount_percent = int(
                ((original_price - price) / original_price * 100)
            )

        in_stock = _check_stock(product)
        image_url = _build_image_url(product_id)

        parsed = ParsedProduct(
            external_id=product_id,
            title=title,
            price=price,
            original_price=original_price,
            discount_percent=discount_percent,
            in_stock=in_stock,
            image_url=image_url,
        )

        logger.info(
            "wb: product parsed",
            product_id=product_id,
            title=title,
            price=str(price),
            in_stock=in_stock,
        )
        return parsed


def _wb_cookie_header() -> str:
    return (settings.WB_COOKIES or "").strip()


def _wb_authorization_header() -> str:
    raw = (settings.WB_AUTHORIZATION or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("bearer "):
        return raw
    return f"Bearer {raw}"


def _parse_wb_cookie_header(raw: str) -> list[dict[str, str]]:
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
        for domain in (".wildberries.ru", ".wb.ru"):
            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": "/",
                }
            )
    return cookies


def _wb_headers(*, referer: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.wildberries.ru",
        "Referer": referer or "https://www.wildberries.ru/",
    }
    cookies = _wb_cookie_header()
    if cookies:
        headers["Cookie"] = cookies
    auth = _wb_authorization_header()
    if auth:
        headers["Authorization"] = auth
    return headers


def _raise_wb_http(response: Any, url: str) -> None:
    code = response.status_code
    if code in (403, 498, 429):
        raise BlockedError(
            f"Wildberries blocked listing request ({code}) for {url}. "
            "Поставь свежие WB_COOKIES из браузера в .env (или PROXY_LIST)."
        )
    if code == 404:
        raise NotFoundError(f"Wildberries listing not found: {url}")
    response.raise_for_status()

async def _load_wb_menu(client: Any) -> dict[str, dict[str, Any]]:
    global _MENU_BY_PATH
    if _MENU_BY_PATH is not None:
        return _MENU_BY_PATH

    response = await client.get(_WB_MENU_URL)
    response.raise_for_status()
    data = response.json()
    mapping: dict[str, dict[str, Any]] = {}

    def walk(nodes: Any) -> None:
        if isinstance(nodes, list):
            for node in nodes:
                walk(node)
            return
        if not isinstance(nodes, dict):
            return
        raw_url = str(nodes.get("url") or "").strip()
        if raw_url.startswith("/catalog/"):
            path = raw_url.split("?")[0].rstrip("/")
            mapping[path] = {
                "id": nodes.get("id"),
                "name": nodes.get("name"),
                "shard": nodes.get("shard"),
                "query": nodes.get("query") or "",
                "url": path,
            }
        for child in nodes.get("childs") or nodes.get("children") or []:
            walk(child)

    walk(data)
    _MENU_BY_PATH = mapping
    logger.info("wb: menu loaded", categories=len(mapping))
    return mapping


async def _resolve_wb_category(client: Any, url: str) -> dict[str, Any] | None:
    path = urlparse(url).path.rstrip("/")
    menu = await _load_wb_menu(client)
    if path in menu:
        return menu[path]
    # longest prefix match
    matches = [p for p in menu if path.startswith(p)]
    if not matches:
        return None
    best = max(matches, key=len)
    return menu[best]


def _catalog_params_from_listing_url(url: str, cat: dict[str, Any]) -> dict[str, str]:
    params: dict[str, str] = {
        "appType": "1",
        "curr": "rub",
        "dest": "-1257786",
        "spp": "30",
        "sort": "popular",
        "page": "1",
    }
    # menu query like "subject=3690" or "cat=123&xsubject=..."
    for key, value in parse_qsl(str(cat.get("query") or ""), keep_blank_values=True):
        params[key] = value
    if cat.get("id") is not None and "cat" not in params and "subject" not in params:
        params["cat"] = str(cat["id"])

    for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True):
        if key in {"sort", "page", "priceU"} or key.startswith("f"):
            params[key] = value
    return params


def _check_stock(product: dict[str, Any]) -> bool:
    total_qty = product.get("totalQuantity")
    if total_qty is not None:
        return int(total_qty) > 0

    for size in product.get("sizes", []):
        for stock in size.get("stocks", []):
            if int(stock.get("qty", 0)) > 0:
                return True
    return False


def _build_image_url(product_id: str) -> str | None:
    """CDN-ссылка на картинку по схеме vol/part/id."""
    try:
        pid = int(product_id)
    except ValueError:
        return None

    vol = pid // 100_000
    part = pid // 1_000

    if vol <= 143:
        basket = "01"
    elif vol <= 287:
        basket = "02"
    elif vol <= 431:
        basket = "03"
    elif vol <= 719:
        basket = "04"
    elif vol <= 1007:
        basket = "05"
    elif vol <= 1061:
        basket = "06"
    elif vol <= 1115:
        basket = "07"
    elif vol <= 1169:
        basket = "08"
    elif vol <= 1313:
        basket = "09"
    elif vol <= 1601:
        basket = "10"
    elif vol <= 1655:
        basket = "11"
    elif vol <= 1919:
        basket = "12"
    elif vol <= 2045:
        basket = "13"
    elif vol <= 2189:
        basket = "14"
    elif vol <= 2405:
        basket = "15"
    elif vol <= 2621:
        basket = "16"
    else:
        basket = "17"

    return (
        f"https://basket-{basket}.wbbasket.ru"
        f"/vol{vol}/part{part}/{pid}/images/big/1.webp"
    )


def _extract_wb_products(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    products = data.get("products")
    if isinstance(products, list) and products:
        return products
    nested = data.get("data")
    if isinstance(nested, dict):
        products = nested.get("products")
        if isinstance(products, list) and products:
            return products
    return []


def _wb_product_price_kopecks(product: dict[str, Any]) -> int:
    for key in ("salePriceU", "priceU", "salePrice", "price"):
        raw = product.get(key)
        if isinstance(raw, (int, float)) and raw > 0:
            # salePrice/price may already be rubles in some payloads
            if key in ("salePrice", "price") and raw < 1_000_000:
                return int(raw * 100)
            return int(raw)
    sizes = product.get("sizes") or []
    if isinstance(sizes, list):
        for size in sizes:
            if not isinstance(size, dict):
                continue
            price_obj = size.get("price")
            if isinstance(price_obj, dict):
                for key in ("product", "total", "basic"):
                    raw = price_obj.get(key)
                    if isinstance(raw, (int, float)) and raw > 0:
                        return int(raw)
            raw = size.get("salePriceU") or size.get("priceU")
            if isinstance(raw, (int, float)) and raw > 0:
                return int(raw)
    return 0


def _items_from_wb_products(products: list[dict[str, Any]]) -> list[ParsedListingItem]:
    items: list[ParsedListingItem] = []
    seen: set[str] = set()
    for product in products:
        pid = product.get("id") or product.get("nmId") or product.get("nm_id")
        if pid is None:
            continue
        product_id = str(pid)
        if product_id in seen:
            continue
        sale_price_raw = _wb_product_price_kopecks(product)
        if not sale_price_raw:
            continue
        price = Decimal(sale_price_raw) / Decimal(100)
        title = product.get("name") or f"WB {product_id}"
        brand = product.get("brand")
        if brand and str(brand) not in str(title):
            title = f"{title} {brand}"
        seen.add(product_id)
        items.append(
            ParsedListingItem(
                external_id=product_id,
                title=str(title)[:500],
                price=price,
                url=f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx",
                image_url=_build_image_url(product_id),
            )
        )
    return items


def _items_from_wb_dom_cards(cards: list[dict[str, Any]]) -> list[ParsedListingItem]:
    items: list[ParsedListingItem] = []
    for card in cards:
        product_id = str(card.get("id") or "").strip()
        price_text = str(card.get("price_text") or "")
        digits = re.sub(r"\D", "", price_text)
        if not product_id or not digits:
            continue
        title = str(card.get("title") or f"WB {product_id}")[:500]
        items.append(
            ParsedListingItem(
                external_id=product_id,
                title=title,
                price=Decimal(digits),
                url=f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx",
                image_url=_build_image_url(product_id),
            )
        )
    return items


_WB_HTML_ID_RE = re.compile(r"catalog/(\d+)/detail\.aspx")
_WB_HTML_PRICE_RE = re.compile(r'"salePriceU"\s*:\s*(\d+)|"priceU"\s*:\s*(\d+)')
_WB_HTML_CARD_RE = re.compile(
    r'<article[^>]*data-nm-id="(\d+)"[^>]*>.*?'
    r'aria-label="([^"]*)".*?'
    r'(\d[\d\s\u00a0]*)\s*₽',
    re.I | re.DOTALL,
)


def _items_from_wb_html(html: str) -> list[ParsedListingItem]:
    # Prefer embedded JSON product blobs
    products: list[dict[str, Any]] = []
    for match in re.finditer(
        r'(\{[^{}]*"id"\s*:\s*\d+[^{}]*"(?:salePriceU|priceU)"\s*:\s*\d+[^{}]*\})',
        html,
    ):
        try:
            import json

            products.append(json.loads(match.group(1)))
        except Exception:
            continue
    if products:
        return _items_from_wb_products(products)

    items: list[ParsedListingItem] = []
    seen: set[str] = set()
    for product_id, title, price_text in _WB_HTML_CARD_RE.findall(html):
        if product_id in seen:
            continue
        digits = re.sub(r"\D", "", price_text)
        if not digits:
            continue
        seen.add(product_id)
        items.append(
            ParsedListingItem(
                external_id=product_id,
                title=(title or f"WB {product_id}")[:500],
                price=Decimal(digits),
                url=f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx",
                image_url=_build_image_url(product_id),
            )
        )
    if items:
        return items

    ids = list(dict.fromkeys(_WB_HTML_ID_RE.findall(html)))
    prices_raw = _WB_HTML_PRICE_RE.findall(html)
    prices: list[Decimal] = []
    for a, b in prices_raw:
        raw = a or b
        if raw:
            prices.append(Decimal(raw) / Decimal(100))

    for idx, product_id in enumerate(ids[:50]):
        price = prices[idx] if idx < len(prices) else None
        if price is None:
            continue
        items.append(
            ParsedListingItem(
                external_id=product_id,
                title=f"WB {product_id}",
                price=price,
                url=f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx",
                image_url=_build_image_url(product_id),
            )
        )
    return items
