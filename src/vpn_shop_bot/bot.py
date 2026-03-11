from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import DeviceConfig, PlanConfig, Settings
from .db import Database, OrderRecord
from .xui import ProvisionedClient, XuiClient, XuiError

LOG = logging.getLogger(__name__)

BUY_TEXT = "Купить VPN"
PROFILE_TEXT = "Профиль"
SUPPORT_TEXT = "Поддержка"
MENU_TEXT = "Главное меню"
HISTORY_CALLBACK = "profile:history"
PROFILE_CALLBACK = "profile:back"


@dataclass(slots=True)
class Services:
    settings: Settings
    db: Database
    xui: XuiClient


def build_application(settings: Settings) -> Application:
    service_bundle = Services(
        settings=settings,
        db=Database(settings.database.path),
        xui=XuiClient(settings.xui, settings.sales),
    )
    application = ApplicationBuilder().token(settings.bot.token).build()
    application.bot_data["services"] = service_bundle
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(handle_callbacks))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return application


def services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data["services"]


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BUY_TEXT)],
            [KeyboardButton(PROFILE_TEXT), KeyboardButton(SUPPORT_TEXT)],
        ],
        resize_keyboard=True,
    )


def flow_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(MENU_TEXT)]], resize_keyboard=True)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_main_menu(update, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    svc = services(context)
    svc.db.upsert_user(update.effective_user.id, update.effective_user.username, update.effective_user.first_name)
    text = update.message.text
    if text == BUY_TEXT:
        await show_device_picker(update, context)
        return
    if text == PROFILE_TEXT:
        await show_profile(update, context)
        return
    if text == SUPPORT_TEXT:
        await show_support(update, context)
        return
    if text == MENU_TEXT:
        await cancel_active_order(update, context, announce=True)
        return
    await update.message.reply_text("🙂 Выбери действие на клавиатуре ниже.", reply_markup=main_menu_keyboard())


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, *, text: str | None = None) -> None:
    svc = services(context)
    user = update.effective_user
    if user:
        svc.db.upsert_user(user.id, user.username, user.first_name)
    context.user_data.clear()
    body = (
        f"{svc.settings.branding.welcome_title}\n\n"
        f"{svc.settings.branding.welcome_text}\n\n"
        "🛍 Главное меню:\n"
        "• Купить VPN\n"
        "• Профиль\n"
        "• Поддержка"
    )
    if text:
        body = f"{text}\n\n{body}"
    image_path = svc.settings.start_image_file
    if update.message:
        if image_path.exists():
            with image_path.open("rb") as image:
                await update.message.reply_photo(photo=image, caption=body, reply_markup=main_menu_keyboard())
        else:
            await update.message.reply_text(body, reply_markup=main_menu_keyboard())
    elif update.callback_query:
        await update.callback_query.message.reply_text(body, reply_markup=main_menu_keyboard())


async def show_device_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    context.user_data.clear()
    context.user_data["flow"] = "select_device"
    keyboard = [
        [InlineKeyboardButton(device.title, callback_data=f"device:{device.key}")]
        for device in svc.settings.devices.values()
    ]
    await update.effective_message.reply_text(
        "🧩 Наш VPN для вашего устройства.\n\nВыберите устройство:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    await update.effective_message.reply_text(
        "⬇️ Для выхода из оформления используй кнопку ниже.",
        reply_markup=flow_keyboard(),
    )


async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    if data.startswith("device:"):
        await handle_device_selection(update, context, data.split(":", 1)[1])
        return
    if data.startswith("plan:"):
        await handle_plan_selection(update, context, data.split(":", 1)[1])
        return
    if data.startswith("pay:"):
        await show_payment_step(update, context, int(data.split(":", 1)[1]))
        return
    if data.startswith("confirm:"):
        await confirm_payment(update, context, int(data.split(":", 1)[1]))
        return
    if data.startswith("cancel:"):
        await cancel_order(update, context, int(data.split(":", 1)[1]))
        return
    if data == HISTORY_CALLBACK:
        await show_payment_history(update, context)
        return
    if data == PROFILE_CALLBACK:
        await show_profile(update, context)
        return
    if data.startswith("config:"):
        await show_saved_config(update, context, int(data.split(":", 1)[1]))


