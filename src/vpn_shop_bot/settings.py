from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib


@dataclass(slots=True)
class DeviceConfig:
    key: str
    title: str
    description: str
    inbound_id: int | None = None
    requires_protocol: bool = False
    allowed_protocols: list[str] | None = None


@dataclass(slots=True)
class PlanConfig:
    key: str
    title: str
    months: int
    price: int
    badge: str = ""
    button_style: str = "default"


@dataclass(slots=True)
class ProtocolConfig:
    key: str
    title: str
    description: str
    enabled: bool
    inbound_id: int | None
    client_template_json: str
    access_template: str
    method: str = ""


@dataclass(slots=True)
class SupportTopicConfig:
    key: str
    title: str


@dataclass(slots=True)
class BotConfig:
    token: str
    start_image_path: str
    timezone: str
    telegram_proxy_url: str


@dataclass(slots=True)
class BrandingConfig:
    bot_name: str
    welcome_title: str
    welcome_text: str
    support_text: str
    support_url: str


@dataclass(slots=True)
class InstructionsConfig:
    common_url: str
    router_url: str
    smarttv_url: str
    smarttv_help_url: str


@dataclass(slots=True)
class PaymentsConfig:
    payment_url: str
    payment_label: str
    currency: str
    auto_approve_manual_payments: bool
    admin_chat_ids: list[int]


@dataclass(slots=True)
class DatabaseConfig:
    path: str


@dataclass(slots=True)
class SalesConfig:
    precreate_client_on_order: bool
    default_inbound_id: int
    default_protocol: str
    subscription_url_template: str
    traffic_limit_gb: int
    limit_ip: int
    reset: int
    server_address: str
    server_port: int
    public_key: str
    short_id: str
    server_name: str
    spider_x: str
    remark_prefix: str


@dataclass(slots=True)
class XuiConfig:
    enabled: bool
    base_url: str
    username: str
    password: str
    api_token: str
    login_path: str
    verify_tls: bool


@dataclass(slots=True)
class Settings:
    bot: BotConfig
    branding: BrandingConfig
    instructions: InstructionsConfig
    payments: PaymentsConfig
    database: DatabaseConfig
    sales: SalesConfig
    devices: dict[str, DeviceConfig]
    plans: dict[str, PlanConfig]
    protocols: dict[str, ProtocolConfig]
    support_topics: dict[str, SupportTopicConfig]
    xui: XuiConfig

    @property
    def start_image_file(self) -> Path:
        return Path(self.bot.start_image_path)


def _get_table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"Section [{name}] must be a table.")
    return value


def _env_value(key: str, fallback: str) -> str:
    value = os.getenv(key)
    return value if value not in (None, "") else fallback


