from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class OrderRecord:
    id: int
    order_code: str
    telegram_id: int
    device_key: str
    period_key: str
    amount_rub: int
    status: str
    payment_url: str
    xui_client_id: str | None
    xui_email: str | None
    subscription_url: str | None
    config_text: str | None
    inbound_id: int | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class SubscriptionRecord:
    id: int
    telegram_id: int
    order_id: int
    device_key: str
    period_key: str
    title: str
    status: str
    starts_at: str
    ends_at: str
    amount_rub: int
    xui_client_id: str | None
    xui_email: str | None
    subscription_url: str | None
    config_text: str | None
    created_at: str
    updated_at: str


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        if self.path.exists():
            return
        self._write(
            {
                "users": {},
                "orders": [],
                "subscriptions": [],
                "counters": {"order_id": 0, "subscription_id": 0},
            }
        )

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def upsert_user(self, telegram_id: int, username: str | None, first_name: str | None) -> None:
        data = self._read()
        now = utc_now()
        key = str(telegram_id)
        existing = data["users"].get(key, {})
        data["users"][key] = {
            "telegram_id": telegram_id,
            "username": username,
            "first_name": first_name,
            "created_at": existing.get("created_at", now),
            "updated_at": now,
        }
        self._write(data)

    def create_order(
        self,
        *,
        order_code: str,
        telegram_id: int,
        device_key: str,
        period_key: str,
        amount_rub: int,
        payment_url: str,
    ) -> OrderRecord:
        data = self._read()
        now = utc_now()
        data["counters"]["order_id"] += 1
        record = OrderRecord(
            id=data["counters"]["order_id"],
            order_code=order_code,
            telegram_id=telegram_id,
            device_key=device_key,
            period_key=period_key,
            amount_rub=amount_rub,
            status="pending_payment",
            payment_url=payment_url,
            xui_client_id=None,
            xui_email=None,
            subscription_url=None,
            config_text=None,
            inbound_id=None,
            created_at=now,
            updated_at=now,
        )
        data["orders"].append(asdict(record))
        self._write(data)
        return record

    def get_order(self, order_id: int) -> OrderRecord | None:
        data = self._read()
        for item in data["orders"]:
            if item["id"] == order_id:
                return OrderRecord(**item)
        return None

    def update_order_xui(
        self,
        order_id: int,
        *,
        xui_client_id: str | None,
        xui_email: str | None,
        subscription_url: str | None,
        config_text: str | None,
        inbound_id: int | None,
    ) -> None:
        data = self._read()
        for item in data["orders"]:
            if item["id"] == order_id:
                item["xui_client_id"] = xui_client_id
                item["xui_email"] = xui_email
                item["subscription_url"] = subscription_url
                item["config_text"] = config_text
                item["inbound_id"] = inbound_id
                item["updated_at"] = utc_now()
                break
        self._write(data)

    def set_order_status(self, order_id: int, status: str) -> None:
        data = self._read()
        for item in data["orders"]:
            if item["id"] == order_id:
                item["status"] = status
                item["updated_at"] = utc_now()
                break
        self._write(data)

    def create_subscription(
        self,
        *,
        telegram_id: int,
        order_id: int,
        device_key: str,
        period_key: str,
        title: str,
        status: str,
        starts_at: str,
        ends_at: str,
        amount_rub: int,
        xui_client_id: str | None,
        xui_email: str | None,
        subscription_url: str | None,
        config_text: str | None,
    ) -> None:
        data = self._read()
        now = utc_now()
        existing = None
        for item in data["subscriptions"]:
            if item["order_id"] == order_id:
                existing = item
                break
        if existing is None:
            data["counters"]["subscription_id"] += 1
            existing = {
                "id": data["counters"]["subscription_id"],
                "created_at": now,
            }
            data["subscriptions"].append(existing)
        existing.update(
            {
                "telegram_id": telegram_id,
                "order_id": order_id,
                "device_key": device_key,
                "period_key": period_key,
                "title": title,
                "status": status,
                "starts_at": starts_at,
                "ends_at": ends_at,
                "amount_rub": amount_rub,
                "xui_client_id": xui_client_id,
                "xui_email": xui_email,
                "subscription_url": subscription_url,
                "config_text": config_text,
                "updated_at": now,
            }
        )
        self._write(data)

    def list_subscriptions(self, telegram_id: int) -> list[SubscriptionRecord]:
        data = self._read()
        rows = [SubscriptionRecord(**item) for item in data["subscriptions"] if item["telegram_id"] == telegram_id]
        return sorted(rows, key=lambda item: item.ends_at)

    def list_paid_orders(self, telegram_id: int) -> list[OrderRecord]:
        data = self._read()
        rows = [
            OrderRecord(**item)
            for item in data["orders"]
            if item["telegram_id"] == telegram_id and item["status"] in {"paid", "fulfilled"}
        ]
        return sorted(rows, key=lambda item: item.created_at, reverse=True)
