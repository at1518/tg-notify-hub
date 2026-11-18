# tg-notify-hub

Лёгкий HTTP → Telegram ретранслятор, прокси и сторож (watchdog) для деплоя на Northflank (0 внешних зависимостей, ~15–20 МБ RAM).

## Переменные окружения в Northflank

- `PORT` — `8080` (по умолчанию)
- `RELAY_TOKEN` — секретный токен для заголовка `Authorization: Bearer <token>` (или `X-Auth-Token`)
- `TG_BOT_TOKEN` — токен Telegram-бота по умолчанию
- `TG_CHAT_ID` — chat_id по умолчанию (личный ID или ID группы/канала)
- `TG_THREAD_ID` — (опционально) ID топика в супергруппе

### Опциональные переменные внешнего сторожа (Watchdog)

- `WATCHDOG_TARGETS` — список хостов для внешней проверки через запятую (например: `vds1.example.com:443,vds2.example.com:443,site.example.com:443`). По умолчанию выключено.
- `WATCHDOG_INTERVAL_SEC` — интервал проверок в секундах (по умолчанию `180` = 3 мин).
- `WATCHDOG_FAIL_THRESHOLD` — количество неудачных проверок подряд перед отправкой алерта в Telegram (по умолчанию `2`).
- `HEARTBEAT_TIMEOUT_SEC` — таймаут сигнала жизни (dead man's switch) в секундах. Если `> 0` (например, `600` = 10 мин) и домашний сервер не присылает `GET /heartbeat`, бот пришлет алерт о потере связи с домом. По умолчанию выключено (`0`).

---

## Эндпоинты

### 1. Healthcheck
- `GET /health` — проверка работы сервиса (без авторизации, `{"status": "ok"}`).

### 2. Сигнал жизни (Dead Man's Switch)
- `GET /heartbeat?source=homelab` — обновление таймера жизни домашнего сервера.

### 3. Внешняя проверка хостов
- `GET /check?host=example.com&port=443` — проверка TCP и TLS соединения из Европы.

### 4. Внешний HTTP Fetcher
- `GET /fetch?url=https://example.com` — выполнение внешнего HTTP-запроса.

### 5. Отправка сообщений (`POST /send`)
Поддерживает HTML, тихие сообщения и inline-кнопки (`reply_markup`):

```sh
curl -X POST https://your-service.example.com/send \
  -H "Authorization: Bearer YOUR_RELAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "server",
    "text": "Бэкап завершён успешно",
    "reply_markup": {
      "inline_keyboard": [
        [{"text": "🔄 Перезапуск", "callback_data": "restart"}]
      ]
    }
  }'
```

### 6. Прозрачный прокси Telegram Bot API (`POST /telegram/<method>`)
Позволяет локальным ботам обращаться к Telegram через Northflank в обход блокировок `api.telegram.org`:

```sh
curl -X POST https://your-service.example.com/telegram/sendMessage \
  -H "Authorization: Bearer YOUR_RELAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"chat_id": "12345678", "text": "Привет через прокси"}'
```
