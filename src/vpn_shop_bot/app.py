from __future__ import annotations

import asyncio
from io import BytesIO
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
import logging
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from .panel import PanelError, ProvisionedAccess, XuiPanel
from .settings import DeviceConfig, PlanConfig, ProtocolConfig, Settings, load_settings
from .store import OrderRecord, PromoGrantRecord, Store, SubscriptionRecord, utc_now

LOG = logging.getLogger(__name__)

BUY_TEXT = "Купить VPN"
PROFILE_TEXT = "Профиль"
SUPPORT_TEXT = "Поддержка"
PROMO_TEXT = "Ввести промокод"
ADMIN_TEXT = "Админ-панель"
MENU_TEXT = "Главное меню"


@dataclass(slots=True)
class Services:
    settings: Settings
    store: Store
    panel: XuiPanel
    order_locks: dict[int, asyncio.Lock] = field(default_factory=dict)


def build_application(settings: Settings | None = None) -> Application:
    settings = settings or load_settings()
    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=10.0,
        proxy=None,
        httpx_kwargs={"trust_env": False},
    )
    get_updates_request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=10.0,
        proxy=None,
        httpx_kwargs={"trust_env": False},
    )
    application = (
        ApplicationBuilder()
        .token(settings.bot.token)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )
    application.bot_data["services"] = Services(
        settings=settings,
        store=Store(settings.database.path),
        panel=XuiPanel(settings.xui, settings.sales),
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", admin_stats_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(handle_application_error)
    if application.job_queue:
        application.job_queue.run_repeating(send_expiry_reminders, interval=6 * 60 * 60, first=30)
        application.job_queue.run_repeating(cleanup_expired_accesses, interval=60 * 60, first=60)
    return application


def services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data["services"]


def order_lock(svc: Services, order_id: int) -> asyncio.Lock:
    lock = svc.order_locks.get(order_id)
    if lock is None:
        lock = asyncio.Lock()
        svc.order_locks[order_id] = lock
    return lock


async def handle_application_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_ignorable_telegram_error(context.error):
        LOG.warning("Ignored benign Telegram error. Update=%s Error=%s", update, context.error)
        return
    LOG.exception("Unhandled bot error. Update=%s", update, exc_info=context.error)
    try:
        await notify_admins_about_runtime_error(context, update, context.error)
    except Exception:
        LOG.exception("Failed to notify admins about runtime error")


def styled_inline_button(text: str, *, callback_data: str | None = None, url: str | None = None, style: str | None = None) -> InlineKeyboardButton:
    api_kwargs = {"style": style} if style and style != "default" else None
    return InlineKeyboardButton(text=text, callback_data=callback_data, url=url, api_kwargs=api_kwargs)


def styled_reply_button(text: str, *, style: str | None = None) -> KeyboardButton:
    api_kwargs = {"style": style} if style and style != "default" else None
    return KeyboardButton(text=text, api_kwargs=api_kwargs)


def is_admin_user(svc: Services, telegram_id: int | None) -> bool:
    return telegram_id in svc.settings.payments.admin_chat_ids if telegram_id is not None else False


def pending_promo_button(grant: PromoGrantRecord | None) -> str | None:
    if not grant:
        return None
    return f"🎁 Бесплатная подписка • {grant.days} дн."


def main_menu_keyboard(svc: Services, telegram_id: int | None) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = [[styled_reply_button(BUY_TEXT, style="primary")]]
    rows.append([styled_reply_button(PROFILE_TEXT), styled_reply_button(SUPPORT_TEXT)])
    promo_button = pending_promo_button(svc.store.get_pending_promo_grant(telegram_id)) if telegram_id is not None else None
    if promo_button:
        rows.append([styled_reply_button(promo_button, style="success")])
    rows.append([styled_reply_button(PROMO_TEXT, style="primary")])
    if is_admin_user(svc, telegram_id):
        rows.append([styled_reply_button(ADMIN_TEXT, style="primary")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def flow_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[styled_reply_button(MENU_TEXT, style="danger")]], resize_keyboard=True)


async def safe_answer_callback(query) -> None:
    try:
        await query.answer()
    except BadRequest as exc:
        message = str(exc).lower()
        if "query is too old" in message or "query id is invalid" in message:
            LOG.warning("Ignored stale callback query: %s", exc)
            return
        raise


async def safe_edit_message_text(query, text: str, **kwargs):
    try:
        return await query.edit_message_text(text=text, **kwargs)
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            LOG.warning("Ignored duplicate edit_message_text call: %s", exc)
            return query.message
        raise


def is_ignorable_telegram_error(error: Exception | None) -> bool:
    if error is None:
        return False
    if isinstance(error, Conflict):
        return True
    if isinstance(error, TimedOut):
        return True
    if isinstance(error, BadRequest):
        return "message is not modified" in str(error).lower()
    if isinstance(error, NetworkError):
        text = str(error).lower()
        return (
            "timed out" in text
            or "remoteprotocolerror" in text
            or "server disconnected without sending a response" in text
            or "terminated by other getupdates request" in text
        )
    return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_main_menu(update, context)


async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await show_main_menu(update, context, notice="Такой команды нет. Используй кнопки ниже.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    svc = services(context)
    user = update.effective_user
    svc.store.upsert_user(user.id, user.username, user.first_name)
    text = update.message.text
    promo_button = pending_promo_button(svc.store.get_pending_promo_grant(user.id))

    if text == MENU_TEXT:
        await handle_menu_return(update, context)
        return
    if text == BUY_TEXT:
        context.user_data.clear()
        await show_device_picker(update, context)
        return
    if text == PROFILE_TEXT:
        context.user_data.clear()
        await show_profile_message(update, context)
        return
    if text == SUPPORT_TEXT:
        context.user_data.clear()
        await show_support_topics(update, context)
        return
    if text == PROMO_TEXT:
        context.user_data.clear()
        context.user_data["flow"] = "promo_manual_entry"
        await update.message.reply_text(
            "🎟 <b>Введите ваш промокод</b>\n\nОтправьте код одним сообщением.",
            parse_mode=ParseMode.HTML,
            reply_markup=flow_keyboard(),
        )
        return
    if text == ADMIN_TEXT and is_admin_user(svc, user.id):
        context.user_data.clear()
        await show_admin_panel(update, context)
        return
    if promo_button and text == promo_button:
        context.user_data.clear()
        await start_promo_activation(update, context)
        return
    if context.user_data.get("flow") == "support_waiting_text":
        await submit_support_ticket(update, context, text)
        return
    if context.user_data.get("flow") == "admin_reply_ticket":
        await submit_admin_reply(update, context, text)
        return
    if context.user_data.get("flow") == "admin_promo_code":
        await admin_capture_promo_code(update, context, text)
        return
    if context.user_data.get("flow") == "admin_promo_limit":
        await admin_capture_promo_limit(update, context, text)
        return
    if context.user_data.get("flow") == "admin_promo_days":
        await admin_capture_promo_days(update, context, text)
        return
    if context.user_data.get("flow") == "admin_user_lookup":
        await admin_lookup_user(update, context, text)
        return
    if context.user_data.get("flow") == "admin_delete_confirm":
        await admin_confirm_delete(update, context, text)
        return
    if context.user_data.get("flow") == "promo_manual_entry":
        context.user_data.clear()
        await claim_promo_from_text(update, context, text)
        return

    promo = svc.store.get_promo_code_by_text(text)
    if promo:
        await claim_promo_from_text(update, context, promo.code)
        return

    await show_main_menu(update, context, notice="Такое сообщение я не понял. Открой главное меню.")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await safe_answer_callback(query)
    data = query.data or ""
    if data.startswith("device:"):
        await on_device_selected(update, context, data.split(":", 1)[1])
        return
    if data.startswith("plan:"):
        await on_plan_selected(update, context, data.split(":", 1)[1])
        return
    if data.startswith("protocol:"):
        await on_protocol_selected(update, context, data.split(":", 1)[1])
        return
    if data.startswith("order:review:"):
        await show_payment_screen(update, context, int(data.rsplit(":", 1)[1]))
        return
    if data.startswith("order:paid:"):
        await mark_order_paid(update, context, int(data.rsplit(":", 1)[1]))
        return
    if data.startswith("order:cancel:"):
        await cancel_order(update, context, int(data.rsplit(":", 1)[1]))
        return
    if data.startswith("admin:approve:"):
        await admin_approve_order(update, context, int(data.rsplit(":", 1)[1]))
        return
    if data.startswith("admin:reject:"):
        await admin_reject_order(update, context, int(data.rsplit(":", 1)[1]))
        return
    if data == "admin:panel":
        await show_admin_panel_callback(update, context)
        return
    if data == "admin:stats":
        await show_admin_stats(update, context)
        return
    if data == "admin:promos":
        await show_admin_promos(update, context)
        return
    if data == "admin:promo:create":
        await start_admin_promo_creation(update, context)
        return
    if data == "admin:promo:list":
        await show_promo_list(update, context)
        return
    if data == "admin:users":
        await prompt_admin_user_lookup(update, context)
        return
    if data.startswith("admin:user:view:"):
        await show_admin_user_card(update, context, int(data.rsplit(":", 1)[1]))
        return
    if data.startswith("admin:user:config:"):
        await show_admin_subscription_card(update, context, int(data.rsplit(":", 1)[1]))
        return
    if data.startswith("admin:user:delete:"):
        await prompt_admin_delete(update, context, int(data.rsplit(":", 1)[1]))
        return
    if data.startswith("admin:user:replace:"):
        await replace_subscription_for_admin(update, context, int(data.rsplit(":", 1)[1]))
        return
    if data == "profile:history":
        await show_history(update, context)
        return
    if data == "profile:back":
        await show_profile_callback(update, context)
        return
    if data.startswith("profile:config:"):
        await show_saved_config(update, context, int(data.rsplit(":", 1)[1]))
        return
    if data.startswith("profile:renew:"):
        await begin_renewal(update, context, int(data.rsplit(":", 1)[1]))
        return
    if data.startswith("renew:plan:"):
        await create_renewal_order(update, context, data.split(":", 2)[2])
        return
    if data.startswith("support:topic:"):
        await on_support_topic(update, context, data.split(":", 2)[2])
        return
    if data.startswith("admin:reply_ticket:"):
        await prompt_admin_reply(update, context, int(data.rsplit(":", 1)[1]))
        return
    if data == "menu:home":
        await show_main_menu(update, context)


async def show_main_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
    force_full: bool = False,
) -> None:
    svc = services(context)
    user = update.effective_user
    existing_user = svc.store.get_user(user.id) if user else None
    if user:
        svc.store.upsert_user(user.id, user.username, user.first_name)
    context.user_data.clear()
    full_text = (
        f"{svc.settings.branding.welcome_title}\n\n"
        f"{svc.settings.branding.welcome_text}\n\n"
        "👇 Нажми кнопку ниже и получи VPN прямо сейчас."
    )
    short_text = (
        "🔥 <b>VPN Халява</b>\n\n"
        "Интернет без блокировок за копейки.\n"
        "Подключение за 1–2 минуты.\n\n"
        "Выбери действие ниже."
    )
    text = full_text if force_full or existing_user is None else short_text
    if notice:
        text = f"{notice}\n\n{text}"
    if svc.settings.start_image_file.exists():
        await send_main_menu_photo(update, context, text)
        return
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=main_menu_keyboard(svc, user.id if user else None),
            parse_mode=ParseMode.HTML,
        )
    elif update.callback_query:
        await update.callback_query.message.reply_text(
            text,
            reply_markup=main_menu_keyboard(svc, user.id if user else None),
            parse_mode=ParseMode.HTML,
        )


