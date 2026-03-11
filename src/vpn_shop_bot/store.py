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
    protocol_key: str
    amount_rub: int
    status: str
    payment_type: str
    payment_url: str
    xui_client_id: str | None
    xui_email: str | None
    xui_sub_id: str | None
    subscription_url: str | None
    config_text: str | None
    inbound_id: int | None
    source_subscription_order_id: int | None
    promo_grant_id: int | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class SubscriptionRecord:
    id: int
    telegram_id: int
    order_id: int
    device_key: str
    period_key: str
    protocol_key: str
    title: str
    status: str
    payment_type: str
    starts_at: str
    ends_at: str
    amount_rub: int
    xui_client_id: str | None
    xui_email: str | None
    xui_sub_id: str | None
    subscription_url: str | None
    config_text: str | None
    reminder_sent_at: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class SupportTicketRecord:
    id: int
    telegram_id: int
    topic_key: str
    question_text: str
    status: str
    admin_reply: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class PromoCodeRecord:
    id: int
    code: str
    days: int
    max_activations: int | None
    expires_at: str | None
    status: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class PromoGrantRecord:
    id: int
    promo_id: int
    telegram_id: int
    code: str
    days: int
    status: str
    activated_order_id: int | None
    created_at: str
    updated_at: str


class Store:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_file()

    def _blank_payload(self) -> dict[str, Any]:
        return {
            "users": {},
            "orders": [],
            "subscriptions": [],
            "tickets": [],
            "promo_codes": [],
            "promo_grants": [],
            "counters": {
                "order_id": 0,
                "subscription_id": 0,
                "ticket_id": 0,
                "promo_id": 0,
                "promo_grant_id": 0,
            },
        }

    def _ensure_file(self) -> None:
        if not self.path.exists():
            self._write(self._blank_payload())
            return
        data = self._read()
        data.setdefault("users", {})
        data.setdefault("orders", [])
        data.setdefault("subscriptions", [])
        data.setdefault("tickets", [])
        data.setdefault("promo_codes", [])
        data.setdefault("promo_grants", [])
        counters = data.setdefault("counters", {})
        counters.setdefault("order_id", len(data["orders"]))
        counters.setdefault("subscription_id", len(data["subscriptions"]))
        counters.setdefault("ticket_id", len(data["tickets"]))
        counters.setdefault("promo_id", len(data["promo_codes"]))
        counters.setdefault("promo_grant_id", len(data["promo_grants"]))
        for order in data["orders"]:
            order.setdefault("payment_type", "paid")
            order.setdefault("source_subscription_order_id", None)
            order.setdefault("promo_grant_id", None)
        for subscription in data["subscriptions"]:
            subscription.setdefault("payment_type", "paid")
            subscription.setdefault("reminder_sent_at", None)
        self._write(data)

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

    def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        data = self._read()
        return data["users"].get(str(telegram_id))

    def list_users(self) -> list[dict[str, Any]]:
        data = self._read()
        rows = list(data["users"].values())
        return sorted(rows, key=lambda item: item.get("created_at", ""))

    def find_user(self, query: str) -> dict[str, Any] | None:
        data = self._read()
        query = query.strip()
        if not query:
            return None
        if query.lstrip("-").isdigit():
            return data["users"].get(str(int(query)))
        normalized = query.lstrip("@").lower()
        for row in data["users"].values():
            if (row.get("username") or "").lower() == normalized:
                return row
        return None

    def create_order(
        self,
        *,
        order_code: str,
        telegram_id: int,
        device_key: str,
        period_key: str,
        protocol_key: str,
        amount_rub: int,
        payment_url: str,
        payment_type: str = "paid",
        source_subscription_order_id: int | None = None,
        promo_grant_id: int | None = None,
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
            protocol_key=protocol_key,
            amount_rub=amount_rub,
            status="pending_payment",
            payment_type=payment_type,
            payment_url=payment_url,
            xui_client_id=None,
            xui_email=None,
            xui_sub_id=None,
            subscription_url=None,
            config_text=None,
            inbound_id=None,
            source_subscription_order_id=source_subscription_order_id,
            promo_grant_id=promo_grant_id,
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

    def list_orders_for_user(self, telegram_id: int) -> list[OrderRecord]:
        data = self._read()
        rows = [OrderRecord(**item) for item in data["orders"] if item["telegram_id"] == telegram_id]
        return sorted(rows, key=lambda item: item.created_at, reverse=True)

    def list_all_orders(self) -> list[OrderRecord]:
        data = self._read()
        rows = [OrderRecord(**item) for item in data["orders"]]
        return sorted(rows, key=lambda item: item.created_at, reverse=True)

    def update_order_xui(
        self,
        order_id: int,
        *,
        xui_client_id: str | None,
        xui_email: str | None,
        xui_sub_id: str | None,
        subscription_url: str | None,
        config_text: str | None,
        inbound_id: int | None,
    ) -> None:
        data = self._read()
        for item in data["orders"]:
            if item["id"] == order_id:
                item["xui_client_id"] = xui_client_id
                item["xui_email"] = xui_email
                item["xui_sub_id"] = xui_sub_id
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
        protocol_key: str,
        title: str,
        status: str,
        payment_type: str,
        starts_at: str,
        ends_at: str,
        amount_rub: int,
        xui_client_id: str | None,
        xui_email: str | None,
        xui_sub_id: str | None,
        subscription_url: str | None,
        config_text: str | None,
        reminder_sent_at: str | None = None,
    ) -> SubscriptionRecord:
        data = self._read()
        now = utc_now()
        existing = next((item for item in data["subscriptions"] if item["order_id"] == order_id), None)
        if existing is None:
            data["counters"]["subscription_id"] += 1
            existing = {"id": data["counters"]["subscription_id"], "created_at": now}
            data["subscriptions"].append(existing)
        existing.update(
            {
                "telegram_id": telegram_id,
                "order_id": order_id,
                "device_key": device_key,
                "period_key": period_key,
                "protocol_key": protocol_key,
                "title": title,
                "status": status,
                "payment_type": payment_type,
                "starts_at": starts_at,
                "ends_at": ends_at,
                "amount_rub": amount_rub,
                "xui_client_id": xui_client_id,
                "xui_email": xui_email,
                "xui_sub_id": xui_sub_id,
                "subscription_url": subscription_url,
                "config_text": config_text,
                "reminder_sent_at": reminder_sent_at,
                "updated_at": now,
            }
        )
        self._write(data)
        return SubscriptionRecord(**existing)

    def get_subscription(self, subscription_id: int) -> SubscriptionRecord | None:
        data = self._read()
        for item in data["subscriptions"]:
            if item["id"] == subscription_id:
                return SubscriptionRecord(**item)
        return None

    def list_subscriptions(self, telegram_id: int) -> list[SubscriptionRecord]:
        data = self._read()
        rows = [SubscriptionRecord(**item) for item in data["subscriptions"] if item["telegram_id"] == telegram_id]
        return sorted(rows, key=lambda item: item.ends_at)

    def list_all_subscriptions(self) -> list[SubscriptionRecord]:
        data = self._read()
        rows = [SubscriptionRecord(**item) for item in data["subscriptions"]]
        return sorted(rows, key=lambda item: item.ends_at)

    def get_subscription_by_order_id(self, order_id: int) -> SubscriptionRecord | None:
        data = self._read()
        for item in data["subscriptions"]:
            if item["order_id"] == order_id:
                return SubscriptionRecord(**item)
        return None

    def set_subscription_reminder_sent(self, order_id: int, sent_at: str) -> None:
        data = self._read()
        for item in data["subscriptions"]:
            if item["order_id"] == order_id:
                item["reminder_sent_at"] = sent_at
                item["updated_at"] = utc_now()
                break
        self._write(data)

    def set_subscription_status(self, subscription_id: int, status: str) -> None:
        data = self._read()
        for item in data["subscriptions"]:
            if item["id"] == subscription_id:
                item["status"] = status
                item["updated_at"] = utc_now()
                break
        self._write(data)

    def update_subscription_access(
        self,
        subscription_id: int,
        *,
        xui_client_id: str | None,
        xui_email: str | None,
        xui_sub_id: str | None,
        subscription_url: str | None,
        config_text: str | None,
        starts_at: str | None = None,
        ends_at: str | None = None,
        protocol_key: str | None = None,
        title: str | None = None,
        reminder_sent_at: str | None = None,
    ) -> None:
        data = self._read()
        for item in data["subscriptions"]:
            if item["id"] == subscription_id:
                item["xui_client_id"] = xui_client_id
                item["xui_email"] = xui_email
                item["xui_sub_id"] = xui_sub_id
                item["subscription_url"] = subscription_url
                item["config_text"] = config_text
                if starts_at is not None:
                    item["starts_at"] = starts_at
                if ends_at is not None:
                    item["ends_at"] = ends_at
                if protocol_key is not None:
                    item["protocol_key"] = protocol_key
                if title is not None:
                    item["title"] = title
                item["reminder_sent_at"] = reminder_sent_at
                item["updated_at"] = utc_now()
                break
        self._write(data)

    def list_paid_orders(self, telegram_id: int) -> list[OrderRecord]:
        data = self._read()
        rows = [
            OrderRecord(**item)
            for item in data["orders"]
            if item["telegram_id"] == telegram_id and item["payment_type"] == "paid" and item["status"] in {"fulfilled", "approved"}
        ]
        return sorted(rows, key=lambda item: item.created_at, reverse=True)

    def create_ticket(self, telegram_id: int, topic_key: str, question_text: str) -> SupportTicketRecord:
        data = self._read()
        now = utc_now()
        data["counters"]["ticket_id"] += 1
        ticket = SupportTicketRecord(
            id=data["counters"]["ticket_id"],
            telegram_id=telegram_id,
            topic_key=topic_key,
            question_text=question_text,
            status="open",
            admin_reply=None,
            created_at=now,
            updated_at=now,
        )
        data["tickets"].append(asdict(ticket))
        self._write(data)
        return ticket

    def get_ticket(self, ticket_id: int) -> SupportTicketRecord | None:
        data = self._read()
        for item in data["tickets"]:
            if item["id"] == ticket_id:
                return SupportTicketRecord(**item)
        return None

    def answer_ticket(self, ticket_id: int, reply_text: str) -> None:
        data = self._read()
        for item in data["tickets"]:
            if item["id"] == ticket_id:
                item["status"] = "answered"
                item["admin_reply"] = reply_text
                item["updated_at"] = utc_now()
                break
        self._write(data)

    def create_promo_code(
        self,
        *,
        code: str,
        days: int,
        max_activations: int | None,
        expires_at: str | None,
    ) -> PromoCodeRecord:
        data = self._read()
        now = utc_now()
        data["counters"]["promo_id"] += 1
        promo = PromoCodeRecord(
            id=data["counters"]["promo_id"],
            code=code.strip().upper(),
            days=days,
            max_activations=max_activations,
            expires_at=expires_at,
            status="active",
            created_at=now,
            updated_at=now,
        )
        data["promo_codes"].append(asdict(promo))
        self._write(data)
        return promo

    def list_promo_codes(self) -> list[PromoCodeRecord]:
        data = self._read()
        rows = [PromoCodeRecord(**item) for item in data["promo_codes"]]
        return sorted(rows, key=lambda item: item.created_at, reverse=True)

    def get_promo_code_by_text(self, code: str) -> PromoCodeRecord | None:
        data = self._read()
        normalized = code.strip().upper()
        for item in data["promo_codes"]:
            if item["code"] == normalized:
                return PromoCodeRecord(**item)
        return None

    def get_pending_promo_grant(self, telegram_id: int) -> PromoGrantRecord | None:
        data = self._read()
        rows = [
            PromoGrantRecord(**item)
            for item in data["promo_grants"]
            if item["telegram_id"] == telegram_id and item["status"] == "pending_activation"
        ]
        if not rows:
            return None
        return sorted(rows, key=lambda item: item.created_at, reverse=True)[0]

    def list_promo_grants(self) -> list[PromoGrantRecord]:
        data = self._read()
        rows = [PromoGrantRecord(**item) for item in data["promo_grants"]]
        return sorted(rows, key=lambda item: item.created_at, reverse=True)

    def list_tickets(self) -> list[SupportTicketRecord]:
        data = self._read()
        rows = [SupportTicketRecord(**item) for item in data["tickets"]]
        return sorted(rows, key=lambda item: item.created_at, reverse=True)

    def export_snapshot(self) -> dict[str, Any]:
        return self._read()

    def claim_promo_code(self, telegram_id: int, code: str) -> tuple[PromoGrantRecord | None, str | None]:
        data = self._read()
        normalized = code.strip().upper()
        promo_row = next((item for item in data["promo_codes"] if item["code"] == normalized), None)
        if promo_row is None:
            return None, "Промокод не найден."
        promo = PromoCodeRecord(**promo_row)
        if promo.status != "active":
            return None, "Этот промокод уже не активен."
        if promo.expires_at:
            try:
                if datetime.fromisoformat(promo.expires_at) < datetime.now(timezone.utc):
                    return None, "Срок действия промокода уже закончился."
            except ValueError:
                pass
        existing_grant = next(
            (
                item
                for item in data["promo_grants"]
                if item["promo_id"] == promo.id and item["telegram_id"] == telegram_id and item["status"] in {"pending_activation", "activated"}
            ),
            None,
        )
        if existing_grant:
            return None, "Этот промокод уже активирован для вашего аккаунта."
        activations = sum(1 for item in data["promo_grants"] if item["promo_id"] == promo.id)
        if promo.max_activations is not None and activations >= promo.max_activations:
            return None, "Лимит активаций по этому промокоду уже исчерпан."
        now = utc_now()
        data["counters"]["promo_grant_id"] += 1
        grant = PromoGrantRecord(
            id=data["counters"]["promo_grant_id"],
            promo_id=promo.id,
            telegram_id=telegram_id,
            code=promo.code,
            days=promo.days,
            status="pending_activation",
            activated_order_id=None,
            created_at=now,
            updated_at=now,
        )
        data["promo_grants"].append(asdict(grant))
        self._write(data)
        return grant, None

    def activate_promo_grant(self, grant_id: int, order_id: int) -> None:
        data = self._read()
        for item in data["promo_grants"]:
            if item["id"] == grant_id:
                item["status"] = "activated"
                item["activated_order_id"] = order_id
                item["updated_at"] = utc_now()
                break
        self._write(data)
