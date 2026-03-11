# Telegram VPN Bot for 3x-ui

Актуальный порядок настройки и запуска смотри в [DEPLOY.md](/c:/Users/plato/OneDrive/Desktop/chat%20gpt%20bot/red%20new/DEPLOY.md).

Telegram-бот для продажи VPN-подписок с выдачей конфига через `3x-ui`.

Что реализовано:

- главное меню с кнопками `Купить VPN`, `Профиль`, `Поддержка`
- стартовое сообщение с картинкой из `assets/start.png`
- покупка: устройство -> период -> заказ -> оплата -> выдача доступа
- единый `config.toml` для цен, сроков, текста, ссылок и параметров `3x-ui`
- JSON база: пользователи, заказы, подписки
- отмена заказа с очисткой локального состояния
- история оплат и повторная выдача сохранённого конфига из профиля
- интеграция с `3x-ui` через HTTP API с авторизацией по cookie или Bearer token

## Быстрый старт

1. Создай виртуальное окружение и установи зависимости:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. Скопируй `config.example.toml` в `config.toml`.

3. Заполни:

- `[bot].token`
- `[payments].payment_url`
- `[branding].support_url`
- `[xui]` секцию для подключения к панели
- цены и сроки в `[plans.*]`

4. Положи стартовую картинку в `assets/start.png`.

5. Запусти бота:

```powershell
python -m src.vpn_shop_bot
```

## Секция `[xui]`

Добавь в `config.toml`:

```toml
[xui]
enabled = true
base_url = "https://YOUR_PANEL_DOMAIN"
username = "admin"
password = "strong_password"
api_token = ""
login_path = "/login"
verify_tls = true
```

Если используешь токен, заполни `api_token`, а `username/password` можно не использовать.

## Важно по 3x-ui

- Бот делает ставку на API-методы `addClient`, `updateClient` и `delClient`.
- Для предпросоздания заказа клиент создаётся сразу после выбора тарифа и отключается до подтверждения оплаты.
- При отмене заказа черновой клиент удаляется из `3x-ui`.
- Если `[xui].enabled = false`, бот продолжит работу в демо-режиме и будет сохранять псевдо-конфиг без реального provisioning.

## Ограничения текущей версии

- подтверждение оплаты сейчас либо по кнопке `Я оплатил`, либо через ручную проверку админом
- автоматическая верификация банка/эквайринга не подключена
- шаблон `config_template` в примере рассчитан на `VLESS + Reality`; если у тебя другой inbound, поменяй его в конфиге