async def handle_device_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, device_key: str) -> None:
    svc = services(context)
    device = svc.settings.devices[device_key]
    context.user_data["device_key"] = device.key
    context.user_data["flow"] = "select_plan"
    keyboard = [
        [InlineKeyboardButton(f"{plan.title} • {plan.price} ₽", callback_data=f"plan:{plan.key}")]
        for plan in svc.settings.plans.values()
    ]
    await update.callback_query.edit_message_text(
        f"{device.title}\n{device.description}\n\n💳 Выберите период подписки:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_plan_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_key: str) -> None:
    svc = services(context)
    user = update.effective_user
    if not user:
        return
    device_key = context.user_data.get("device_key")
    if not device_key:
        await update.callback_query.message.reply_text("Сессия оформления сброшена. Начни покупку заново.")
        await show_main_menu(update, context)
        return
    device = svc.settings.devices[device_key]
    plan = svc.settings.plans[plan_key]
    order = svc.db.create_order(
        order_code=build_order_code(user.id),
        telegram_id=user.id,
        device_key=device.key,
        period_key=plan.key,
        amount_rub=plan.price,
        payment_url=svc.settings.payments.payment_url,
    )
    context.user_data["active_order_id"] = order.id
    context.user_data["flow"] = "payment"
    if svc.settings.sales.precreate_client_on_order:
        await ensure_order_client(order, device, plan, user.id, svc, enabled=False)
        order = svc.db.get_order(order.id) or order
    await update.callback_query.edit_message_text(
        render_order_summary(order, device, plan),
        reply_markup=build_order_keyboard(order.id),
        parse_mode=ParseMode.HTML,
    )


def render_order_summary(order: OrderRecord, device: DeviceConfig, plan: PlanConfig) -> str:
    return (
        "🧾 <b>Заказ оформлен</b>\n\n"
        f"Номер заказа: <code>{order.order_code}</code>\n"
        f"Устройство: {device.title}\n"
        f"Период: {plan.title}\n"
        f"Сумма: <b>{plan.price} ₽</b>\n\n"
        "Оплати заказ и используй кнопки ниже."
    )


def build_order_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Оплатить", callback_data=f"pay:{order_id}")],
            [InlineKeyboardButton("✅ Я оплатил", callback_data=f"confirm:{order_id}")],
            [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel:{order_id}")],
        ]
    )