async def send_main_menu_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    caption: str,
) -> None:
    svc = services(context)
    user = update.effective_user
    reply_markup = main_menu_keyboard(svc, user.id if user else None)
    cached_file_id = context.application.bot_data.get("start_image_file_id")
    sender = update.message or (update.callback_query.message if update.callback_query else None)
    if sender is None:
        return
    if cached_file_id:
        sent_message = await sender.reply_photo(
            photo=cached_file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
        cache_photo_file_id(context, sent_message)
        return
    with svc.settings.start_image_file.open("rb") as image:
        sent_message = await sender.reply_photo(
            photo=image,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    cache_photo_file_id(context, sent_message)


def cache_photo_file_id(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    if not message or not getattr(message, "photo", None):
        return
    context.application.bot_data["start_image_file_id"] = message.photo[-1].file_id


async def handle_menu_return(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    order_id = context.user_data.get("active_order_id")
    if order_id:
        await cancel_order(update, context, int(order_id), announce=True)
        return
    await show_main_menu(update, context)


async def show_device_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    keyboard = [
        [styled_inline_button(device.title, callback_data=f"device:{device.key}", style="primary")]
        for device in svc.settings.devices.values()
    ]
    await update.effective_message.reply_text(
        "🧩 <b>Наш VPN для вашего устройства</b>\n\nВыберите, где хотите пользоваться VPN:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )
    await update.effective_message.reply_text("Для отмены оформления нажми кнопку ниже.", reply_markup=flow_keyboard())


async def on_device_selected(update: Update, context: ContextTypes.DEFAULT_TYPE, device_key: str) -> None:
    svc = services(context)
    device = svc.settings.devices[device_key]
    context.user_data["device_key"] = device.key
    if context.user_data.get("promo_grant_id"):
        if device.requires_protocol:
            await show_protocol_picker(update, context, device)
            return
        await create_promo_activation(update, context, svc.settings.sales.default_protocol)
        return
    buttons = [
        [
            styled_inline_button(
                f"{plan.title} • {plan.price} ₽",
                callback_data=f"plan:{plan.key}",
                style=plan.button_style,
            )
        ]
        for plan in svc.settings.plans.values()
    ]
    await safe_edit_message_text(
        update.callback_query,
        f"{device.title}\n{device.description}\n\n💳 <b>Выберите срок подписки</b>:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def on_plan_selected(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_key: str) -> None:
    svc = services(context)
    device_key = context.user_data.get("device_key")
    if not device_key:
        await safe_edit_message_text(update.callback_query, "Сессия сброшена. Начни покупку заново.")
        return
    device = svc.settings.devices[device_key]
    context.user_data["plan_key"] = plan_key
    if device.requires_protocol:
        await show_protocol_picker(update, context, device)
        return
    await create_order_from_selection(update, context, svc.settings.sales.default_protocol)


async def show_protocol_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, device: DeviceConfig) -> None:
    svc = services(context)
    allowed = device.allowed_protocols or list(svc.settings.protocols.keys())
    buttons = []
    for protocol_key in allowed:
        protocol = svc.settings.protocols.get(protocol_key)
        if protocol and protocol.enabled:
            buttons.append([styled_inline_button(protocol.title, callback_data=f"protocol:{protocol.key}", style="primary")])
    await safe_edit_message_text(
        update.callback_query,
        f"{device.title}\n\n🌐 <b>Выберите протокол для роутера</b>:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def on_protocol_selected(update: Update, context: ContextTypes.DEFAULT_TYPE, protocol_key: str) -> None:
    if context.user_data.get("promo_grant_id"):
        await create_promo_activation(update, context, protocol_key)
        return
    await create_order_from_selection(update, context, protocol_key)


async def create_order_from_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, protocol_key: str) -> None:
    svc = services(context)
    user = update.effective_user
    if not user:
        return
    device = svc.settings.devices[context.user_data["device_key"]]
    plan = svc.settings.plans[context.user_data["plan_key"]]
    protocol = svc.settings.protocols[protocol_key]
    source_subscription_order_id = context.user_data.get("renew_subscription_order_id")
    source_subscription = (
        svc.store.get_subscription_by_order_id(int(source_subscription_order_id))
        if source_subscription_order_id
        else None
    )
    order = svc.store.create_order(
        order_code=build_order_code(user.id),
        telegram_id=user.id,
        device_key=device.key,
        period_key=plan.key,
        protocol_key=protocol.key,
        amount_rub=plan.price,
        payment_url=svc.settings.payments.payment_url,
        payment_type="paid",
        source_subscription_order_id=int(source_subscription_order_id) if source_subscription_order_id else None,
    )
    if source_subscription:
        svc.store.update_order_xui(
            order.id,
            xui_client_id=source_subscription.xui_client_id,
            xui_email=source_subscription.xui_email,
            xui_sub_id=source_subscription.xui_sub_id,
            subscription_url=source_subscription.subscription_url,
            config_text=source_subscription.config_text,
            inbound_id=None,
        )
        order = svc.store.get_order(order.id) or order
    context.user_data["active_order_id"] = order.id
    if svc.settings.sales.precreate_client_on_order:
        try:
            await ensure_order_access(
                order,
                device,
                plan,
                protocol,
                user.id,
                user.username,
                svc,
                enabled=False,
                base_ends_at=source_subscription.ends_at if source_subscription else None,
            )
            order = svc.store.get_order(order.id) or order
        except PanelError as exc:
            svc.store.set_order_status(order.id, "provision_error")
            await notify_admins_about_panel_error(
                context,
                stage="Подготовка доступа при создании заказа",
                order=order,
                user_id=user.id,
                username=user.username,
                error=exc,
            )
            await update.callback_query.edit_message_text(friendly_provision_error_text(), parse_mode=ParseMode.HTML)
            return
    await update.callback_query.edit_message_text(
        render_order_summary(order, device, plan, protocol),
        reply_markup=InlineKeyboardMarkup(
            [
                [styled_inline_button("Перейти к оплате", callback_data=f"order:review:{order.id}", style="primary")],
                [styled_inline_button("Отменить заказ", callback_data=f"order:cancel:{order.id}", style="danger")],
            ]
        ),
        parse_mode=ParseMode.HTML,
    )


async def start_promo_activation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user = update.effective_user
    if not user:
        return
    grant = svc.store.get_pending_promo_grant(user.id)
    if not grant:
        await show_main_menu(update, context, notice="Активных бесплатных подписок пока нет.")
        return
    context.user_data["promo_grant_id"] = grant.id
    context.user_data["promo_days"] = grant.days
    await show_device_picker(update, context)


async def create_promo_activation(update: Update, context: ContextTypes.DEFAULT_TYPE, protocol_key: str) -> None:
    svc = services(context)
    user = update.effective_user
    if not user:
        return
    grant_id = context.user_data.get("promo_grant_id")
    promo_days = context.user_data.get("promo_days")
    if not grant_id or not promo_days:
        await update.callback_query.edit_message_text("Бесплатная подписка не найдена. Введите промокод заново.")
        return
    device = svc.settings.devices[context.user_data["device_key"]]
    protocol = svc.settings.protocols[protocol_key]
    promo_period_key = f"promo_{int(promo_days)}d"
    promo_plan = PlanConfig(
        key=promo_period_key,
        title=f"🎁 {int(promo_days)} дн.",
        months=0,
        price=0,
        badge="Подарок по промокоду",
        button_style="success",
    )
    order = svc.store.create_order(
        order_code=build_order_code(user.id),
        telegram_id=user.id,
        device_key=device.key,
        period_key=promo_period_key,
        protocol_key=protocol.key,
        amount_rub=0,
        payment_url="",
        payment_type="promo",
        promo_grant_id=int(grant_id),
    )
    context.user_data["active_order_id"] = order.id
    try:
        await fulfill_order(context, order, user.id, duration_days=int(promo_days))
    except PanelError as exc:
        svc.store.set_order_status(order.id, "provision_error")
        await notify_admins_about_panel_error(
            context,
            stage="Выдача бесплатной подписки",
            order=order,
            user_id=user.id,
            username=user.username,
            error=exc,
        )
        await update.callback_query.edit_message_text(friendly_provision_error_text(), parse_mode=ParseMode.HTML)
        return
    svc.store.activate_promo_grant(int(grant_id), order.id)
    context.user_data.clear()
    await update.callback_query.edit_message_text(
        "🎁 <b>Бесплатная подписка активирована</b>\n\nДоступ уже отправлен в этот чат.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[styled_inline_button("К профилю", callback_data="profile:back", style="primary")]]),
    )
    await context.bot.send_message(
        chat_id=user.id,
        text="Главное меню обновлено.",
        reply_markup=main_menu_keyboard(svc, user.id),
    )


def render_order_summary(order: OrderRecord, device: DeviceConfig, plan: PlanConfig, protocol: ProtocolConfig) -> str:
    badge = f"• <b>Тариф:</b> {plan.badge}\n" if plan.badge else ""
    return (
        "🧾 <b>Заказ готов к оплате</b>\n\n"
        f"• <b>Заказ:</b> #{order.id}\n"
        f"• <b>Код заказа:</b> <code>{order.order_code}</code>\n"
        f"• <b>Устройство:</b> {device.title}\n"
        f"• <b>Срок:</b> {plan.title}\n"
        f"• <b>Протокол:</b> {protocol.title}\n"
        f"{badge}"
        f"• <b>Сумма к оплате:</b> {plan.price} ₽\n\n"
        "Проверь данные заказа и нажми кнопку ниже, чтобы перейти к оплате."
    )


def friendly_provision_error_text() -> str:
    return (
        "⚠️ <b>Что-то пошло не так</b>\n\n"
        "Не удалось подготовить доступ прямо сейчас.\n"
        "Попробуй ещё раз немного позже.\n\n"
        "Если ошибка повторится, подожди чуть-чуть и повтори попытку."
    )


async def notify_admins_about_panel_error(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    stage: str,
    order: OrderRecord | None,
    user_id: int | None,
    username: str | None,
    error: Exception,
) -> None:
    svc = services(context)
    if not svc.settings.payments.admin_chat_ids:
        return
    username_text = f"@{username}" if username else "без username"
    order_text = f"#{order.id} / {order.order_code}" if order else "без заказа"
    lines = [
        "🚨 <b>Ошибка выдачи 3x-ui</b>",
        "",
        f"• <b>Этап:</b> {stage}",
        f"• <b>Пользователь:</b> {username_text}",
        f"• <b>Telegram ID:</b> <code>{user_id or 0}</code>",
        f"• <b>Заказ:</b> <code>{order_text}</code>",
        "",
        f"<code>{escape(str(error))}</code>",
    ]
    for admin_id in svc.settings.payments.admin_chat_ids:
        await context.bot.send_message(chat_id=admin_id, text="\n".join(lines), parse_mode=ParseMode.HTML)


async def notify_admins_about_runtime_error(
    context: ContextTypes.DEFAULT_TYPE,
    update: object,
    error: Exception | None,
) -> None:
    svc = services(context)
    if not svc.settings.payments.admin_chat_ids or error is None:
        return
    update_kind = update.__class__.__name__ if update is not None else "None"
    text = (
        "🚨 <b>Непойманная ошибка бота</b>\n\n"
        f"• <b>Тип update:</b> {update_kind}\n\n"
        f"<code>{escape(str(error))}</code>"
    )
    for admin_id in svc.settings.payments.admin_chat_ids:
        await context.bot.send_message(chat_id=admin_id, text=text, parse_mode=ParseMode.HTML)


async def notify_user_about_expired_access(
    context: ContextTypes.DEFAULT_TYPE,
    subscription: SubscriptionRecord,
) -> None:
    svc = services(context)
    try:
        await context.bot.send_message(
            chat_id=subscription.telegram_id,
            text=(
                "⛔ <b>Срок подписки закончился</b>\n\n"
                f"• <b>Подписка:</b> {profile_summary_title(subscription)}\n"
                "Доступ отключён.\n\n"
                "Если хочешь продолжить пользоваться VPN, продли подписку в профиле."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(svc, subscription.telegram_id),
        )
    except Exception:
        LOG.exception("Failed to notify user %s about expired access", subscription.telegram_id)


def instruction_url_for_device(svc: Services, device_key: str) -> str:
    if device_key == "smarttv":
        return svc.settings.instructions.smarttv_url or svc.settings.instructions.common_url
    if device_key == "router":
        return svc.settings.instructions.router_url
    return svc.settings.instructions.common_url


def smarttv_help_url(svc: Services) -> str:
    if svc.settings.instructions.smarttv_help_url:
        return svc.settings.instructions.smarttv_help_url
    return build_prefilled_support_url(
        svc.settings.branding.support_url,
        "Здравствуйте! Мне нужна помощь с подключением VPN к Smart TV.",
    )


def build_prefilled_support_url(base_url: str, message: str) -> str:
    url = (base_url or "").strip()
    if not url:
        return ""
    if "t.me/" in url:
        username = url.split("t.me/", 1)[1].split("?", 1)[0].strip("/ ")
        if username:
            return f"https://t.me/{username}?text={quote(message)}&profile"
    return url


def access_buttons(
    svc: Services,
    *,
    device_key: str,
    device_title: str,
    subscription_url: str,
    renew_order_id: int,
) -> InlineKeyboardMarkup:
    rows = [[styled_inline_button(f"Открыть подписку • {device_title}", url=subscription_url)]]
    instruction_url = instruction_url_for_device(svc, device_key)
    if instruction_url:
        rows.append([styled_inline_button("Открыть инструкцию", url=instruction_url)])
    if device_key == "smarttv":
        help_url = smarttv_help_url(svc)
        if help_url:
            rows.append([styled_inline_button("Помощь с подключением", url=help_url)])
    rows.append([styled_inline_button(f"Продлить подписку • {device_title}", callback_data=f"profile:renew:{renew_order_id}", style="success")])
    rows.append([styled_inline_button("К профилю", callback_data="profile:back", style="primary")])
    return InlineKeyboardMarkup(rows)


def safe_filename_part(value: str | None) -> str:
    if not value:
        return "user"
    cleaned = "".join(ch.lower() if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip())
    cleaned = cleaned.strip("-_")
    return cleaned or "user"


def wireguard_filename(*, telegram_id: int, username: str | None, device_key: str) -> str:
    username_part = safe_filename_part(username)
    device_part = safe_filename_part(device_key)
    return f"halyava-vpn-{username_part}-{telegram_id}-{device_part}-wireguard.conf"


async def send_wireguard_conf_file(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    telegram_id: int,
    username: str | None,
    device_key: str,
    config_text: str,
) -> None:
    payload = BytesIO(config_text.encode("utf-8"))
    filename = wireguard_filename(telegram_id=telegram_id, username=username, device_key=device_key)
    payload.name = filename
    await context.bot.send_document(
        chat_id=telegram_id,
        document=InputFile(payload, filename=filename),
        caption="📄 Конфиг WireGuard в формате .conf",
    )


async def show_payment_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    svc = services(context)
    order = svc.store.get_order(order_id)
    if not order:
        await safe_edit_message_text(update.callback_query, "Заказ не найден.")
        return
    await safe_edit_message_text(
        update.callback_query,
        "💸 <b>Оплата заказа</b>\n\n"
        f"• <b>Сумма перевода:</b> {order.amount_rub} ₽\n"
        "• Нажми кнопку ниже и выполни перевод\n"
        "• После оплаты вернись в бот и нажми <b>Я оплатил</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [styled_inline_button(svc.settings.payments.payment_label, url=order.payment_url, style="success")],
                [styled_inline_button("Я оплатил", callback_data=f"order:paid:{order.id}", style="success")],
                [styled_inline_button("Отменить заказ", callback_data=f"order:cancel:{order.id}", style="danger")],
            ]
        ),
    )


async def mark_order_paid(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    svc = services(context)
    user = update.effective_user
    if not user:
        return
    lock = order_lock(svc, order_id)
    async with lock:
        order = svc.store.get_order(order_id)
        if not order:
            await safe_edit_message_text(update.callback_query, "Заказ не найден.")
            return
        if order.status == "cancelled":
            await safe_edit_message_text(update.callback_query, "Этот заказ уже отменён.")
            return
        if order.status in {"awaiting_confirmation", "fulfilling"}:
            await safe_edit_message_text(
                update.callback_query,
                "⏳ Этот заказ уже отправлен на проверку. После подтверждения доступ придёт сюда автоматически.",
            )
            return
        if order.status in {"fulfilled", "approved"}:
            await safe_edit_message_text(update.callback_query, "✅ Оплата уже подтверждена. Доступ отправлен.")
            return
        if not svc.settings.payments.admin_chat_ids and not svc.settings.payments.auto_approve_manual_payments:
            await safe_edit_message_text(update.callback_query, "Не настроены администраторы для проверки оплаты.")
            return
        if svc.settings.payments.auto_approve_manual_payments:
            await safe_edit_message_text(update.callback_query, "⏳ Подтверждаю оплату и готовлю доступ...")
            try:
                await deliver_paid_order(context, order, user.id)
            except PanelError as exc:
                LOG.exception("Failed to auto-approve order %s", order.id)
                await notify_admins_about_panel_error(
                    context,
                    stage="Автоподтверждение оплаты",
                    order=order,
                    user_id=user.id,
                    username=user.username,
                    error=exc,
                )
                await safe_edit_message_text(update.callback_query, friendly_provision_error_text(), parse_mode=ParseMode.HTML)
                return
            except (TimedOut, NetworkError):
                latest_order = svc.store.get_order(order.id)
                if latest_order and latest_order.status == "fulfilled":
                    await safe_edit_message_text(update.callback_query, "✅ Оплата подтверждена. Доступ обрабатывается или уже отправлен.")
                    return
                raise
            await safe_edit_message_text(update.callback_query, "✅ Оплата подтверждена. Конфиг уже отправлен.")
            return
        svc.store.set_order_status(order.id, "awaiting_confirmation")
        await notify_admins_about_payment(update, context, order)
        await safe_edit_message_text(
            update.callback_query,
            "⏳ Платёж отправлен на проверку администратору.\nПосле подтверждения доступ придёт сюда автоматически.",
        )


async def notify_admins_about_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, order: OrderRecord) -> None:
    svc = services(context)
    user = update.effective_user
    username = f"@{user.username}" if user and user.username else "без username"
    for admin_id in svc.settings.payments.admin_chat_ids:
        await context.bot.send_message(
            chat_id=admin_id,
            text=(
                "💳 <b>Новая заявка на подтверждение оплаты</b>\n\n"
                f"Пользователь: {username}\n"
                f"Telegram ID: <code>{order.telegram_id}</code>\n"
                f"Заказ: <code>{order.order_code}</code>\n"
                f"Сумма: <b>{order.amount_rub} ₽</b>\n"
                f"Время отметки: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [styled_inline_button("Подтвердить", callback_data=f"admin:approve:{order.id}", style="success")],
                    [styled_inline_button("Отклонить", callback_data=f"admin:reject:{order.id}", style="danger")],
                ]
            ),
        )


async def admin_approve_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    svc = services(context)
    if update.effective_user is None or update.effective_user.id not in svc.settings.payments.admin_chat_ids:
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    lock = order_lock(svc, order_id)
    async with lock:
        order = svc.store.get_order(order_id)
        if not order:
            await safe_edit_message_text(update.callback_query, "Заказ не найден.")
            return
        if order.status == "cancelled":
            await safe_edit_message_text(update.callback_query, "Заказ отменён пользователем. Подтверждение больше недоступно.")
            return
        if order.status in {"fulfilled", "approved"}:
            await safe_edit_message_text(update.callback_query, "✅ Этот заказ уже подтверждён. Доступ уже был отправлен.")
            return
        if order.status == "fulfilling":
            await safe_edit_message_text(update.callback_query, "⏳ Этот заказ уже подтверждается. Подождите несколько секунд.")
            return
        svc.store.set_order_status(order.id, "fulfilling")
        await safe_edit_message_text(update.callback_query, "⏳ Подтверждаю оплату и выдаю доступ...")
        try:
            await deliver_paid_order(context, order, order.telegram_id)
        except PanelError as exc:
            svc.store.set_order_status(order.id, "awaiting_confirmation")
            LOG.exception("Failed to approve order %s", order.id)
            target_user = svc.store.get_user(order.telegram_id) or {}
            await notify_admins_about_panel_error(
                context,
                stage="Подтверждение оплаты администратором",
                order=order,
                user_id=order.telegram_id,
                username=target_user.get("username"),
                error=exc,
            )
            await safe_edit_message_text(update.callback_query, "⚠️ Ошибка при выдаче доступа. Подробности отправлены администратору.")
            return
        except (TimedOut, NetworkError):
            latest_order = svc.store.get_order(order.id)
            if latest_order and latest_order.status == "fulfilled":
                await safe_edit_message_text(
                    update.callback_query,
                    "✅ Оплата подтверждена. Выдача уже обработана. Если пользователь не увидел сообщение сразу, пусть откроет профиль.",
                )
                return
            svc.store.set_order_status(order.id, "awaiting_confirmation")
            raise
        await safe_edit_message_text(update.callback_query, "✅ Оплата подтверждена. Пользователю отправлен доступ.")


async def admin_reject_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    svc = services(context)
    if update.effective_user is None or update.effective_user.id not in svc.settings.payments.admin_chat_ids:
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    order = svc.store.get_order(order_id)
    if not order:
        await safe_edit_message_text(update.callback_query, "Заказ не найден.")
        return
    if order.status in {"fulfilled", "approved"}:
        await safe_edit_message_text(update.callback_query, "Заказ уже подтверждён, отклонение больше недоступно.")
        return
    svc.store.set_order_status(order.id, "payment_rejected")
    await context.bot.send_message(
        chat_id=order.telegram_id,
        text="❌ Оплата пока не подтверждена. Если это ошибка, напишите в поддержку.",
        reply_markup=main_menu_keyboard(svc, order.telegram_id),
    )
    await safe_edit_message_text(update.callback_query, "❌ Оплата отклонена. Пользователь уведомлён.")


async def deliver_paid_order(context: ContextTypes.DEFAULT_TYPE, order: OrderRecord, user_id: int) -> None:
    svc = services(context)
    latest_order = svc.store.get_order(order.id)
    if latest_order and latest_order.status in {"fulfilled", "approved"}:
        return
    await fulfill_order(context, order, user_id)


async def fulfill_order(
    context: ContextTypes.DEFAULT_TYPE,
    order: OrderRecord,
    user_id: int,
    *,
    duration_days: int | None = None,
) -> None:
    svc = services(context)
    device = svc.settings.devices[order.device_key]
    plan = plan_for_order(order, svc.settings)
    protocol = svc.settings.protocols[order.protocol_key]
    source_subscription = (
        svc.store.get_subscription_by_order_id(order.source_subscription_order_id)
        if order.source_subscription_order_id
        else None
    )
    user_info = svc.store.get_user(user_id) or {}
    access = await ensure_order_access(
        order,
        device,
        plan,
        protocol,
        user_id,
        user_info.get("username"),
        svc,
        enabled=True,
        duration_days=duration_days,
        base_ends_at=source_subscription.ends_at if source_subscription else None,
    )
    svc.store.set_order_status(order.id, "fulfilled")
    subscription_order_id = source_subscription.order_id if source_subscription else order.id
    starts_at, ends_at = subscription_dates(
        plan.months,
        svc.settings.bot.timezone,
        duration_days=duration_days,
        base_ends_at=source_subscription.ends_at if source_subscription else None,
    )
    svc.store.create_subscription(
        telegram_id=user_id,
        order_id=subscription_order_id,
        device_key=device.key,
        period_key=plan.key,
        protocol_key=protocol.key,
        title=build_subscription_title(device, protocol),
        status="active",
        payment_type=order.payment_type,
        starts_at=starts_at,
        ends_at=ends_at,
        amount_rub=plan.price,
        xui_client_id=access.client_id,
        xui_email=access.email,
        xui_sub_id=access.sub_id,
        subscription_url=access.subscription_url,
        config_text=access.config_text,
        reminder_sent_at=None,
    )
    await context.bot.send_message(
        chat_id=user_id,
        text=render_access_message(
            access,
            device.title,
            ends_at,
            order_id=order.id,
            payment_type=order.payment_type,
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=access_buttons(
            svc,
            device_key=device.key,
            device_title=device.title,
            subscription_url=access.subscription_url,
            renew_order_id=subscription_order_id,
        ),
    )
    if protocol.key == "wireguard":
        await send_wireguard_conf_file(
            context,
            telegram_id=user_id,
            username=user_info.get("username"),
            device_key=device.key,
            config_text=access.config_text,
        )


def render_access_message(
    access: ProvisionedAccess,
    device_title: str,
    ends_at: str,
    *,
    order_id: int,
    payment_type: str = "paid",
) -> str:
    title = "✅ <b>Оплата подтверждена</b>" if payment_type == "paid" else "🎁 <b>Бесплатная подписка активирована</b>"
    return (
        f"{title}\n\n"
        f"• <b>Заказ:</b> #{order_id}\n"
        f"• <b>Устройство:</b> {device_title}\n"
        f"• <b>Доступ активен до:</b> {format_datetime_human(ends_at)}\n\n"
        "Чтобы добавить VPN как полноценную подписку с автообновлением, используй кнопку <b>Открыть подписку</b> ниже.\n"
        "Если вставить конфиг вручную, приложение создаст обычный локальный профиль.\n\n"
        + (
            "📄 <b>Файл .conf отправлен отдельным сообщением.</b>"
            if access.protocol_key == "wireguard"
            else f"⚙️ <b>Конфиг для копирования:</b>\n<code>{access.config_text}</code>"
        )
    )


async def ensure_order_access(
    order: OrderRecord,
    device: DeviceConfig,
    plan: PlanConfig,
    protocol: ProtocolConfig,
    telegram_id: int,
    username: str | None,
    svc: Services,
    *,
    enabled: bool,
    duration_days: int | None = None,
    base_ends_at: str | None = None,
) -> ProvisionedAccess:
    if order.xui_client_id and order.xui_email and order.xui_sub_id:
        access = svc.panel.update_client(
            protocol=protocol,
            device=device,
            plan=plan,
            telegram_id=telegram_id,
            username=username,
            order_code=order.order_code,
            enabled=enabled,
            client_id=order.xui_client_id,
            email=order.xui_email,
            sub_id=order.xui_sub_id,
            duration_days=duration_days,
            base_ends_at=base_ends_at,
        )
    else:
        access = svc.panel.add_client(
            protocol=protocol,
            device=device,
            plan=plan,
            telegram_id=telegram_id,
            username=username,
            order_code=order.order_code,
            enabled=enabled,
            duration_days=duration_days,
            base_ends_at=base_ends_at,
        )
    svc.store.update_order_xui(
        order.id,
        xui_client_id=access.client_id,
        xui_email=access.email,
        xui_sub_id=access.sub_id,
        subscription_url=access.subscription_url,
        config_text=access.config_text,
        inbound_id=access.inbound_id,
    )
    return access


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int, announce: bool = False) -> None:
    svc = services(context)
    order = svc.store.get_order(order_id)
    context.user_data.clear()
    if not order:
        if announce and update.effective_message:
            await update.effective_message.reply_text("Заказ уже неактивен.", reply_markup=main_menu_keyboard(svc, update.effective_user.id if update.effective_user else None))
        return
    if order.xui_client_id and order.inbound_id and order.status not in {"fulfilled", "approved"}:
        try:
            svc.panel.delete_client(inbound_id=order.inbound_id, client_id=order.xui_client_id)
        except PanelError:
            LOG.exception("Failed to delete draft client %s", order.xui_client_id)
    svc.store.set_order_status(order.id, "cancelled")
    if update.callback_query:
        await update.callback_query.edit_message_text("❌ Заказ отменён.")
    if announce or update.callback_query:
        await show_main_menu(update, context, notice="Заказ отменён.")


async def show_profile_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user = update.effective_user
    if not user:
        return
    subscriptions = svc.store.list_subscriptions(user.id)
    active = [item for item in subscriptions if item.status == "active" and remaining_days(item.ends_at) >= 0]
    lines = [
        "👤 <b>Ваш профиль</b>",
        "",
        f"• <b>Пользователь:</b> @{user.username}" if user.username else f"• <b>Пользователь:</b> {user.first_name}",
        f"• <b>ID:</b> <code>{user.id}</code>",
        f"• <b>Активных подписок:</b> {len(active)}",
    ]
    if active:
        lines.append("")
        for item in active:
            lines.append(f"• {profile_summary_title(item)} — осталось {format_remaining(item.ends_at)}")
    else:
        lines.append("Активных подписок пока нет.")
    keyboard = [[styled_inline_button("История оплат", callback_data="profile:history", style="primary")]]
    for item in active[:5]:
        keyboard.append([styled_inline_button(profile_button_title(item), callback_data=f"profile:config:{item.order_id}", style="success")])
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user = update.effective_user
    if not user:
        return
    subscriptions = svc.store.list_subscriptions(user.id)
    active = [item for item in subscriptions if item.status == "active" and remaining_days(item.ends_at) >= 0]
    lines = [
        "👤 <b>Ваш профиль</b>",
        "",
        f"• <b>Пользователь:</b> @{user.username}" if user.username else f"• <b>Пользователь:</b> {user.first_name}",
        f"• <b>ID:</b> <code>{user.id}</code>",
        f"• <b>Активных подписок:</b> {len(active)}",
    ]
    if active:
        lines.append("")
        for item in active:
            lines.append(f"• {profile_summary_title(item)} — осталось {format_remaining(item.ends_at)}")
    else:
        lines.append("Активных подписок пока нет.")
    keyboard = [[styled_inline_button("История оплат", callback_data="profile:history", style="primary")]]
    for item in active[:5]:
        keyboard.append([styled_inline_button(profile_button_title(item), callback_data=f"profile:config:{item.order_id}", style="success")])
    keyboard.append([styled_inline_button("Назад в меню", callback_data="menu:home", style="danger")])
    await update.callback_query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user = update.effective_user
    if not user:
        return
    orders = svc.store.list_paid_orders(user.id)
    if not orders:
        text = "🧾 История оплат пока пустая."
    else:
        lines = ["🧾 <b>История оплат</b>", ""]
        for order in orders[:10]:
            protocol = svc.settings.protocols[order.protocol_key]
            plan = svc.settings.plans[order.period_key]
            device = svc.settings.devices[order.device_key]
            lines.append(f"• #{order.id} — <code>{order.order_code}</code> — {device.title} • {plan.title} • {protocol.title} — {order.amount_rub} ₽")
        text = "\n".join(lines)
    await update.callback_query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[styled_inline_button("К профилю", callback_data="profile:back", style="primary")]]),
    )


