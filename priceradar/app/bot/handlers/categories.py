from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import (
    CATEGORIES_PER_PAGE,
    back_to_menu_keyboard,
    categories_pagination_keyboard,
    category_actions_keyboard,
    category_alert_type_keyboard,
    confirm_delete_category_keyboard,
    main_menu_keyboard,
)
from app.database import async_session_factory
from app.models.alert import RuleType
from app.models.user import User
from app.services.alert_service import AlertService
from app.services.category_service import CategoryService
from app.services.subscription_service import SubscriptionService

logger = structlog.get_logger()

router = Router(name="categories")

_MSK = ZoneInfo("Europe/Moscow")


class CategoryAlertStates(StatesGroup):
    waiting_for_threshold = State()


def _fmt_price(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:,.0f} ₽".replace(",", "\u202f")


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "ещё не обновлялось"
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(_MSK).strftime("%d.%m.%Y %H:%M")


MARKETPLACE_LABELS: dict[str, str] = {
    "wildberries": "Wildberries",
    "ozon": "Ozon",
    "yandex_market": "Яндекс Маркет",
}


def _category_card(category) -> str:
    mp = MARKETPLACE_LABELS.get(category.marketplace.value, category.marketplace.value)
    lines = [
        f"📂 <b>{category.title}</b>",
        f"🏪 {mp}",
        f"📦 В выдаче: {category.item_count}",
        f"💰 Мин. цена: {_fmt_price(category.min_price)}",
        f"🕒 Обновлено: {_fmt_dt(category.last_parsed_at)}",
    ]
    if category.min_price_url:
        title = category.min_price_title or "товар"
        # Escape is already HTML mode; titles may contain <>& — keep simple
        safe_title = (
            title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        lines.append(f"🏷 <a href=\"{category.min_price_url}\">{safe_title}</a>")
        lines.append(f"🔗 Мин. товар: {category.min_price_url}")
    lines.append(f"🔗 Выдача: {category.url}")
    return "\n".join(lines)



async def _get_user(telegram_id: int, first_name: str, username: str | None) -> User:
    async with async_session_factory() as session:
        svc = SubscriptionService(session)
        user = await svc.get_or_create_user(
            telegram_id=telegram_id,
            first_name=first_name or "",
            username=username,
        )
        await session.commit()
        return user


@router.callback_query(F.data == "my_categories")
@router.callback_query(F.data.startswith("categories_page:"))
async def cb_my_categories(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return

    page = 0
    if callback.data and callback.data.startswith("categories_page:"):
        page = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        sub = SubscriptionService(session)
        user = await sub.get_or_create_user(
            telegram_id=callback.from_user.id,
            first_name=callback.from_user.first_name or "",
            username=callback.from_user.username,
        )
        cats = await CategoryService(session).get_user_categories(user.id)
        await session.commit()

    if not cats:
        await callback.message.edit_text(
            "📂 У вас пока нет отслеживаемых категорий.\n\n"
            "Отправьте ссылку на категорию или поиск с фильтрами "
            "через «Добавить товар».",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    total_pages = max(1, math.ceil(len(cats) / CATEGORIES_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    await callback.message.edit_text(
        f"📂 <b>Мои категории</b> ({len(cats)})\n\n"
        "Выберите категорию, затем нажмите «🔔 Алерты категории».",
        reply_markup=categories_pagination_keyboard(cats, page, total_pages),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("category_detail:"))
async def cb_category_detail(callback: CallbackQuery) -> None:
    category_id = int(callback.data.split(":")[1])
    async with async_session_factory() as session:
        category = await CategoryService(session).get_category_by_id(category_id)
        if category is None or not category.is_active:
            await callback.answer("Категория не найдена", show_alert=True)
            return
        text = _category_card(category)

    await callback.message.edit_text(
        text,
        reply_markup=category_actions_keyboard(category_id),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("refresh_category:"))
async def cb_refresh_category(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    category_id = int(callback.data.split(":")[1])
    await callback.answer("Обновляю…")

    async with async_session_factory() as session:
        sub = SubscriptionService(session)
        user = await sub.get_or_create_user(
            telegram_id=callback.from_user.id,
            first_name=callback.from_user.first_name or "",
            username=callback.from_user.username,
        )
        cat_svc = CategoryService(session)
        category = await cat_svc.get_category_by_id(category_id)
        if category is None or category.user_id != user.id:
            await callback.message.edit_text(
                "❌ Категория не найдена",
                reply_markup=main_menu_keyboard(),
            )
            return
        try:
            category, _, appeared = await cat_svc.refresh_category(category)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("refresh_category_failed", category_id=category_id)
            await callback.message.edit_text(
                "❌ Не удалось обновить выдачу. Попробуйте позже.",
                reply_markup=category_actions_keyboard(category_id),
            )
            return

        text = _category_card(category)
        if appeared:
            text += f"\n\n🆕 Новых SKU: {len(appeared)}"
        else:
            text += "\n\n✅ Выдача актуальна, изменений нет"

    try:
        await callback.message.edit_text(
            text,
            reply_markup=category_actions_keyboard(category_id),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise
        await callback.answer("Без изменений", show_alert=False)


@router.callback_query(F.data.startswith("delete_category:"))
async def cb_delete_category(callback: CallbackQuery) -> None:
    category_id = int(callback.data.split(":")[1])
    await callback.message.edit_text(
        "Удалить эту категорию из отслеживания?",
        reply_markup=confirm_delete_category_keyboard(category_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_delete_category:"))
async def cb_confirm_delete_category(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    category_id = int(callback.data.split(":")[1])
    async with async_session_factory() as session:
        sub = SubscriptionService(session)
        user = await sub.get_or_create_user(
            telegram_id=callback.from_user.id,
            first_name=callback.from_user.first_name or "",
            username=callback.from_user.username,
        )
        ok = await CategoryService(session).remove_category(category_id, user.id)
        await session.commit()

    if ok:
        await callback.message.edit_text(
            "✅ Категория удалена.",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await callback.answer("Не найдено", show_alert=True)
    await callback.answer()


@router.callback_query(F.data.startswith("category_alerts:"))
async def cb_category_alerts(callback: CallbackQuery) -> None:
    category_id = int(callback.data.split(":")[1])
    await callback.message.edit_text(
        "🔔 Выберите тип алерта для категории:",
        reply_markup=category_alert_type_keyboard(category_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cat_alert_min_drop:"))
async def cb_cat_alert_min_drop(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    category_id = int(callback.data.split(":")[1])
    async with async_session_factory() as session:
        sub = SubscriptionService(session)
        user = await sub.get_or_create_user(
            telegram_id=callback.from_user.id,
            first_name=callback.from_user.first_name or "",
            username=callback.from_user.username,
        )
        await AlertService(session).create_alert_rule(
            user_id=user.id,
            rule_type=RuleType.CATEGORY_MIN_DROP,
            category_id=category_id,
            threshold_value=Decimal("0"),
        )
        await session.commit()

    await callback.message.edit_text(
        "✅ Алерт на падение мин. цены включён.",
        reply_markup=category_actions_keyboard(category_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cat_alert_new_sku:"))
async def cb_cat_alert_new_sku(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    category_id = int(callback.data.split(":")[1])
    async with async_session_factory() as session:
        sub = SubscriptionService(session)
        user = await sub.get_or_create_user(
            telegram_id=callback.from_user.id,
            first_name=callback.from_user.first_name or "",
            username=callback.from_user.username,
        )
        await AlertService(session).create_alert_rule(
            user_id=user.id,
            rule_type=RuleType.CATEGORY_NEW_SKU,
            category_id=category_id,
        )
        await session.commit()

    await callback.message.edit_text(
        "✅ Алерт на новые товары в выдаче включён.",
        reply_markup=category_actions_keyboard(category_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cat_alert_below:"))
async def cb_cat_alert_below(callback: CallbackQuery, state: FSMContext) -> None:
    category_id = int(callback.data.split(":")[1])
    await state.set_state(CategoryAlertStates.waiting_for_threshold)
    await state.update_data(category_id=category_id)
    await callback.message.edit_text(
        "✏️ Введите целевую мин. цену в рублях (например <code>45000</code>):",
        reply_markup=back_to_menu_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CategoryAlertStates.waiting_for_threshold)
async def msg_category_threshold(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text:
        return
    data = await state.get_data()
    category_id = int(data["category_id"])
    try:
        threshold = Decimal(message.text.strip().replace(" ", "").replace(",", "."))
        if threshold <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.answer("❌ Введите положительное число.")
        return

    async with async_session_factory() as session:
        sub = SubscriptionService(session)
        user = await sub.get_or_create_user(
            telegram_id=message.from_user.id,
            first_name=message.from_user.first_name or "",
            username=message.from_user.username,
        )
        await AlertService(session).create_alert_rule(
            user_id=user.id,
            rule_type=RuleType.CATEGORY_PRICE_BELOW,
            category_id=category_id,
            threshold_value=threshold,
        )
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ Алерт: уведомим, если мин. цена в выдаче ≤ {_fmt_price(threshold)}.",
        reply_markup=category_actions_keyboard(category_id),
    )