async def show_payment_step(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    svc = services(context)
    order = svc.db.get_order(order_id)
    if not order:
        await update.callback_query.message.reply_text("Заказ не найден.")
        return
    await update.callback_query.message.reply_text(
        "💸 Перейдите по ссылке для оплаты.\n\n"
        f"Укажите сумму: <b>{order.amount_rub} ₽</b>\n"
        "Оплатите удобным способом и после оплаты нажмите кнопку <b>Я оплатил</b>.",
        reply_markup=build_order_keyboard(order.id),
        parse_mode=ParseMode.HTML,
    )
    await update.callback_query.message.reply_text(
        f"{svc.settings.payments.payment_label}: {order.payment_url}",
        disable_web_page_preview=True,
    )


async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    svc = services(context)
    order = svc.db.get_order(order_id)
    user = update.effective_user
    if not order or not user:
        await update.callback_query.message.reply_text("Заказ не найден.")
        return
    device = svc.settings.devices[order.device_key]
    plan = svc.settings.plans[order.period_key]
    if not svc.settings.payments.auto_approve_manual_payments:
        svc.db.set_order_status(order.id, "awaiting_confirmation")
        await notify_admins(svc, context, order, user.id)
        await update.callback_query.message.reply_text(
            "⏳ Отметили оплату. Сейчас проверим перевод и сразу пришлём доступ.",
            reply_markup=flow_keyboard(),
        )
        return

    try:
        provisioned = await ensure_order_client(order, device, plan, user.id, svc, enabled=True)
    except XuiError as exc:
        LOG.exception("Failed to provision 3x-ui client")
        await update.callback_query.message.reply_text(
            "⚠️ Не удалось выдать конфиг через 3x-ui. Проверь настройки панели и повтори позже.\n\n"
            f"Деталь: <code>{exc}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
        return

    svc.db.set_order_status(order.id, "fulfilled")
    starts_at, ends_at = subscription_dates(plan.months, svc.settings.bot.timezone)
    svc.db.create_subscription(
        telegram_id=user.id,
        order_id=order.id,
        device_key=device.key,
        period_key=plan.key,
        title=f"{device.title} • {plan.title}",
        status="active",
        starts_at=starts_at,
        ends_at=ends_at,
        amount_rub=plan.price,
        xui_client_id=provisioned.client_id if provisioned else order.xui_client_id,
        xui_email=provisioned.email if provisioned else order.xui_email,
        subscription_url=provisioned.subscription_url if provisioned else order.subscription_url,
        config_text=provisioned.config_text if provisioned else order.config_text,
    )
    await send_successful_delivery(update, context, order.id)


async def ensure_order_client(
    order: OrderRecord,
    device: DeviceConfig,
    plan: PlanConfig,
    telegram_id: int,
    svc: Services,
    *,
    enabled: bool,
) -> ProvisionedClient | None:
    inbound_id = select_inbound_id(device, svc.settings)
    if not svc.xui.enabled:
        subscription_url = f"manual://{order.order_code}"
        config_text = (
            f"Order: {order.order_code}\n"
            f"Device: {device.title}\n"
            f"Plan: {plan.title}\n"
            "3x-ui integration disabled. Fill config.toml [xui] to enable auto provisioning."
        )
        svc.db.update_order_xui(
            order.id,
            xui_client_id=order.xui_client_id,
            xui_email=order.xui_email,
            subscription_url=subscription_url,
            config_text=config_text,
            inbound_id=inbound_id,
        )
        return ProvisionedClient(
            client_id=order.xui_client_id or "manual",
            email=order.xui_email or f"manual_{telegram_id}_{order.order_code}",
            inbound_id=inbound_id,
            subscription_url=subscription_url,
            config_text=config_text,
            title=f"{device.title} • {plan.title}",
        )

    if order.xui_client_id and order.xui_email:
        svc.xui.update_client(
            inbound_id=inbound_id,
            client_id=order.xui_client_id,
            email=order.xui_email,
            tg_id=telegram_id,
            order_code=order.order_code,
            period_title=plan.title,
            months=plan.months,
            device_title=device.title,
            price_rub=plan.price,
            enabled=enabled,
        )
        return ProvisionedClient(
            client_id=order.xui_client_id,
            email=order.xui_email,
            inbound_id=inbound_id,
            subscription_url=order.subscription_url or "",
            config_text=order.config_text or "",
            title=f"{device.title} • {plan.title}",
        )

    created = svc.xui.add_client(
        inbound_id=inbound_id,
        tg_id=telegram_id,
        order_code=order.order_code,
        period_title=plan.title,
        months=plan.months,
        device_title=device.title,
        price_rub=plan.price,
        enabled=enabled,
    )
    svc.db.update_order_xui(
        order.id,
        xui_client_id=created.client_id,
        xui_email=created.email,
        subscription_url=created.subscription_url,
        config_text=created.config_text,
        inbound_id=created.inbound_id,
    )
    return created


def select_inbound_id(device: DeviceConfig, settings: Settings) -> int:
    return device.inbound_id or settings.sales.default_inbound_id


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    svc = services(context)
    order = svc.db.get_order(order_id)
    if not order:
        await show_main_menu(update, context, text="Заказ уже неактивен.")
        return
    if order.xui_client_id and order.inbound_id and svc.xui.enabled:
        try:
            svc.xui.delete_client(inbound_id=order.inbound_id, client_id=order.xui_client_id)
        except XuiError:
            LOG.exception("Failed to delete draft client %s", order.xui_client_id)
    svc.db.set_order_status(order.id, "cancelled")
    await show_main_menu(update, context, text="❌ Заказ отменён. Все шаги оформления очищены.")


async def cancel_active_order(update: Update, context: ContextTypes.DEFAULT_TYPE, *, announce: bool) -> None:
    order_id = context.user_data.get("active_order_id")
    if order_id:
        await cancel_order(update, context, int(order_id))
        return
    if announce:
        await show_main_menu(update, context)


async def send_successful_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    svc = services(context)
    order = svc.db.get_order(order_id)
    if not order:
        await show_main_menu(update, context)
        return
    message = (
        "✅ <b>Оплата подтверждена</b>\n\n"
        "Доступ готов. Ниже твой конфиг и подписочная ссылка.\n\n"
        f"🔗 Подписка: <code>{order.subscription_url or 'не настроена'}</code>\n\n"
        f"⚙️ Конфиг:\n<code>{order.config_text or 'не настроен'}</code>"
    )
    await update.callback_query.message.reply_text(
        message,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
        disable_web_page_preview=True,
    )
    context.user_data.clear()


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user = update.effective_user
    if not user:
        return
    subs = svc.db.list_subscriptions(user.id)
    lines = [
        "👤 <b>Ваш профиль</b>",
        "",
        f"Nickname: @{user.username}" if user.username else f"Nickname: {user.first_name}",
        f"ID: <code>{user.id}</code>",
    ]
    active = [sub for sub in subs if sub.status == "active" and remaining_days(sub.ends_at) >= 0]
    if active:
        lines.append(f"Активных подписок: <b>{len(active)}</b>")
        lines.append("")
        for sub in active:
            lines.append(f"• {sub.title} — осталось {format_remaining(sub.ends_at)}")
    else:
        lines.append("Активных подписок нет.")

    keyboard_rows = [[InlineKeyboardButton("🧾 История оплат", callback_data=HISTORY_CALLBACK)]]
    for sub in active[:5]:
        keyboard_rows.append([InlineKeyboardButton(f"🔐 Получить конфиг #{sub.order_id}", callback_data=f"config:{sub.order_id}")])
    keyboard_rows.append([InlineKeyboardButton("🏠 Назад в меню", callback_data=PROFILE_CALLBACK)])

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
    )
    await update.effective_message.reply_text("⬇️ Главное меню доступно на клавиатуре ниже.", reply_markup=main_menu_keyboard())


async def show_payment_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user = update.effective_user
    if not user:
        return
    orders = svc.db.list_paid_orders(user.id)
    if not orders:
        text = "🧾 История оплат пока пустая."
    else:
        chunks = ["🧾 <b>История оплат</b>", ""]
        for order in orders[:10]:
            plan = svc.settings.plans.get(order.period_key)
            device = svc.settings.devices.get(order.device_key)
            title = f"{device.title if device else order.device_key} • {plan.title if plan else order.period_key}"
            chunks.append(f"• <code>{order.order_code}</code> — {title} — {order.amount_rub} ₽")
        text = "\n".join(chunks)
    if update.callback_query:
        await update.callback_query.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ К профилю", callback_data=PROFILE_CALLBACK)]]),
        )