async def show_saved_config(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    svc = services(context)
    order = svc.store.get_order(order_id)
    subscription = svc.store.get_subscription_by_order_id(order_id)
    if not order:
        await update.callback_query.answer("Конфиг не найден.", show_alert=True)
        return
    subscription_url = subscription.subscription_url if subscription and subscription.subscription_url else order.subscription_url
    config_text = subscription.config_text if subscription and subscription.config_text else order.config_text
    title = (
        profile_summary_title(subscription)
        if subscription
        else svc.settings.devices.get(order.device_key, DeviceConfig(order.device_key, order.device_key, "")).title
    )
    lines = [
        "🔐 <b>Ваш доступ</b>",
        "",
        f"• <b>Заказ:</b> #{order.id}",
        f"• <b>Подписка:</b> {title}",
    ]
    if subscription:
        lines.append(f"• <b>Осталось:</b> {format_remaining(subscription.ends_at)}")
        lines.append(f"• <b>Активна до:</b> {format_datetime_human(subscription.ends_at)}")
    lines.extend(
        [
            "",
            "Для полноценной подписки с автообновлением используй кнопку <b>Открыть подписку</b> ниже.",
            "Если вставить конфиг вручную, приложение создаст локальный профиль.",
        ]
    )
    if order.protocol_key == "wireguard":
        lines.extend(["", "📄 <b>Файл .conf будет отправлен отдельным сообщением.</b>"])
    else:
        lines.extend(["", "⚙️ <b>Конфиг для копирования:</b>", f"<code>{config_text or 'не настроен'}</code>"])
    buttons = []
    if subscription_url:
        buttons.append([styled_inline_button("Открыть подписку", url=subscription_url)])
    instruction_url = instruction_url_for_device(svc, order.device_key)
    if instruction_url:
        buttons.append([styled_inline_button("Открыть инструкцию", url=instruction_url)])
    if order.device_key == "smarttv":
        help_url = smarttv_help_url(svc)
        if help_url:
            buttons.append([styled_inline_button("Помощь с подключением", url=help_url)])
    buttons.extend(
        [
            [styled_inline_button("Продлить подписку", callback_data=f"profile:renew:{order_id}", style="success")],
            [styled_inline_button("К профилю", callback_data="profile:back", style="primary")],
        ]
    )
    await update.callback_query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )
    if order.protocol_key == "wireguard" and config_text:
        user_row = svc.store.get_user(order.telegram_id) or {}
        await send_wireguard_conf_file(
            context,
            telegram_id=order.telegram_id,
            username=user_row.get("username"),
            device_key=order.device_key,
            config_text=config_text,
        )


