from __future__ import annotations

import math
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

if TYPE_CHECKING:
    from app.models.product import TrackedProduct


def _build_markup(buttons: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _fmt_price(value) -> str:
    if value is None:
        return "N/A"
    return f"{value:,.0f} \u20bd".replace(",", "\u202f")


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return _build_markup(
        [
            [InlineKeyboardButton(text="🔍 Добавить товар", callback_data="add_product")],
            [InlineKeyboardButton(text="📂 Мои категории", callback_data="my_categories")],
            [InlineKeyboardButton(text="📊 Мои товары", callback_data="my_products")],
            [InlineKeyboardButton(text="🔔 Настроить алерты", callback_data="alerts_settings")],
            [InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
        ]
    )


def category_actions_keyboard(category_id: int) -> InlineKeyboardMarkup:
    return _build_markup(
        [
            [
                InlineKeyboardButton(
                    text="🔔 Алерты категории",
                    callback_data=f"category_alerts:{category_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить выдачу",
                    callback_data=f"refresh_category:{category_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"delete_category:{category_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="my_categories",
                ),
            ],
        ]
    )


def category_alert_type_keyboard(category_id: int) -> InlineKeyboardMarkup:
    return _build_markup(
        [
            [
                InlineKeyboardButton(
                    text="📉 Падение мин. цены",
                    callback_data=f"cat_alert_min_drop:{category_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎯 Дешевле своей цены",
                    callback_data=f"cat_alert_below:{category_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🆕 Новый товар в выдаче",
                    callback_data=f"cat_alert_new_sku:{category_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=f"category_detail:{category_id}",
                ),
            ],
        ]
    )


CATEGORIES_PER_PAGE: int = 5


def _category_list_label(category) -> str:
    """Telegram inline button text is limited to 64 characters."""
    price = _fmt_price(category.min_price)
    title = (getattr(category, "title", None) or "Категория").strip()
    product = (getattr(category, "min_price_title", None) or "").strip()

    if product:
        suffix = f" — {price}"
        mid = " · "
        budget = 64 - len(suffix) - len(mid)
        if budget < 8:
            label = f"{product} — {price}"
            return label if len(label) <= 64 else f"{product[: 64 - len(suffix) - 1]}…{suffix}"
        title_budget = min(16, max(6, budget // 3))
        product_budget = budget - title_budget
        short_title = title if len(title) <= title_budget else f"{title[: title_budget - 1]}…"
        short_product = (
            product if len(product) <= product_budget else f"{product[: product_budget - 1]}…"
        )
        return f"{short_title}{mid}{short_product}{suffix}"

    label = f"{title} — мин. {price}"
    return label if len(label) <= 64 else f"{title[: 64 - len(f' — мин. {price}') - 1]}… — мин. {price}"


def categories_pagination_keyboard(
    categories: list,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    start = page * CATEGORIES_PER_PAGE
    end = start + CATEGORIES_PER_PAGE
    for category in categories[start:end]:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=_category_list_label(category),
                    callback_data=f"category_detail:{category.id}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(text="◀️", callback_data=f"categories_page:{page - 1}")
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(text="▶️", callback_data=f"categories_page:{page + 1}")
        )
    if nav_row:
        buttons.append(nav_row)

    buttons.append(
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    )
    return _build_markup(buttons)


def confirm_delete_category_keyboard(category_id: int) -> InlineKeyboardMarkup:
    return _build_markup(
        [
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить",
                    callback_data=f"confirm_delete_category:{category_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"category_detail:{category_id}",
                ),
            ],
        ]
    )


def product_actions_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return _build_markup(
        [
            [
                InlineKeyboardButton(
                    text="\U0001f4c8 \u0418\u0441\u0442\u043e\u0440\u0438\u044f \u0446\u0435\u043d",
                    callback_data=f"price_history:{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="\U0001f514 \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0430\u043b\u0435\u0440\u0442",
                    callback_data=f"add_alert:{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="\U0001f504 \u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c \u0446\u0435\u043d\u0443",
                    callback_data=f"refresh_price:{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="\U0001f5d1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c",
                    callback_data=f"delete_product:{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="\u25c0\ufe0f \u041d\u0430\u0437\u0430\u0434",
                    callback_data="my_products",
                ),
            ],
        ]
    )


def alert_type_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return _build_markup(
        [
            [
                InlineKeyboardButton(
                    text="\U0001f4c9 \u0421\u043d\u0438\u0436\u0435\u043d\u0438\u0438 \u0446\u0435\u043d\u044b",
                    callback_data=f"alert_drop:{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="\U0001f4e6 \u041f\u043e\u044f\u0432\u043b\u0435\u043d\u0438\u0438 \u0432 \u043d\u0430\u043b\u0438\u0447\u0438\u0438",
                    callback_data=f"alert_stock:{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="\u270f\ufe0f \u0421\u0432\u043e\u044f \u0446\u0435\u043d\u0430",
                    callback_data=f"alert_custom:{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="\u25c0\ufe0f \u041d\u0430\u0437\u0430\u0434",
                    callback_data=f"product_detail:{product_id}",
                ),
            ],
        ]
    )


def subscription_keyboard() -> InlineKeyboardMarkup:
    return _build_markup(
        [
            [
                InlineKeyboardButton(
                    text="\u2b50 Basic \u2014 990 \u20bd/\u043c\u0435\u0441",
                    callback_data="subscribe:basic",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="\U0001f451 Pro \u2014 2 490 \u20bd/\u043c\u0435\u0441",
                    callback_data="subscribe:pro",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="\u25c0\ufe0f \u0413\u043b\u0430\u0432\u043d\u043e\u0435 \u043c\u0435\u043d\u044e",
                    callback_data="main_menu",
                ),
            ],
        ]
    )


PRODUCTS_PER_PAGE: int = 5


def products_pagination_keyboard(
    products: list[TrackedProduct],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []

    start = page * PRODUCTS_PER_PAGE
    end = start + PRODUCTS_PER_PAGE
    page_products = products[start:end]

    for product in page_products:
        price_str = _fmt_price(product.current_price)
        label = f"{product.title[:40]} \u2014 {price_str}"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"product_detail:{product.id}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(text="\u25c0\ufe0f", callback_data=f"products_page:{page - 1}")
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(text="\u25b6\ufe0f", callback_data=f"products_page:{page + 1}")
        )
    if nav_row:
        buttons.append(nav_row)

    buttons.append(
        [InlineKeyboardButton(text="\U0001f3e0 \u0413\u043b\u0430\u0432\u043d\u043e\u0435 \u043c\u0435\u043d\u044e", callback_data="main_menu")]
    )

    return _build_markup(buttons)


def confirm_delete_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return _build_markup(
        [
            [
                InlineKeyboardButton(
                    text="\u2705 \u0414\u0430, \u0443\u0434\u0430\u043b\u0438\u0442\u044c",
                    callback_data=f"confirm_delete:{product_id}",
                ),
                InlineKeyboardButton(
                    text="\u274c \u041e\u0442\u043c\u0435\u043d\u0430",
                    callback_data=f"product_detail:{product_id}",
                ),
            ],
        ]
    )


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return _build_markup(
        [
            [InlineKeyboardButton(text="\U0001f3e0 \u0413\u043b\u0430\u0432\u043d\u043e\u0435 \u043c\u0435\u043d\u044e", callback_data="main_menu")],
        ]
    )
