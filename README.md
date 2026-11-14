# tg-notify-hub

Лёгкий HTTP → Telegram ретранслятор для деплоя на Northflank (0 внешних зависимостей, ~15–20 МБ RAM).

## Переменные окружения в Northflank

- `PORT` — `8080` (по умолчанию)
- `RELAY_TOKEN` — секретный токен для заголовка `Authorization: Bearer <token>` (или `X-Auth-Token`)
- `TG_BOT_TOKEN` — токен Telegram-бота по умолчанию
- `TG_CHAT_ID` — chat_id по умолчанию (личный ID или ID группы/канала)
- `TG_THREAD_ID` — (опционально) ID топика в супергруппе

Healthcheck: `GET /health` (порт `8080`, без авторизации).

## Пример вызова (`POST /send`)

```sh
curl -X POST https://your-service.example.com/send \
  -H "Authorization: Bearer YOUR_RELAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"source":"server","text":"Бэкап завершён успешно"}'
```