async def begin_renewal(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    svc = services(context)
    subscription = svc.store.get_subscription_by_order_id(order_id)
    if not subscription:
        await update.callback_query.answer("Подписка не найдена", show_alert=True)
        return
    context.user_data["renew_subscription_order_id"] = order_id
    context.user_data["device_key"] = subscription.device_key
    context.user_data["plan_key"] = subscription.period_key
    context.user_data["renew_protocol_key"] = subscription.protocol_key
    buttons = [
        [styled_inline_button(f"{plan.title} • {plan.price} ₽", callback_data=f"renew:plan:{plan.key}", style=plan.button_style)]
        for plan in svc.settings.plans.values()
    ]
    buttons.append([styled_inline_button("К профилю", callback_data="profile:back", style="danger")])
    await update.callback_query.edit_message_text(
        "♻️ <b>Продление подписки</b>\n\n"
        f"• <b>Подписка:</b> {profile_summary_title(subscription)}\n"
        f"• <b>Осталось:</b> {format_remaining(subscription.ends_at)}\n"
        f"• <b>Активна до:</b> {format_datetime_human(subscription.ends_at)}\n\n"
        "Выберите срок продления:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def create_renewal_order(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_key: str) -> None:
    context.user_data["plan_key"] = plan_key
    protocol_key = context.user_data.get("renew_protocol_key")
    if not protocol_key:
        await update.callback_query.answer("Сессия продления сброшена", show_alert=True)
        return
    await create_order_from_selection(update, context, protocol_key)


def profile_button_title(item: SubscriptionRecord) -> str:
    return f"Получить подписку • {profile_summary_title(item)}"


def profile_summary_title(item: SubscriptionRecord) -> str:
    return item.title


def renewal_message(subscription: SubscriptionRecord) -> str:
    return (
        "⏳ <b>Подписка скоро закончится</b>\n\n"
        f"• <b>Подписка:</b> {profile_summary_title(subscription)}\n"
        f"• <b>Осталось:</b> {format_remaining(subscription.ends_at)}\n"
        f"• <b>Активна до:</b> {format_datetime_human(subscription.ends_at)}\n\n"
        "Можно продлить доступ прямо сейчас."
    )


async def send_expiry_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    for subscription in svc.store.list_all_subscriptions():
        if subscription.status != "active":
            continue
        days_left = remaining_days(subscription.ends_at)
        if days_left < 0 or days_left > 3:
            continue
        if subscription.reminder_sent_at:
            continue
        await context.bot.send_message(
            chat_id=subscription.telegram_id,
            text=renewal_message(subscription),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [styled_inline_button("Продлить подписку", callback_data=f"profile:renew:{subscription.order_id}", style="success")],
                    [styled_inline_button("Позже", callback_data="menu:home", style="danger")],
                ]
            ),
        )
        svc.store.set_subscription_reminder_sent(subscription.order_id, utc_now())


