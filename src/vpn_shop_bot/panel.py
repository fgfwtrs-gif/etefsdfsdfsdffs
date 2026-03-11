from __future__ import annotations

from base64 import b64encode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import requests

from .settings import DeviceConfig, PlanConfig, ProtocolConfig, SalesConfig, XuiConfig


class PanelError(RuntimeError):
    pass


@dataclass(slots=True)
class ProvisionedAccess:
    client_id: str
    email: str
    sub_id: str
    inbound_id: int
    protocol_key: str
    subscription_url: str
    config_text: str
    title: str


class XuiPanel:
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
            json={"username": self.config.username, "password": self.config.password},
            timeout=20,
        )
        if not response.ok:
            raise PanelError(f"3x-ui login failed: {response.status_code} {response.text}")

    def add_client(
        self,
        *,
        protocol: ProtocolConfig,
        device: DeviceConfig,
        plan: PlanConfig,
        telegram_id: int,
        username: str | None,
        order_code: str,
        enabled: bool,
        duration_days: int | None = None,
        base_ends_at: str | None = None,
        absolute_ends_at: str | None = None,
    ) -> ProvisionedAccess:
        if protocol.key == "wireguard":
            return self._add_wireguard_client(
                protocol=protocol,
                device=device,
                plan=plan,
                telegram_id=telegram_id,
                username=username,
                order_code=order_code,
            )
        client_id = str(uuid4())
        sub_id = str(uuid4())
        email = self._build_email(
            telegram_id=telegram_id,
            username=username,
            device=device,
            protocol=protocol,
            client_id=client_id,
        )
        payload_client = self._render_client_payload(
            protocol=protocol,
            client_id=client_id,
            sub_id=sub_id,
            email=email,
            enabled=enabled,
            telegram_id=telegram_id,
            device=device,
            plan=plan,
            order_code=order_code,
            duration_days=duration_days,
            base_ends_at=base_ends_at,
            absolute_ends_at=absolute_ends_at,
        )
        if self.enabled:
            self.ensure_auth()
            response = self.session.post(
                f"{self.config.base_url}/panel/api/inbounds/addClient",
                json={
                    "id": self._select_inbound_id(protocol, device),
                    "settings": json.dumps({"clients": [payload_client]}, ensure_ascii=False),
                },
                timeout=20,
            )
            self._ensure_success(response, "addClient")
        return self._build_access(
            protocol=protocol,
            device=device,
            plan=plan,
            order_code=order_code,
            telegram_id=telegram_id,
            client_id=client_id,
            email=email,
            sub_id=sub_id,
        )

    def update_client(
        self,
        *,
        protocol: ProtocolConfig,
        device: DeviceConfig,
        plan: PlanConfig,
        telegram_id: int,
        username: str | None,
        order_code: str,
        enabled: bool,
        client_id: str,
        email: str,
        sub_id: str,
        duration_days: int | None = None,
        base_ends_at: str | None = None,
        absolute_ends_at: str | None = None,
    ) -> ProvisionedAccess:
        if protocol.key == "wireguard":
            return self._update_wireguard_client(
                protocol=protocol,
                device=device,
                plan=plan,
                telegram_id=telegram_id,
                username=username,
                order_code=order_code,
                client_id=client_id,
                email=email,
                sub_id=sub_id,
            )
        payload_client = self._render_client_payload(
            protocol=protocol,
            client_id=client_id,
            sub_id=sub_id,
            email=email,
            enabled=enabled,
            telegram_id=telegram_id,
            device=device,
            plan=plan,
            order_code=order_code,
            duration_days=duration_days,
            base_ends_at=base_ends_at,
            absolute_ends_at=absolute_ends_at,
        )
        if self.enabled:
            self.ensure_auth()
            response = self.session.post(
                f"{self.config.base_url}/panel/api/inbounds/updateClient/{client_id}",
                json={
                    "id": self._select_inbound_id(protocol, device),
                    "settings": json.dumps({"clients": [payload_client]}, ensure_ascii=False),
                },
                timeout=20,
            )
            self._ensure_success(response, "updateClient")
        return self._build_access(
            protocol=protocol,
            device=device,
            plan=plan,
            order_code=order_code,
            telegram_id=telegram_id,
            client_id=client_id,
            email=email,
            sub_id=sub_id,
        )

    def delete_client(self, *, inbound_id: int, client_id: str) -> None:
        if not self.enabled:
            return
        inbound = self._get_inbound(inbound_id)
        if inbound.get("protocol") == "wireguard":
            self._delete_wireguard_peer(inbound, client_id)
            return
        self.ensure_auth()
        response = self.session.post(
            f"{self.config.base_url}/panel/api/inbounds/{inbound_id}/delClient/{client_id}",
            timeout=20,
        )
        self._ensure_success(response, "delClient")

    def _add_wireguard_client(
        self,
        *,
        protocol: ProtocolConfig,
        device: DeviceConfig,
        plan: PlanConfig,
        telegram_id: int,
        username: str | None,
        order_code: str,
    ) -> ProvisionedAccess:
        client_id = str(uuid4())
        sub_id = str(uuid4())
        email = self._build_email(
            telegram_id=telegram_id,
            username=username,
            device=device,
            protocol=protocol,
            client_id=client_id,
        )
        private_key, public_key = self._generate_wireguard_keypair()
        inbound_id = self._select_inbound_id(protocol, device)
        inbound = self._get_inbound(inbound_id)
        settings = self._parse_json_field(inbound.get("settings"))
        peers = list(settings.get("peers", []))
        client_address = self._next_wireguard_address(peers)
        peers.append(
            {
                "privateKey": private_key,
                "publicKey": public_key,
                "allowedIPs": [client_address],
                "keepAlive": 0,
            }
        )
        settings["peers"] = peers
        self._update_inbound(inbound, settings)
        return self._build_access(
            protocol=protocol,
            device=device,
            plan=plan,
            order_code=order_code,
            telegram_id=telegram_id,
            client_id=public_key,
            email=email,
            sub_id=sub_id,
            client_private_key=private_key,
            client_public_key=public_key,
            client_address=client_address,
        )

    def _update_wireguard_client(
        self,
        *,
        protocol: ProtocolConfig,
        device: DeviceConfig,
        plan: PlanConfig,
        telegram_id: int,
        username: str | None,
        order_code: str,
        client_id: str,
        email: str,
        sub_id: str,
    ) -> ProvisionedAccess:
        inbound_id = self._select_inbound_id(protocol, device)
        inbound = self._get_inbound(inbound_id)
        settings = self._parse_json_field(inbound.get("settings"))
        peers = list(settings.get("peers", []))
        matched = next((peer for peer in peers if peer.get("publicKey") == client_id), None)
        if matched is None:
            private_key, public_key = self._generate_wireguard_keypair()
            client_address = self._next_wireguard_address(peers)
            matched = {
                "privateKey": private_key,
                "publicKey": public_key,
                "allowedIPs": [client_address],
                "keepAlive": 0,
            }
            peers.append(matched)
            settings["peers"] = peers
            self._update_inbound(inbound, settings)
            client_id = public_key
        else:
            private_key = matched.get("privateKey", "")
            public_key = matched.get("publicKey", client_id)
            allowed_ips = matched.get("allowedIPs", []) or []
            client_address = allowed_ips[0] if allowed_ips else self._next_wireguard_address(peers)
        return self._build_access(
            protocol=protocol,
            device=device,
            plan=plan,
            order_code=order_code,
            telegram_id=telegram_id,
            client_id=client_id,
            email=email,
            sub_id=sub_id,
            client_private_key=private_key,
            client_public_key=public_key,
            client_address=client_address,
        )

    def _build_access(
        self,
        *,
        protocol: ProtocolConfig,
        device: DeviceConfig,
        plan: PlanConfig,
        order_code: str,
        telegram_id: int,
        client_id: str,
        email: str,
        sub_id: str,
        client_private_key: str = "",
        client_public_key: str = "",
        client_address: str = "",
    ) -> ProvisionedAccess:
        password = client_id
        remark = self._build_remark(telegram_id=telegram_id, device=device, protocol=protocol)
        raw_subscription_url = self.sales.subscription_url_template.format(sub_id=sub_id, email=email, client_id=client_id)
        subscription_title = self._build_subscription_title(device)
        subscription_url = f"{raw_subscription_url}#{quote(subscription_title, safe='')}"
        vmess_payload = {
            "v": "2",
            "ps": remark,
            "add": self.sales.server_address,
            "port": str(self.sales.server_port),
            "id": client_id,
            "aid": "0",
            "scy": "auto",
            "net": "tcp",
            "type": "none",
            "host": "",
            "path": "",
            "tls": "",
            "sni": self.sales.server_name,
        }
        context = {
            "client_id": client_id,
            "password": password,
            "email": email,
            "sub_id": sub_id,
            "remark": remark,
            "server_address": self.sales.server_address,
            "server_port": self.sales.server_port,
            "public_key": self.sales.public_key,
            "short_id": self.sales.short_id,
            "server_name": self.sales.server_name,
            "spider_x": self.sales.spider_x,
            "ss_base64": urlsafe_b64encode(f"{protocol.method}:{password}".encode()).decode().rstrip("="),
            "vmess_b64": b64encode(json.dumps(vmess_payload, ensure_ascii=False).encode()).decode(),
            "client_private_key": client_private_key,
            "client_public_key": client_public_key,
            "client_address": client_address,
        }
        return ProvisionedAccess(
            client_id=client_id,
            email=email,
            sub_id=sub_id,
            inbound_id=self._select_inbound_id(protocol, device),
            protocol_key=protocol.key,
            subscription_url=subscription_url,
            config_text=protocol.access_template.format(**context),
            title=f"{device.title} • {plan.title} • {protocol.title}",
        )

    def _render_client_payload(
        self,
        *,
        protocol: ProtocolConfig,
        client_id: str,
        sub_id: str,
        email: str,
        enabled: bool,
        telegram_id: int,
        device: DeviceConfig,
        plan: PlanConfig,
        order_code: str,
        duration_days: int | None = None,
        base_ends_at: str | None = None,
        absolute_ends_at: str | None = None,
    ) -> dict[str, Any]:
        comment = f"{device.title} | {plan.title} | {protocol.title} | {order_code}"
        total_bytes = self.sales.traffic_limit_gb * 1024 * 1024 * 1024
        replacements = {
            "json_client_id": json.dumps(client_id, ensure_ascii=False),
            "json_password": json.dumps(client_id, ensure_ascii=False),
            "json_email": json.dumps(email, ensure_ascii=False),
            "json_sub_id": json.dumps(sub_id, ensure_ascii=False),
            "json_enabled": "true" if enabled else "false",
            "json_tg_id": json.dumps(str(telegram_id), ensure_ascii=False),
            "json_comment": json.dumps(comment, ensure_ascii=False),
            "json_method": json.dumps(protocol.method, ensure_ascii=False),
            "expiry_time": self._expiry_time_ms(
                plan.months,
                duration_days=duration_days,
                base_ends_at=base_ends_at,
                absolute_ends_at=absolute_ends_at,
            ),
            "limit_ip": self.sales.limit_ip,
            "total_bytes": total_bytes,
            "reset": self.sales.reset,
        }
        try:
            rendered = protocol.client_template_json
            for key, value in replacements.items():
                rendered = rendered.replace(f"{{{key}}}", str(value))
            return json.loads(rendered)
        except Exception as exc:
            raise PanelError(f"Invalid client template for protocol {protocol.key}: {exc}") from exc

    def _select_inbound_id(self, protocol: ProtocolConfig, device: DeviceConfig) -> int:
        return int(protocol.inbound_id or device.inbound_id or self.sales.default_inbound_id)

    def _ensure_success(self, response: requests.Response, action: str) -> None:
        if not response.ok:
            raise PanelError(f"3x-ui {action} failed: {response.status_code} {response.text}")
        try:
            payload = response.json()
        except ValueError:
            return
        if payload.get("success") is False:
            raise PanelError(f"3x-ui {action} error: {payload}")

    def _get_inbound(self, inbound_id: int) -> dict[str, Any]:
        if not self.enabled:
            raise PanelError("3x-ui is disabled")
        self.ensure_auth()
        response = self.session.get(f"{self.config.base_url}/panel/api/inbounds/get/{inbound_id}", timeout=20)
        self._ensure_success(response, "getInbound")
        payload = response.json()
        obj = payload.get("obj")
        if not isinstance(obj, dict):
            raise PanelError(f"3x-ui getInbound error: {payload}")
        return obj

    def _update_inbound(self, inbound: dict[str, Any], settings: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self.ensure_auth()
        inbound_id = int(inbound["id"])
        payload = {
            "up": int(inbound.get("up", 0)),
            "down": int(inbound.get("down", 0)),
            "total": int(inbound.get("total", 0)),
            "remark": inbound.get("remark", ""),
            "enable": bool(inbound.get("enable", True)),
            "expiryTime": int(inbound.get("expiryTime", 0)),
            "listen": inbound.get("listen", ""),
            "port": int(inbound.get("port", 0)),
            "protocol": inbound.get("protocol", ""),
            "settings": json.dumps(settings, ensure_ascii=False),
            "streamSettings": inbound.get("streamSettings", ""),
            "sniffing": inbound.get("sniffing", ""),
            "allocate": inbound.get("allocate", ""),
        }
        response = self.session.post(f"{self.config.base_url}/panel/api/inbounds/update/{inbound_id}", json=payload, timeout=20)
        self._ensure_success(response, "updateInbound")

    def _delete_wireguard_peer(self, inbound: dict[str, Any], client_id: str) -> None:
        settings = self._parse_json_field(inbound.get("settings"))
        peers = list(settings.get("peers", []))
        filtered = [peer for peer in peers if peer.get("publicKey") != client_id]
        if len(filtered) == len(peers):
            return
        settings["peers"] = filtered
        self._update_inbound(inbound, settings)

    @staticmethod
    def _parse_json_field(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value:
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass
        return {}

    @staticmethod
    def _next_wireguard_address(peers: list[dict[str, Any]]) -> str:
        used: set[int] = set()
        base_prefix = "10.0.0."
        for peer in peers:
            for raw in peer.get("allowedIPs", []) or []:
                if isinstance(raw, str) and raw.startswith(base_prefix):
                    host = raw.split("/", 1)[0].split(".")[-1]
                    if host.isdigit():
                        used.add(int(host))
        for candidate in range(2, 255):
            if candidate not in used:
                return f"{base_prefix}{candidate}/32"
        raise PanelError("No free WireGuard IP addresses left in 10.0.0.0/24")

    @staticmethod
    def _generate_wireguard_keypair() -> tuple[str, str]:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        except ImportError as exc:
            raise PanelError("WireGuard requires package 'cryptography'. Run pip install -r requirements.txt") from exc
        private_key = X25519PrivateKey.generate()
        public_key = private_key.public_key()
        private_raw = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_raw = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return b64encode(private_raw).decode(), b64encode(public_raw).decode()

    @staticmethod
    def _expiry_time_ms(
        months: int,
        *,
        duration_days: int | None = None,
        base_ends_at: str | None = None,
        absolute_ends_at: str | None = None,
    ) -> int:
        now = datetime.now(timezone.utc)
        if absolute_ends_at:
            try:
                parsed = datetime.fromisoformat(absolute_ends_at)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return int(parsed.astimezone(timezone.utc).timestamp() * 1000)
            except ValueError:
                pass
        if base_ends_at:
            try:
                parsed = datetime.fromisoformat(base_ends_at)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed > now:
                    now = parsed.astimezone(timezone.utc)
            except ValueError:
                pass
        if duration_days is not None:
            future = now + timedelta(days=duration_days)
            return int(future.timestamp() * 1000)
        new_month = now.month - 1 + months
        year = now.year + new_month // 12
        month = new_month % 12 + 1
        day = min(now.day, 28)
        future = now.replace(year=year, month=month, day=day)
        return int(future.timestamp() * 1000)

    @staticmethod
    def _sanitize_username(username: str | None) -> str:
        if not username:
            return "nouser"
        cleaned = "".join(ch.lower() if ch.isalnum() or ch in {"_", "-"} else "_" for ch in username)
        return cleaned.strip("_") or "nouser"

    def _build_email(
        self,
        *,
        telegram_id: int,
        username: str | None,
        device: DeviceConfig,
        protocol: ProtocolConfig,
        client_id: str,
    ) -> str:
        _ = telegram_id
        _ = username
        _ = protocol
        device_alias = self._device_alias(device).lower()
        unique_suffix = client_id.split("-", 1)[0][:6].lower()
        return f"halyava-{device_alias}-{unique_suffix}"

    @staticmethod
    def _device_alias(device: DeviceConfig) -> str:
        aliases = {
            "pc": "PC",
            "ios": "IOS",
            "android": "ANDROID",
            "smarttv": "SMARTTV",
            "router": "ROUTER",
        }
        return aliases.get(device.key, device.key.upper())

    def _build_remark(self, *, telegram_id: int, device: DeviceConfig, protocol: ProtocolConfig) -> str:
        _ = telegram_id
        _ = protocol
        return self._build_subscription_title(device)

    @staticmethod
    def _device_profile_name(device: DeviceConfig) -> str:
        names = {
            "pc": "💻 ПК / ноутбук",
            "ios": "📱 iPhone / iPad",
            "android": "🤖 Android",
            "smarttv": "📺 Smart TV",
            "router": "📡 Роутер",
        }
        return names.get(device.key, device.title)

    def _build_subscription_title(self, device: DeviceConfig) -> str:
        return f"🔥 Халява VPN • {self._device_profile_name(device)}"