async def show_saved_config(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    svc = services(context)
    order = svc.db.get_order(order_id)
    if not order:
        await update.callback_query.message.reply_text("Конфиг не найден.")
        return
    await update.callback_query.message.reply_text(
        "🔐 <b>Ваш доступ</b>\n\n"
        f"Подписка: <code>{order.subscription_url or 'не настроена'}</code>\n\n"
        f"Конфиг:\n<code>{order.config_text or 'не настроен'}</code>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    await update.effective_message.reply_text(
        f"{svc.settings.branding.support_text}\n\n{svc.settings.branding.support_url}",
        disable_web_page_preview=True,
        reply_markup=main_menu_keyboard(),
    )


async def notify_admins(svc: Services, context: ContextTypes.DEFAULT_TYPE, order: OrderRecord, telegram_id: int) -> None:
    if not svc.settings.payments.admin_chat_ids:
        return
    for chat_id in svc.settings.payments.admin_chat_ids:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Новая отметка об оплате.\n"
                f"Заказ: {order.order_code}\n"
                f"Telegram ID: {telegram_id}\n"
                f"Сумма: {order.amount_rub} ₽"
            ),
        )


def build_order_code(telegram_id: int) -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"{telegram_id}-{stamp}"


def subscription_dates(months: int, timezone_name: str) -> tuple[str, str]:
    zone = ZoneInfo(timezone_name)
    now = datetime.now(zone)
    future = now + timedelta(days=30 * months)
    return now.isoformat(), future.isoformat()


def remaining_days(ends_at: str) -> int:
    finish = datetime.fromisoformat(ends_at)
    return int((finish - datetime.now(finish.tzinfo)).total_seconds() // 86400)


def format_remaining(ends_at: str) -> str:
    finish = datetime.fromisoformat(ends_at)
    delta = finish - datetime.now(finish.tzinfo)
    days = max(int(delta.total_seconds() // 86400), 0)
    if days == 0:
        return "меньше суток"
    return f"{days} дн."