async def cleanup_expired_accesses(context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    for subscription in svc.store.list_all_subscriptions():
        if subscription.status != "active":
            continue
        if not is_subscription_expired(subscription.ends_at):
            continue
        order = svc.store.get_order(subscription.order_id)
        if subscription.protocol_key == "wireguard" and order and order.inbound_id and subscription.xui_client_id:
            try:
                svc.panel.delete_client(inbound_id=order.inbound_id, client_id=subscription.xui_client_id)
            except PanelError as exc:
                user_row = svc.store.get_user(subscription.telegram_id) or {}
                await notify_admins_about_panel_error(
                    context,
                    stage="Автоудаление истёкшего WireGuard peer",
                    order=order,
                    user_id=subscription.telegram_id,
                    username=user_row.get("username"),
                    error=exc,
                )
                continue
        svc.store.set_subscription_status(subscription.id, "expired")
        if order:
            svc.store.set_order_status(order.id, "expired")
        await notify_user_about_expired_access(context, subscription)


def admin_panel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [styled_inline_button("Статистика", callback_data="admin:stats", style="primary")],
            [styled_inline_button("Промокоды", callback_data="admin:promos", style="success")],
            [styled_inline_button("Пользователи", callback_data="admin:users", style="primary")],
            [styled_inline_button("Назад в меню", callback_data="menu:home", style="danger")],
        ]
    )


async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user = update.effective_user
    if not is_admin_user(svc, user.id if user else None):
        await show_main_menu(update, context, notice="Раздел доступен только администратору.")
        return
    await update.message.reply_text(
        "🛠 <b>Админ-панель</b>\n\nВыберите раздел для управления ботом.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_panel_markup(),
    )


async def show_admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user = update.effective_user
    if not is_admin_user(svc, user.id if user else None):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    await update.callback_query.edit_message_text(
        "🛠 <b>Админ-панель</b>\n\nВыберите раздел для управления ботом.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_panel_markup(),
    )


async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user = update.effective_user
    if not is_admin_user(svc, user.id if user else None):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    text = build_admin_stats_text(svc)
    await update.callback_query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[styled_inline_button("Назад", callback_data="admin:panel", style="primary")]]),
    )


async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user = update.effective_user
    if not is_admin_user(svc, user.id if user else None):
        if update.message:
            await update.message.reply_text("Команда доступна только администратору.")
        return
    report_path = write_stats_report(svc)
    if update.message:
        with report_path.open("rb") as handle:
            await update.message.reply_document(
                document=InputFile(handle, filename=report_path.name),
                caption="📄 Актуальный отчёт по боту.",
            )


def build_admin_stats_text(svc: Services) -> str:
    users = svc.store.list_users()
    orders = svc.store.list_all_orders()
    subscriptions = svc.store.list_all_subscriptions()
    now = datetime.now(ZoneInfo(svc.settings.bot.timezone))
    today = sum(1 for row in users if age_in_days(row.get("created_at"), now) < 1)
    week = sum(1 for row in users if age_in_days(row.get("created_at"), now) < 7)
    month = sum(1 for row in users if age_in_days(row.get("created_at"), now) < 30)
    confirmed_paid_orders = [item for item in orders if is_confirmed_paid_order(item)]
    users_with_any_subscription = {item.telegram_id for item in subscriptions}
    active_paid_subscriptions = [
        item for item in subscriptions if item.payment_type == "paid" and item.status == "active" and remaining_days(item.ends_at) >= 0
    ]
    active_paid_users = {item.telegram_id for item in active_paid_subscriptions}
    paid_users = {item.telegram_id for item in confirmed_paid_orders}
    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"• <b>Всего пользователей:</b> {len(users)}\n"
        f"• <b>Новых за день:</b> {today}\n"
        f"• <b>Новых за неделю:</b> {week}\n"
        f"• <b>Новых за месяц:</b> {month}\n\n"
        f"• <b>Пользователей с любой подпиской:</b> {len(users_with_any_subscription)}\n"
        f"• <b>Пользователей с активной платной подпиской:</b> {len(active_paid_users)}\n"
        f"• <b>Пользователей, оплативших хотя бы раз:</b> {len(paid_users)}\n"
        f"• <b>Действующих платных подписок:</b> {len(active_paid_subscriptions)}\n\n"
        f"• <b>Выручка за день:</b> {sum_revenue_for_days(confirmed_paid_orders, now, 1)} ₽\n"
        f"• <b>Выручка за неделю:</b> {sum_revenue_for_days(confirmed_paid_orders, now, 7)} ₽\n"
        f"• <b>Выручка за месяц:</b> {sum_revenue_for_days(confirmed_paid_orders, now, 30)} ₽\n"
        f"• <b>Выручка за всё время:</b> {sum(item.amount_rub for item in confirmed_paid_orders)} ₽\n\n"
        "• <b>Полный лог-файл:</b> команда <code>/stats</code>"
    )
    return text


