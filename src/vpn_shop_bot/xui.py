from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any
from uuid import uuid4

import requests

from .config import SalesConfig, XuiConfig


class XuiError(RuntimeError):
    pass


@dataclass(slots=True)
class ProvisionedClient:
    client_id: str
    email: str
    inbound_id: int
    subscription_url: str
    config_text: str
    title: str


class XuiClient:
    def __init__(self, config: XuiConfig, sales: SalesConfig) -> None:
        self.config = config
        self.sales = sales
        self.session = requests.Session()
        self.session.verify = config.verify_tls
        if self.config.api_token:
            self.session.headers["Authorization"] = f"Bearer {self.config.api_token}"

    @property
    def enabled(self) -> bool:
        return self.config.enabled and bool(self.config.base_url)

    def ensure_auth(self) -> None:
        if not self.enabled or self.config.api_token:
            return
        response = self.session.post(
            f"{self.config.base_url}{self.config.login_path}",
            data={"username": self.config.username, "password": self.config.password},
            timeout=20,
        )
        if not response.ok:
            raise XuiError(f"3x-ui login failed: {response.status_code} {response.text}")

    def add_client(
        self,
        *,
        inbound_id: int,
        tg_id: int,
        order_code: str,
        period_title: str,
        months: int,
        device_title: str,
        price_rub: int,
        enabled: bool,
    ) -> ProvisionedClient:
        self.ensure_auth()
        client_id = str(uuid4())
        email = f"{self.sales.remark_prefix}_{tg_id}_{order_code}".lower()
        sub_id = str(uuid4())
        remark = f"{self.sales.remark_prefix}-{tg_id}-{device_title}"
        expiry_time = self._expiry_time_ms(months)
        client_payload = {
            "clients": [
                {
                    "id": client_id,
                    "flow": self.sales.client_flow,
                    "email": email,
                    "limitIp": self.sales.limit_ip,
                    "totalGB": self.sales.traffic_limit_gb * 1024 * 1024 * 1024,
                    "expiryTime": expiry_time,
                    "enable": enabled,
                    "tgId": str(tg_id),
                    "subId": sub_id,
                    "comment": f"{device_title} | {period_title} | {price_rub} RUB | {order_code}",
                    "reset": self.sales.reset,
                }
            ]
        }
        response = self.session.post(
            f"{self.config.base_url}/panel/api/inbounds/addClient",
            json={"id": inbound_id, "settings": json.dumps(client_payload, ensure_ascii=False)},
            timeout=20,
        )
        self._ensure_success(response, "addClient")
        subscription_url = self.sales.subscription_url_template.format(sub_id=sub_id, email=email, client_id=client_id)
        config_text = self.sales.config_template.format(
            client_id=client_id,
            server_address=self.sales.server_address,
            server_port=self.sales.server_port,
            security=self.sales.client_security,
            public_key=self.sales.public_key,
            server_name=self.sales.server_name,
            short_id=self.sales.short_id,
            spider_x=self.sales.spider_x,
            flow=self.sales.client_flow,
            remark=remark,
            email=email,
            sub_id=sub_id,
        )
        return ProvisionedClient(
            client_id=client_id,
            email=email,
            inbound_id=inbound_id,
            subscription_url=subscription_url,
            config_text=config_text,
            title=remark,
        )

    def update_client(
        self,
        *,
        inbound_id: int,
        client_id: str,
        email: str,
        tg_id: int,
        order_code: str,
        period_title: str,
        months: int,
        device_title: str,
        price_rub: int,
        enabled: bool,
    ) -> None:
        self.ensure_auth()
        response = self.session.post(
            f"{self.config.base_url}/panel/api/inbounds/updateClient/{client_id}",
            json={
                "id": inbound_id,
                "settings": json.dumps(
                    {
                        "clients": [
                            {
                                "id": client_id,
                                "flow": self.sales.client_flow,
                                "email": email,
                                "limitIp": self.sales.limit_ip,
                                "totalGB": self.sales.traffic_limit_gb * 1024 * 1024 * 1024,
                                "expiryTime": self._expiry_time_ms(months),
                                "enable": enabled,
                                "tgId": str(tg_id),
                                "comment": f"{device_title} | {period_title} | {price_rub} RUB | {order_code}",
                                "reset": self.sales.reset,
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            },
            timeout=20,
        )
        self._ensure_success(response, "updateClient")

    def delete_client(self, *, inbound_id: int, client_id: str) -> None:
        self.ensure_auth()
        response = self.session.post(
            f"{self.config.base_url}/panel/api/inbounds/{inbound_id}/delClient/{client_id}",
            timeout=20,
        )
        self._ensure_success(response, "delClient")

    def _ensure_success(self, response: requests.Response, action: str) -> None:
        if not response.ok:
            raise XuiError(f"3x-ui {action} failed: {response.status_code} {response.text}")
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            return
        if payload.get("success") is False:
            raise XuiError(f"3x-ui {action} error: {payload}")

    @staticmethod
    def _expiry_time_ms(months: int) -> int:
        now = datetime.now(timezone.utc)
        new_month = now.month - 1 + months
        year = now.year + new_month // 12
        month = new_month % 12 + 1
        day = min(now.day, 28)
        future = now.replace(year=year, month=month, day=day)
        return int(future.timestamp() * 1000)
