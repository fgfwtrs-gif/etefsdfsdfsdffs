# Запуск и настройка

## 1. Что заполнить

1. Скопируй `.env.example` в `.env`
2. Укажи:
   - `BOT_TOKEN`
   - `ADMIN_CHAT_IDS`
   - `PAYMENT_URL`
   - `SUPPORT_URL`
   - данные панели `3x-ui`
3. Открой `config.toml` и проверь:
   - тексты бота
   - тарифы в `[plans.*]`
   - протоколы в `[protocols.*]`
   - `inbound_id` для каждого протокола
   - `client_template_json` и `access_template`

## 2. Что важно по 3x-ui

- Бот использует методы `addClient`, `updateClient`, `delClient`
- Для роутера можно включить несколько протоколов, но каждый должен быть связан со своим `inbound_id`
- Поля клиента в `client_template_json` должны совпадать с типом inbound в твоей панели
- Шаблон выдачи `access_template` тоже должен соответствовать реальному протоколу

## 3. Локальный запуск на Windows

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m src.vpn_shop_bot
```

## 4. Запуск на Ubuntu VPS

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
git clone <твой-репозиторий> vpn-bot
cd vpn-bot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Дальше:

1. Заполни `.env`
2. Проверь `config.toml`
3. Положи стартовую картинку в `assets/start.png`
4. Запусти:

```bash
source .venv/bin/activate
python -m src.vpn_shop_bot
```

## 5. Systemd сервис на Ubuntu

Создай сервис:

```bash
sudo nano /etc/systemd/system/vpn-shop-bot.service
```

Содержимое:

```ini
[Unit]
Description=Telegram VPN Shop Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/vpn-bot
Environment="PYTHONUNBUFFERED=1"
ExecStart=/root/vpn-bot/.venv/bin/python -m src.vpn_shop_bot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Запуск:

```bash
sudo systemctl daemon-reload
sudo systemctl enable vpn-shop-bot
sudo systemctl start vpn-shop-bot
sudo systemctl status vpn-shop-bot
```

Логи:

```bash
journalctl -u vpn-shop-bot -f
```

## 6. Как подключить к твоей панели

1. В `3x-ui` создай inbound для каждого нужного протокола
2. Сохрани их `id`
3. Пропиши эти `id` в `config.toml` внутри `[protocols.<name>]`
4. Подставь реальные параметры сервера:
   - `server_address`
   - `server_port`
   - `public_key`
   - `short_id`
   - `server_name`
   - `spider_x`
5. Включи `XUI_ENABLED=true` в `.env`
6. Укажи `XUI_BASE_URL`, `XUI_USERNAME`, `XUI_PASSWORD` или `XUI_API_TOKEN`

## 7. Логика оплаты

- Пользователь создаёт заказ
- Бот показывает ссылку оплаты Ozon Bank / СБП
- После кнопки `Я оплатил` админу приходит заявка
- Админ подтверждает или отклоняет оплату
- После подтверждения бот включает клиента в `3x-ui` и отправляет конфиг пользователю

## 8. Ограничение

Сам бот умеет делать ручную модерацию оплаты. Автоматической проверки перевода по банку здесь нет. Если захочешь полную автоматизацию, следующим шагом нужно подключать платёжку с webhook/API.