def report_file_path() -> Path:
    path = Path("reports") / "bot-stats-latest.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_stats_report(svc: Services) -> Path:
    path = report_file_path()
    path.write_text(build_stats_report_text(svc), encoding="utf-8")
    return path


def build_stats_report_text(svc: Services) -> str:
    users = svc.store.list_users()
    orders = svc.store.list_all_orders()
    subscriptions = svc.store.list_all_subscriptions()
    promo_codes = svc.store.list_promo_codes()
    promo_grants = svc.store.list_promo_grants()
    tickets = svc.store.list_tickets()
    now = datetime.now(ZoneInfo(svc.settings.bot.timezone))
    lines: list[str] = [
        f"Отчёт по боту VPN Халява",
        f"Сформирован: {now.strftime('%d.%m.%Y %H:%M:%S')}",
        "",
        build_admin_stats_plain(svc, users, orders, subscriptions, now),
        "",
        "=== Все пользователи ===",
    ]
    if users:
        for row in users:
            lines.append(
                " | ".join(
                    [
                        f"ID={row.get('telegram_id')}",
                        f"username=@{row.get('username')}" if row.get("username") else "username=нет",
                        f"имя={row.get('first_name') or 'не указано'}",
                        f"создан={row.get('created_at') or '-'}",
                        f"обновлён={row.get('updated_at') or '-'}",
                    ]
                )
            )
    else:
        lines.append("Нет данных.")
    lines.extend(["", "=== Активные платные подписки ==="])
    active_paid = [
        item for item in subscriptions if item.payment_type == "paid" and item.status == "active" and remaining_days(item.ends_at) >= 0
    ]
    if active_paid:
        for item in active_paid:
            lines.append(describe_subscription_line(svc, item))
    else:
        lines.append("Нет данных.")
    lines.extend(["", "=== Подтверждённые покупки ==="])
    confirmed_paid_orders = [item for item in orders if is_confirmed_paid_order(item)]
    if confirmed_paid_orders:
        for item in confirmed_paid_orders:
            lines.append(describe_order_line(svc, item))
    else:
        lines.append("Нет данных.")
    lines.extend(["", "=== Все заказы ==="])
    if orders:
        for item in orders:
            lines.append(describe_order_line(svc, item))
    else:
        lines.append("Нет данных.")
    lines.extend(["", "=== Все подписки ==="])
    if subscriptions:
        for item in subscriptions:
            lines.append(describe_subscription_line(svc, item))
    else:
        lines.append("Нет данных.")
    lines.extend(["", "=== Промокоды ==="])
    if promo_codes:
        for promo in promo_codes:
            activations = [grant for grant in promo_grants if grant.promo_id == promo.id]
            limit = "без лимита" if promo.max_activations is None else str(promo.max_activations)
            lines.append(
                " | ".join(
                    [
                        f"promo_id={promo.id}",
                        f"код={promo.code}",
                        f"дней={promo.days}",
                        f"статус={promo.status}",
                        f"активаций={len(activations)}/{limit}",
                        f"до={promo.expires_at or '-'}",
                        f"создан={promo.created_at}",
                    ]
                )
            )
    else:
        lines.append("Нет данных.")
    lines.extend(["", "=== Активации промокодов ==="])
    if promo_grants:
        for grant in promo_grants:
            user_row = svc.store.get_user(grant.telegram_id) or {}
            lines.append(
                " | ".join(
                    [
                        f"grant_id={grant.id}",
                        f"promo_id={grant.promo_id}",
                        f"код={grant.code}",
                        f"user_id={grant.telegram_id}",
                        f"username=@{user_row.get('username')}" if user_row.get("username") else "username=нет",
                        f"дней={grant.days}",
                        f"статус={grant.status}",
                        f"order_id={grant.activated_order_id or '-'}",
                        f"создан={grant.created_at}",
                        f"обновлён={grant.updated_at}",
                    ]
                )
            )
    else:
        lines.append("Нет данных.")
    lines.extend(["", "=== Тикеты поддержки ==="])
    if tickets:
        for ticket in tickets:
            user_row = svc.store.get_user(ticket.telegram_id) or {}
            lines.append(
                " | ".join(
                    [
                        f"ticket_id={ticket.id}",
                        f"user_id={ticket.telegram_id}",
                        f"username=@{user_row.get('username')}" if user_row.get("username") else "username=нет",
                        f"topic={ticket.topic_key}",
                        f"status={ticket.status}",
                        f"создан={ticket.created_at}",
                        f"обновлён={ticket.updated_at}",
                        f"вопрос={compact_text(ticket.question_text)}",
                        f"ответ={compact_text(ticket.admin_reply or '') or '-'}",
                    ]
                )
            )
    else:
        lines.append("Нет данных.")
    lines.append("")
    return "\n".join(lines)


def build_admin_stats_plain(
    svc: Services,
    users: list[dict[str, object]],
    orders: list[OrderRecord],
    subscriptions: list[SubscriptionRecord],
    now: datetime,
) -> str:
    confirmed_paid_orders = [item for item in orders if is_confirmed_paid_order(item)]
    active_paid_subscriptions = [
        item for item in subscriptions if item.payment_type == "paid" and item.status == "active" and remaining_days(item.ends_at) >= 0
    ]
    users_with_any_subscription = {item.telegram_id for item in subscriptions}
    paid_users = {item.telegram_id for item in confirmed_paid_orders}
    return "\n".join(
        [
            "=== Сводка ===",
            f"Всего пользователей: {len(users)}",
            f"Новых за день: {sum(1 for row in users if age_in_days(row.get('created_at'), now) < 1)}",
            f"Новых за неделю: {sum(1 for row in users if age_in_days(row.get('created_at'), now) < 7)}",
            f"Новых за месяц: {sum(1 for row in users if age_in_days(row.get('created_at'), now) < 30)}",
            f"Пользователей с любой подпиской: {len(users_with_any_subscription)}",
            f"Пользователей с активной платной подпиской: {len({item.telegram_id for item in active_paid_subscriptions})}",
            f"Пользователей, оплативших хотя бы раз: {len(paid_users)}",
            f"Действующих платных подписок: {len(active_paid_subscriptions)}",
            f"Выручка за день: {sum_revenue_for_days(confirmed_paid_orders, now, 1)} ₽",
            f"Выручка за неделю: {sum_revenue_for_days(confirmed_paid_orders, now, 7)} ₽",
            f"Выручка за месяц: {sum_revenue_for_days(confirmed_paid_orders, now, 30)} ₽",
            f"Выручка за всё время: {sum(item.amount_rub for item in confirmed_paid_orders)} ₽",
        ]
    )


def is_confirmed_paid_order(order: OrderRecord) -> bool:
    return order.payment_type == "paid" and order.status in {"fulfilled", "approved"}


def sum_revenue_for_days(orders: list[OrderRecord], now: datetime, days: int) -> int:
    return sum(item.amount_rub for item in orders if age_in_days(item.updated_at or item.created_at, now) < days)


def describe_order_line(svc: Services, order: OrderRecord) -> str:
    device = svc.settings.devices.get(order.device_key)
    plan = plan_for_order(order, svc.settings)
    protocol = svc.settings.protocols.get(order.protocol_key)
    user_row = svc.store.get_user(order.telegram_id) or {}
    return " | ".join(
        [
            f"order_id={order.id}",
            f"order_code={order.order_code}",
            f"user_id={order.telegram_id}",
            f"username=@{user_row.get('username')}" if user_row.get("username") else "username=нет",
            f"device={device.title if device else order.device_key}",
            f"period={plan.title}",
            f"protocol={protocol.title if protocol else order.protocol_key}",
            f"amount={order.amount_rub} ₽",
            f"payment_type={order.payment_type}",
            f"status={order.status}",
            f"inbound_id={order.inbound_id if order.inbound_id is not None else '-'}",
            f"xui_client_id={order.xui_client_id or '-'}",
            f"created={order.created_at}",
            f"updated={order.updated_at}",
        ]
    )


def describe_subscription_line(svc: Services, subscription: SubscriptionRecord) -> str:
    protocol = svc.settings.protocols.get(subscription.protocol_key)
    user_row = svc.store.get_user(subscription.telegram_id) or {}
    return " | ".join(
        [
            f"subscription_id={subscription.id}",
            f"order_id={subscription.order_id}",
            f"user_id={subscription.telegram_id}",
            f"username=@{user_row.get('username')}" if user_row.get("username") else "username=нет",
            f"title={subscription.title}",
            f"protocol={protocol.title if protocol else subscription.protocol_key}",
            f"payment_type={subscription.payment_type}",
            f"status={subscription.status}",
            f"amount={subscription.amount_rub} ₽",
            f"starts={subscription.starts_at}",
            f"ends={subscription.ends_at}",
            f"xui_client_id={subscription.xui_client_id or '-'}",
            f"xui_email={subscription.xui_email or '-'}",
            f"sub_id={subscription.xui_sub_id or '-'}",
        ]
    )


def compact_text(value: str, limit: int = 160) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


async def show_admin_promos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user = update.effective_user
    if not is_admin_user(svc, user.id if user else None):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    await update.callback_query.edit_message_text(
        "🎟 <b>Промокоды</b>\n\nВыберите действие ниже.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [styled_inline_button("Создать промокод", callback_data="admin:promo:create", style="success")],
                [styled_inline_button("Список промокодов", callback_data="admin:promo:list", style="primary")],
                [styled_inline_button("Назад", callback_data="admin:panel", style="danger")],
            ]
        ),
    )


async def start_admin_promo_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user = update.effective_user
    if not is_admin_user(svc, user.id if user else None):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    context.user_data.clear()
    context.user_data["flow"] = "admin_promo_code"
    await update.callback_query.message.reply_text(
        "Введите текст промокода.\n\nПример: <code>HALYAVA7</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=flow_keyboard(),
    )


async def show_promo_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    grants = svc.store.list_promo_grants()
    promos = svc.store.list_promo_codes()
    if not promos:
        text = "🎟 Промокодов пока нет."
    else:
        lines = ["🎟 <b>Промокоды</b>", ""]
        for promo in promos[:10]:
            activations = sum(1 for item in grants if item.promo_id == promo.id)
            limit = "без лимита" if promo.max_activations is None else str(promo.max_activations)
            expires = format_datetime_human(promo.expires_at) if promo.expires_at else "без даты"
            lines.append(
                f"• <b>{promo.code}</b> — {promo.days} дн. • активаций: {activations}/{limit} • до {expires}"
            )
        text = "\n".join(lines)
    await update.callback_query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[styled_inline_button("Назад", callback_data="admin:promos", style="primary")]]),
    )