def _env_bool(key: str, fallback: bool) -> bool:
    value = os.getenv(key)
    if value is None or value == "":
        return fallback
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int_list(key: str, fallback: list[int]) -> list[int]:
    value = os.getenv(key)
    if not value:
        return fallback
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def load_settings(path: str | Path = "config.toml") -> Settings:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))

    bot = _get_table(raw, "bot")
    branding = _get_table(raw, "branding")
    payments = _get_table(raw, "payments")
    database = _get_table(raw, "database")
    sales = _get_table(raw, "sales")
    devices = _get_table(raw, "devices")
    plans = _get_table(raw, "plans")
    protocols = _get_table(raw, "protocols")
    support_topics = _get_table(raw, "support_topics")
    xui = _get_table(raw, "xui")

    device_configs = {
        key: DeviceConfig(
            key=key,
            title=value["title"],
            description=value.get("description", ""),
            inbound_id=value.get("inbound_id"),
            requires_protocol=bool(value.get("requires_protocol", False)),
            allowed_protocols=list(value.get("allowed_protocols", [])) or None,
        )
        for key, value in devices.items()
    }

    plan_configs = {
        key: PlanConfig(
            key=key,
            title=value["title"],
            months=int(value["months"]),
            price=int(value["price"]),
            badge=value.get("badge", ""),
            button_style=value.get("button_style", "default"),
        )
        for key, value in plans.items()
    }

    protocol_configs = {
        key: ProtocolConfig(
            key=key,
            title=value["title"],
            description=value.get("description", ""),
            enabled=bool(value.get("enabled", False)),
            inbound_id=value.get("inbound_id"),
            client_template_json=value.get("client_template_json", ""),
            access_template=value.get("access_template", ""),
            method=value.get("method", ""),
        )
        for key, value in protocols.items()
    }

    support_topic_configs = {
        key: SupportTopicConfig(key=key, title=value["title"])
        for key, value in support_topics.items()
    }

    return Settings(
        bot=BotConfig(
            token=_env_value("BOT_TOKEN", bot.get("token", "")),
            start_image_path=bot.get("start_image_path", ""),
            timezone=bot.get("timezone", "UTC"),
            telegram_proxy_url=_env_value("TELEGRAM_PROXY_URL", ""),
        ),
        branding=BrandingConfig(
            bot_name=branding["bot_name"],
            welcome_title=branding["welcome_title"],
            welcome_text=branding["welcome_text"],
            support_text=branding["support_text"],
            support_url=_env_value("SUPPORT_URL", branding.get("support_url", "")),
        ),
        instructions=InstructionsConfig(
            common_url=_env_value("INSTRUCTION_COMMON_URL", ""),
            router_url=_env_value("INSTRUCTION_ROUTER_URL", ""),
            smarttv_url=_env_value("INSTRUCTION_SMARTTV_URL", ""),
            smarttv_help_url=_env_value("SMARTTV_HELP_URL", ""),
        ),
        payments=PaymentsConfig(
            payment_url=_env_value("PAYMENT_URL", payments.get("payment_url", "")),
            payment_label=payments.get("payment_label", "Оплатить"),
            currency=payments.get("currency", "RUB"),
            auto_approve_manual_payments=bool(payments.get("auto_approve_manual_payments", False)),
            admin_chat_ids=_env_int_list("ADMIN_CHAT_IDS", [int(item) for item in payments.get("admin_chat_ids", [])]),
        ),
        database=DatabaseConfig(path=database.get("path", "bot-data.json")),
        sales=SalesConfig(
            precreate_client_on_order=bool(sales.get("precreate_client_on_order", False)),
            default_inbound_id=int(sales["default_inbound_id"]),
            default_protocol=sales.get("default_protocol", "vless"),
            subscription_url_template=sales.get("subscription_url_template", ""),
            traffic_limit_gb=int(sales.get("traffic_limit_gb", 0)),
            limit_ip=int(sales.get("limit_ip", 0)),
            reset=int(sales.get("reset", 0)),
            server_address=sales.get("server_address", ""),
            server_port=int(sales.get("server_port", 443)),
            public_key=sales.get("public_key", ""),
            short_id=sales.get("short_id", ""),
            server_name=sales.get("server_name", ""),
            spider_x=sales.get("spider_x", ""),
            remark_prefix=sales.get("remark_prefix", "tg"),
        ),
        devices=device_configs,
        plans=plan_configs,
        protocols=protocol_configs,
        support_topics=support_topic_configs,
        xui=XuiConfig(
            enabled=_env_bool("XUI_ENABLED", bool(xui.get("enabled", False))),
            base_url=_env_value("XUI_BASE_URL", str(xui.get("base_url", ""))).rstrip("/"),
            username=_env_value("XUI_USERNAME", xui.get("username", "")),
            password=_env_value("XUI_PASSWORD", xui.get("password", "")),
            api_token=_env_value("XUI_API_TOKEN", xui.get("api_token", "")),
            login_path=xui.get("login_path", "/login"),
            verify_tls=bool(xui.get("verify_tls", True)),
        ),
    )
