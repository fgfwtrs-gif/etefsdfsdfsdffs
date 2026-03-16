from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(slots=True)
class PlanConfig:
    key: str
    title: str
    months: int
    price: int


@dataclass(slots=True)
class BotConfig:
    token: str
    start_image_path: str
    timezone: str


@dataclass(slots=True)
class BrandingConfig:
    bot_name: str
    welcome_title: str
    welcome_text: str
    support_text: str
    support_url: str


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
    subscription_url_template: str
    traffic_limit_gb: int
    limit_ip: int
    reset: int
    client_flow: str
    client_security: str
    server_address: str
    server_port: int
    public_key: str
    short_id: str
    server_name: str
    spider_x: str
    remark_prefix: str
    protocol: str
    config_template: str


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
    payments: PaymentsConfig
    database: DatabaseConfig
    sales: SalesConfig
    devices: dict[str, DeviceConfig]
    plans: dict[str, PlanConfig]
    xui: XuiConfig

    @property
    def start_image_file(self) -> Path:
        return Path(self.bot.start_image_path)


def _get_table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"Section [{name}] must be a table.")
    return value


def load_settings(path: str | Path = "config.toml") -> Settings:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))

    bot = _get_table(raw, "bot")
    branding = _get_table(raw, "branding")
    payments = _get_table(raw, "payments")
    database = _get_table(raw, "database")
    sales = _get_table(raw, "sales")
    devices = _get_table(raw, "devices")
    plans = _get_table(raw, "plans")
    xui = _get_table(raw, "xui")

    device_configs = {
        key: DeviceConfig(
            key=key,
            title=value["title"],
            description=value.get("description", ""),
            inbound_id=value.get("inbound_id"),
        )
        for key, value in devices.items()
    }

    plan_configs = {
        key: PlanConfig(
            key=key,
            title=value["title"],
            months=int(value["months"]),
            price=int(value["price"]),
        )
        for key, value in plans.items()
    }

    return Settings(
        bot=BotConfig(
            token=bot["token"],
            start_image_path=bot.get("start_image_path", ""),
            timezone=bot.get("timezone", "UTC"),
        ),
        branding=BrandingConfig(
            bot_name=branding["bot_name"],
            welcome_title=branding["welcome_title"],
            welcome_text=branding["welcome_text"],
            support_text=branding["support_text"],
            support_url=branding["support_url"],
        ),
        payments=PaymentsConfig(
            payment_url=payments["payment_url"],
            payment_label=payments.get("payment_label", "Оплатить"),
            currency=payments.get("currency", "RUB"),
            auto_approve_manual_payments=bool(payments.get("auto_approve_manual_payments", False)),
            admin_chat_ids=[int(item) for item in payments.get("admin_chat_ids", [])],
        ),
        database=DatabaseConfig(path=database.get("path", "bot.sqlite3")),
        sales=SalesConfig(
            precreate_client_on_order=bool(sales.get("precreate_client_on_order", False)),
            default_inbound_id=int(sales["default_inbound_id"]),
            subscription_url_template=sales.get("subscription_url_template", ""),
            traffic_limit_gb=int(sales.get("traffic_limit_gb", 0)),
            limit_ip=int(sales.get("limit_ip", 0)),
            reset=int(sales.get("reset", 0)),
            client_flow=sales.get("client_flow", ""),
            client_security=sales.get("client_security", ""),
            server_address=sales.get("server_address", ""),
            server_port=int(sales.get("server_port", 443)),
            public_key=sales.get("public_key", ""),
            short_id=sales.get("short_id", ""),
            server_name=sales.get("server_name", ""),
            spider_x=sales.get("spider_x", ""),
            remark_prefix=sales.get("remark_prefix", "tg"),
            protocol=sales.get("protocol", "vless"),
            config_template=sales.get("config_template", ""),
        ),
        devices=device_configs,
        plans=plan_configs,
        xui=XuiConfig(
            enabled=bool(xui.get("enabled", False)),
            base_url=str(xui.get("base_url", "")).rstrip("/"),
            username=xui.get("username", ""),
            password=xui.get("password", ""),
            api_token=xui.get("api_token", ""),
            login_path=xui.get("login_path", "/login"),
            verify_tls=bool(xui.get("verify_tls", True)),
        ),
    )