async def admin_capture_promo_code(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    code = text.strip().upper()
    if not code:
        await update.message.reply_text("Промокод не может быть пустым. Попробуй ещё раз.", reply_markup=flow_keyboard())
        return
    context.user_data["promo_draft"] = {"code": code}
    context.user_data["flow"] = "admin_promo_limit"
    await update.message.reply_text(
        "Теперь введи лимит активаций числом или дату окончания в формате <code>10.04.2026</code>.\n"
        "Если лимит не нужен, напиши <code>безлимит</code>.",
        parse_mode=ParseMode.HTML,
        reply_markup=flow_keyboard(),
    )


async def admin_capture_promo_limit(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    draft = context.user_data.get("promo_draft", {})
    normalized = text.strip().lower()
    max_activations: int | None = None
    expires_at: str | None = None
    if normalized in {"безлимит", "нет", "-"}:
        pass
    elif text.strip().isdigit():
        max_activations = int(text.strip())
    else:
        expires_at = parse_admin_date(text.strip(), services(context).settings.bot.timezone)
        if not expires_at:
            await update.message.reply_text(
                "Введи либо число активаций, либо дату в формате <code>ДД.ММ.ГГГГ</code>.",
                parse_mode=ParseMode.HTML,
                reply_markup=flow_keyboard(),
            )
            return
    draft["max_activations"] = max_activations
    draft["expires_at"] = expires_at
    context.user_data["promo_draft"] = draft
    context.user_data["flow"] = "admin_promo_days"
    await update.message.reply_text(
        "Сколько дней будет давать этот промокод? Введи только число.",
        reply_markup=flow_keyboard(),
    )


async def admin_capture_promo_days(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    svc = services(context)
    if not text.strip().isdigit() or int(text.strip()) <= 0:
        await update.message.reply_text("Нужно ввести положительное число дней.", reply_markup=flow_keyboard())
        return
    draft = context.user_data.get("promo_draft", {})
    promo = svc.store.create_promo_code(
        code=draft["code"],
        days=int(text.strip()),
        max_activations=draft.get("max_activations"),
        expires_at=draft.get("expires_at"),
    )
    context.user_data.clear()
    await update.message.reply_text(
        "✅ Промокод создан\n\n"
        f"• Код: <code>{promo.code}</code>\n"
        f"• Дней: {promo.days}\n"
        f"• Лимит: {'без лимита' if promo.max_activations is None else promo.max_activations}\n"
        f"• Действует до: {format_datetime_human(promo.expires_at) if promo.expires_at else 'без даты'}",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(svc, update.effective_user.id if update.effective_user else None),
    )


async def prompt_admin_user_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user = update.effective_user
    if not is_admin_user(svc, user.id if user else None):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    context.user_data.clear()
    context.user_data["flow"] = "admin_user_lookup"
    await update.callback_query.message.reply_text(
        "Введите ID пользователя или его username.",
        reply_markup=flow_keyboard(),
    )


async def admin_lookup_user(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    svc = services(context)
    user_row = svc.store.find_user(text)
    if not user_row:
        await update.message.reply_text("Пользователь не найден. Попробуй ещё раз.", reply_markup=flow_keyboard())
        return
    context.user_data.clear()
    await send_admin_user_card(update.message, svc, user_row["telegram_id"])


async def show_admin_user_card(update: Update, context: ContextTypes.DEFAULT_TYPE, telegram_id: int) -> None:
    svc = services(context)
    if not is_admin_user(svc, update.effective_user.id if update.effective_user else None):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    await edit_or_send_admin_user_card(update, context, svc, telegram_id)


async def send_admin_user_card(message, svc: Services, telegram_id: int) -> None:
    user_row = svc.store.get_user(telegram_id)
    if not user_row:
        await message.reply_text("Пользователь не найден.", reply_markup=main_menu_keyboard(svc, message.chat_id))
        return
    text, markup = build_admin_user_card(svc, user_row)
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def edit_or_send_admin_user_card(update: Update, context: ContextTypes.DEFAULT_TYPE, svc: Services, telegram_id: int) -> None:
    user_row = svc.store.get_user(telegram_id)
    if not user_row:
        await update.callback_query.edit_message_text("Пользователь не найден.")
        return
    text, markup = build_admin_user_card(svc, user_row)
    await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


def build_admin_user_card(svc: Services, user_row: dict[str, object]) -> tuple[str, InlineKeyboardMarkup]:
    telegram_id = int(user_row["telegram_id"])
    orders = svc.store.list_orders_for_user(telegram_id)
    subscriptions = svc.store.list_subscriptions(telegram_id)
    active = [item for item in subscriptions if item.status == "active" and remaining_days(item.ends_at) >= 0]
    paid_orders = [item for item in orders if item.payment_type == "paid" and item.status in {"fulfilled", "approved"}]
    lines = [
        "👤 <b>Пользователь</b>",
        "",
        f"• <b>Имя:</b> {user_row.get('first_name') or 'не указано'}",
        f"• <b>Username:</b> @{user_row.get('username')}" if user_row.get("username") else "• <b>Username:</b> отсутствует",
        f"• <b>ID:</b> <code>{telegram_id}</code>",
        f"• <b>Дата регистрации:</b> {format_datetime_human(str(user_row.get('created_at')))}",
        f"• <b>Оплаченных подписок:</b> {len(paid_orders)}",
        f"• <b>Активных конфигов:</b> {len(active)}",
    ]
    if active:
        lines.append("")
        lines.append("<b>Активные конфиги:</b>")
        for item in active:
            protocol = svc.settings.protocols.get(item.protocol_key)
            protocol_title = protocol.title if protocol else item.protocol_key
            lines.append(f"• #{item.id} — {profile_summary_title(item)} — {protocol_title} — {format_remaining(item.ends_at)}")
    buttons = []
    for item in active[:8]:
        buttons.append([styled_inline_button(f"Конфиг #{item.id} • {profile_summary_title(item)}", callback_data=f"admin:user:config:{item.id}", style="success")])
    buttons.append([styled_inline_button("Назад", callback_data="admin:panel", style="danger")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def show_admin_subscription_card(update: Update, context: ContextTypes.DEFAULT_TYPE, subscription_id: int) -> None:
    svc = services(context)
    if not is_admin_user(svc, update.effective_user.id if update.effective_user else None):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    subscription = svc.store.get_subscription(subscription_id)
    if not subscription:
        await update.callback_query.edit_message_text("Конфиг не найден.")
        return
    user_row = svc.store.get_user(subscription.telegram_id) or {}
    username_line = (
        f"• <b>Пользователь:</b> @{user_row.get('username')}"
        if user_row.get("username")
        else f"• <b>Пользователь:</b> {user_row.get('first_name') or subscription.telegram_id}"
    )
    text = (
        "🔐 <b>Конфиг пользователя</b>\n\n"
        f"• <b>Конфиг:</b> #{subscription.id}\n"
        f"{username_line}"
    )
    details = (
        f"\n• <b>ID:</b> <code>{subscription.telegram_id}</code>\n"
        f"• <b>Заказ:</b> #{subscription.order_id}\n"
        f"• <b>Подписка:</b> {profile_summary_title(subscription)}\n"
        f"• <b>Протокол:</b> {svc.settings.protocols.get(subscription.protocol_key).title if svc.settings.protocols.get(subscription.protocol_key) else subscription.protocol_key}\n"
        f"• <b>Статус:</b> {subscription.status}\n"
        f"• <b>Активна до:</b> {format_datetime_human(subscription.ends_at)}\n"
        f"• <b>Осталось:</b> {format_remaining(subscription.ends_at)}"
    )
    await update.callback_query.edit_message_text(
        text + details,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [styled_inline_button("Удалить", callback_data=f"admin:user:delete:{subscription.id}", style="danger")],
                [styled_inline_button("Заменить", callback_data=f"admin:user:replace:{subscription.id}", style="success")],
                [styled_inline_button("Назад к пользователю", callback_data=f"admin:user:view:{subscription.telegram_id}", style="primary")],
            ]
        ),
    )


async def prompt_admin_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, subscription_id: int) -> None:
    svc = services(context)
    if not is_admin_user(svc, update.effective_user.id if update.effective_user else None):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    subscription = svc.store.get_subscription(subscription_id)
    if not subscription:
        await update.callback_query.edit_message_text("Конфиг не найден.")
        return
    context.user_data.clear()
    context.user_data["flow"] = "admin_delete_confirm"
    context.user_data["delete_subscription_id"] = subscription_id
    context.user_data["delete_user_id"] = subscription.telegram_id
    await update.callback_query.message.reply_text(
        f"Чтобы удалить конфиг #{subscription.id}, напиши вручную слово <code>удалить</code>.",
        parse_mode=ParseMode.HTML,
        reply_markup=flow_keyboard(),
    )


async def admin_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    svc = services(context)
    subscription_id = context.user_data.get("delete_subscription_id")
    telegram_id = context.user_data.get("delete_user_id")
    if text.strip().lower() != "удалить" or not subscription_id:
        context.user_data.clear()
        await update.message.reply_text(
            "Удаление отменено.",
            reply_markup=main_menu_keyboard(svc, update.effective_user.id if update.effective_user else None),
        )
        return
    subscription = svc.store.get_subscription(int(subscription_id))
    order = svc.store.get_order(subscription.order_id) if subscription else None
    if subscription and order and order.inbound_id and subscription.xui_client_id:
        try:
            svc.panel.delete_client(inbound_id=order.inbound_id, client_id=subscription.xui_client_id)
        except PanelError:
            LOG.exception("Failed to delete subscription client %s", subscription.xui_client_id)
    if subscription:
        svc.store.set_subscription_status(subscription.id, "deleted")
    if order:
        svc.store.set_order_status(order.id, "revoked")
    context.user_data.clear()
    if subscription:
        await context.bot.send_message(
            chat_id=subscription.telegram_id,
            text="❌ Один из ваших конфигов был отключён администратором.",
            reply_markup=main_menu_keyboard(svc, subscription.telegram_id),
        )
    await update.message.reply_text(
        "Конфиг удалён.",
        reply_markup=main_menu_keyboard(svc, update.effective_user.id if update.effective_user else None),
    )
    if telegram_id:
        await send_admin_user_card(update.message, svc, int(telegram_id))


async def replace_subscription_for_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, subscription_id: int) -> None:
    svc = services(context)
    if not is_admin_user(svc, update.effective_user.id if update.effective_user else None):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    subscription = svc.store.get_subscription(subscription_id)
    if not subscription:
        await update.callback_query.edit_message_text("Конфиг не найден.")
        return
    order = svc.store.get_order(subscription.order_id)
    if not order:
        await update.callback_query.edit_message_text("Заказ для этого конфига не найден.")
        return
    user_row = svc.store.get_user(subscription.telegram_id) or {}
    device = svc.settings.devices[subscription.device_key]
    protocol = svc.settings.protocols[subscription.protocol_key]
    plan = plan_for_order(order, svc.settings)
    if order.inbound_id and subscription.xui_client_id:
        try:
            svc.panel.delete_client(inbound_id=order.inbound_id, client_id=subscription.xui_client_id)
        except PanelError:
            LOG.exception("Failed to delete old client during replacement %s", subscription.xui_client_id)
    access = svc.panel.add_client(
        protocol=protocol,
        device=device,
        plan=plan,
        telegram_id=subscription.telegram_id,
        username=user_row.get("username"),
        order_code=order.order_code,
        enabled=True,
        absolute_ends_at=subscription.ends_at,
    )
    svc.store.update_order_xui(
        order.id,
        xui_client_id=access.client_id,
        xui_email=access.email,
        xui_sub_id=access.sub_id,
        subscription_url=access.subscription_url,
        config_text=access.config_text,
        inbound_id=access.inbound_id,
    )
    svc.store.update_subscription_access(
        subscription.id,
        xui_client_id=access.client_id,
        xui_email=access.email,
        xui_sub_id=access.sub_id,
        subscription_url=access.subscription_url,
        config_text=access.config_text,
        title=build_subscription_title(device, protocol),
        reminder_sent_at=None,
    )
    await context.bot.send_message(
        chat_id=subscription.telegram_id,
        text=(
            "♻️ <b>Ваш конфиг был заменён</b>\n\n"
            "Срок действия не изменился. Ниже отправлен новый доступ.\n\n"
            f"⚙️ <b>Новый конфиг:</b>\n<code>{access.config_text}</code>"
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=access_buttons(
            svc,
            device_key=subscription.device_key,
            device_title=svc.settings.devices[subscription.device_key].title,
            subscription_url=access.subscription_url,
            renew_order_id=subscription.order_id,
        ),
    )
    await update.callback_query.edit_message_text(
        "✅ Конфиг заменён. Пользователю отправлен новый доступ.",
        reply_markup=InlineKeyboardMarkup(
            [[styled_inline_button("Назад к пользователю", callback_data=f"admin:user:view:{subscription.telegram_id}", style="primary")]]
        ),
    )


async def claim_promo_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    svc = services(context)
    user = update.effective_user
    if not user or not update.message:
        return
    grant, error = svc.store.claim_promo_code(user.id, code)
    if error:
        await update.message.reply_text(error, reply_markup=main_menu_keyboard(svc, user.id))
        return
    await update.message.reply_text(
        "🎁 <b>Промокод активирован</b>\n\n"
        f"Вы получили бесплатную подписку на <b>{grant.days} дн.</b>\n"
        "Кнопка активации уже появилась в главном меню.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(svc, user.id),
    )


async def show_support_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    rows = [
        [styled_inline_button(topic.title, callback_data=f"support:topic:{topic.key}", style="primary")]
        for topic in svc.settings.support_topics.values()
    ]
    rows.append([styled_inline_button("Назад в меню", callback_data="menu:home", style="danger")])
    await update.message.reply_text(
        f"{svc.settings.branding.support_text}\n\nВыберите тему вопроса:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    await update.message.reply_text("Для выхода нажми кнопку ниже.", reply_markup=flow_keyboard())


async def on_support_topic(update: Update, context: ContextTypes.DEFAULT_TYPE, topic_key: str) -> None:
    svc = services(context)
    topic = svc.settings.support_topics[topic_key]
    context.user_data["flow"] = "support_waiting_text"
    context.user_data["support_topic_key"] = topic_key
    await update.callback_query.edit_message_text(
        f"{topic.title}\n\nОпишите ваш вопрос одним сообщением. Администратор увидит его и ответит прямо в боте.",
        reply_markup=InlineKeyboardMarkup([[styled_inline_button("Назад в меню", callback_data="menu:home", style="danger")]]),
    )


async def submit_support_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE, question_text: str) -> None:
    svc = services(context)
    user = update.effective_user
    if not user:
        return
    topic_key = context.user_data.get("support_topic_key", "other")
    ticket = svc.store.create_ticket(user.id, topic_key, question_text)
    context.user_data.clear()
    await update.message.reply_text("✅ Вопрос отправлен. Как только администратор ответит, сообщение придёт сюда.", reply_markup=main_menu_keyboard(svc, user.id))
    await notify_admins_about_ticket(context, ticket, user.username)


async def notify_admins_about_ticket(context: ContextTypes.DEFAULT_TYPE, ticket, username: str | None) -> None:
    svc = services(context)
    topic = svc.settings.support_topics[ticket.topic_key]
    for admin_id in svc.settings.payments.admin_chat_ids:
        parts = [
            "🛟 <b>Новый вопрос в поддержку</b>",
            "",
            f"Тема: {topic.title}",
            f"Пользователь: @{username}" if username else "Пользователь: без username",
            f"Telegram ID: <code>{ticket.telegram_id}</code>",
            f"Время: {ticket.created_at}",
            "",
            f"Сообщение:\n{ticket.question_text}",
        ]
        buttons = [[styled_inline_button("Ответить в боте", callback_data=f"admin:reply_ticket:{ticket.id}", style="success")]]
        if username:
            buttons.append([styled_inline_button("Открыть личку", url=f"https://t.me/{username}", style="primary")])
        await context.bot.send_message(
            chat_id=admin_id,
            text="\n".join(parts),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def prompt_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, ticket_id: int) -> None:
    svc = services(context)
    if update.effective_user is None or update.effective_user.id not in svc.settings.payments.admin_chat_ids:
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    ticket = svc.store.get_ticket(ticket_id)
    if not ticket:
        await update.callback_query.answer("Тикет не найден", show_alert=True)
        return
    context.user_data["flow"] = "admin_reply_ticket"
    context.user_data["reply_ticket_id"] = ticket.id
    await update.callback_query.message.reply_text("Введите сообщение, которое хотите отправить пользователю.")


async def submit_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_text: str) -> None:
    svc = services(context)
    ticket_id = context.user_data.get("reply_ticket_id")
    if not ticket_id:
        context.user_data.clear()
        await update.message.reply_text("Сессия ответа сброшена.", reply_markup=main_menu_keyboard(svc, update.effective_user.id if update.effective_user else None))
        return
    ticket = svc.store.get_ticket(int(ticket_id))
    if not ticket:
        context.user_data.clear()
        await update.message.reply_text("Тикет не найден.", reply_markup=main_menu_keyboard(svc, update.effective_user.id if update.effective_user else None))
        return
    svc.store.answer_ticket(ticket.id, reply_text)
    context.user_data.clear()
    await context.bot.send_message(
        chat_id=ticket.telegram_id,
        text=f"💬 <b>Ответ поддержки</b>\n\n{reply_text}",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(svc, ticket.telegram_id),
    )
    await update.message.reply_text("Ответ отправлен пользователю.", reply_markup=main_menu_keyboard(svc, update.effective_user.id if update.effective_user else None))


def build_order_code(telegram_id: int) -> str:
    return f"{telegram_id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"


def plan_for_order(order: OrderRecord, settings: Settings) -> PlanConfig:
    if order.period_key in settings.plans:
        return settings.plans[order.period_key]
    if order.period_key.startswith("promo_") and order.period_key.endswith("d"):
        days = parse_promo_days(order.period_key)
        return PlanConfig(
            key=order.period_key,
            title=f"🎁 {days} дн.",
            months=0,
            price=0,
            badge="Промокод",
            button_style="success",
        )
    return settings.plans[order.period_key]


def parse_promo_days(period_key: str) -> int:
    if period_key.startswith("promo_") and period_key.endswith("d"):
        raw = period_key.removeprefix("promo_").removesuffix("d")
        if raw.isdigit():
            return int(raw)
    return 0


def subscription_dates(
    months: int,
    timezone_name: str,
    *,
    duration_days: int | None = None,
    base_ends_at: str | None = None,
) -> tuple[str, str]:
    zone = ZoneInfo(timezone_name)
    now = datetime.now(zone)
    start = now
    if base_ends_at:
        try:
            base = datetime.fromisoformat(base_ends_at)
            if base.tzinfo is None:
                base = base.replace(tzinfo=zone)
            else:
                base = base.astimezone(zone)
            if base > now:
                start = base
        except ValueError:
            pass
    future = start if duration_days == 0 else add_months(start, months)
    if duration_days is not None and duration_days > 0:
        future = start + timedelta(days=duration_days)
    return now.isoformat(), future.isoformat()


def add_months(value: datetime, months: int) -> datetime:
    new_month = value.month - 1 + months
    year = value.year + new_month // 12
    month = new_month % 12 + 1
    day = min(value.day, 28)
    return value.replace(year=year, month=month, day=day)


def remaining_days(ends_at: str) -> int:
    finish = datetime.fromisoformat(ends_at)
    return int((finish - datetime.now(finish.tzinfo)).total_seconds() // 86400)


def is_subscription_expired(ends_at: str) -> bool:
    finish = datetime.fromisoformat(ends_at)
    return finish <= datetime.now(finish.tzinfo)


def format_remaining(ends_at: str) -> str:
    days = max(remaining_days(ends_at), 0)
    return "меньше суток" if days == 0 else f"{days} дн."


def format_datetime_human(value: str) -> str:
    dt = datetime.fromisoformat(value)
    return dt.strftime("%d.%m.%Y %H:%M")


def age_in_days(value: str | None, now: datetime) -> float:
    if not value:
        return 10_000
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return 10_000
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    else:
        parsed = parsed.astimezone(now.tzinfo)
    return max((now - parsed).total_seconds() / 86400, 0)


def parse_admin_date(value: str, timezone_name: str) -> str | None:
    zone = ZoneInfo(timezone_name)
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d-%m-%Y", "%d-%m-%y"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(hour=23, minute=59, second=59, tzinfo=zone).astimezone(ZoneInfo("UTC")).isoformat()
        except ValueError:
            continue
    return None


def build_subscription_title(device: DeviceConfig, protocol: ProtocolConfig) -> str:
    _ = protocol
    return device.title
