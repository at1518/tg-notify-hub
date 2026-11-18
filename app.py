#!/usr/bin/env python3
"""Minimal zero-dependency HTTP-to-Telegram notification relay and watchdog for Northflank.

Features:
- /health: Healthcheck endpoint (no auth)
- /check?host=...&port=...: External reachability check (TCP + TLS)
- /fetch?url=...: External HTTP fetcher
- /send: HTTP-to-Telegram relay with HTML, threads and inline keyboards (reply_markup)
- /webhook: Generic event webhook forwarder
- /telegram/<method>: Transparent reverse proxy for Telegram Bot API (failover relay)
- /heartbeat: Dead-man's switch / heartbeat receiver from Homelab
- Background Watchdog: Independent external prober for VDS/infrastructure (via ENV)
"""

from __future__ import annotations

import hmac
import html
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
TG_THREAD_ID = os.environ.get("TG_THREAD_ID", "")
MAX_BODY_BYTES = 131072

# Watchdog configuration (strictly from Environment Variables, no hardcoded domains)
WATCHDOG_TARGETS = [t.strip() for t in os.environ.get("WATCHDOG_TARGETS", "").split(",") if t.strip()]
WATCHDOG_INTERVAL_SEC = int(os.environ.get("WATCHDOG_INTERVAL_SEC", "180"))
WATCHDOG_FAIL_THRESHOLD = int(os.environ.get("WATCHDOG_FAIL_THRESHOLD", "2"))
HEARTBEAT_TIMEOUT_SEC = int(os.environ.get("HEARTBEAT_TIMEOUT_SEC", "0"))

LAST_HEARTBEATS: dict[str, float] = {}
TARGET_FAIL_COUNTS: dict[str, int] = {}
TARGET_IS_DOWN: dict[str, bool] = {}


def verify_token(headers) -> bool:
    if not RELAY_TOKEN:
        return False
    auth = headers.get("Authorization", "")
    token = ""
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
    elif headers.get("X-Auth-Token"):
        token = headers.get("X-Auth-Token", "").strip()
    return bool(token) and hmac.compare_digest(token, RELAY_TOKEN)


def send_telegram(
    text: str,
    chat_id: str | None = None,
    bot_token: str | None = None,
    parse_mode: str = "HTML",
    thread_id: int | None = None,
    silent: bool = False,
    reply_markup: dict | None = None,
) -> tuple[int, dict]:
    token = (bot_token or TG_BOT_TOKEN).strip()
    target_chat = str(chat_id or TG_CHAT_ID).strip()
    if not token or not target_chat:
        return 500, {"ok": False, "error": "TG_BOT_TOKEN or TG_CHAT_ID is not configured"}

    payload: dict = {
        "chat_id": target_chat,
        "text": text[:4096],
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if silent:
        payload["disable_notification"] = True
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    elif TG_THREAD_ID.isdigit():
        payload["message_thread_id"] = int(TG_THREAD_ID)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return 200, {"ok": True, "telegram": body}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        return 502, {"ok": False, "status": exc.code, "error": err_body}
    except Exception as exc:
        return 502, {"ok": False, "error": str(exc)}


def format_webhook_message(data: dict) -> str:
    host = html.escape(str(data.get("host", "node")))
    event = html.escape(str(data.get("event", "info")))
    selector = html.escape(str(data.get("selector", "default")))
    from_node = html.escape(str(data.get("from", "?")))
    to_node = html.escape(str(data.get("to", "?")))
    ok_count = html.escape(str(data.get("ok_count", "?")))

    if event == "failover":
        return (
            f"<b>[{host}] Failover ({selector})</b>\n"
            f"<code>{from_node}</code> → <code>{to_node}</code>\n"
            f"Healthy: <b>{ok_count}</b>"
        )
    if event == "all_down":
        return f"🚨 <b>[{host}] ALL NODES DOWN</b> ({selector})"
    return (
        f"<b>[{host}] Event: {event}</b>\n"
        f"Group: <code>{selector}</code> | Target: <code>{to_node}</code> | Healthy: <b>{ok_count}</b>"
    )


def probe_target(target_str: str) -> bool:
    """Probes host:port via TCP + TLS (if port 443)."""
    parts = target_str.split(":")
    host = parts[0].strip()
    port = int(parts[1]) if len(parts) > 1 else 443
    try:
        with socket.create_connection((host, port), timeout=4):
            pass
        if port == 443:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(socket.socket(), server_hostname=host) as s:
                s.settimeout(4)
                s.connect((host, port))
        return True
    except Exception:
        return False


def watchdog_loop() -> None:
    """Background watchdog thread that checks external targets and homelab heartbeat."""
    if not WATCHDOG_TARGETS and HEARTBEAT_TIMEOUT_SEC <= 0:
        return

    print(
        f"[Watchdog] Started. Targets: {len(WATCHDOG_TARGETS)}, interval: {WATCHDOG_INTERVAL_SEC}s, "
        f"heartbeat timeout: {HEARTBEAT_TIMEOUT_SEC}s",
        flush=True,
    )

    while True:
        time.sleep(WATCHDOG_INTERVAL_SEC)
        now = time.time()

        # 1. Check external targets (VDS, etc.)
        for target in WATCHDOG_TARGETS:
            ok = probe_target(target)
            prev_fails = TARGET_FAIL_COUNTS.get(target, 0)
            was_down = TARGET_IS_DOWN.get(target, False)

            if not ok:
                new_fails = prev_fails + 1
                TARGET_FAIL_COUNTS[target] = new_fails
                if new_fails >= WATCHDOG_FAIL_THRESHOLD and not was_down:
                    TARGET_IS_DOWN[target] = True
                    send_telegram(
                        f"🚨 <b>[Northflank Watchdog] Сбой сервиса!</b>\n"
                        f"Узел <code>{html.escape(target)}</code> недоступен из Европы ({new_fails} проверок подряд)."
                    )
            else:
                TARGET_FAIL_COUNTS[target] = 0
                if was_down:
                    TARGET_IS_DOWN[target] = False
                    send_telegram(
                        f"✅ <b>[Northflank Watchdog] Восстановление!</b>\n"
                        f"Узел <code>{html.escape(target)}</code> снова отвечает."
                    )

        # 2. Check dead-man's switch / heartbeat
        if HEARTBEAT_TIMEOUT_SEC > 0:
            for source, last_ts in list(LAST_HEARTBEATS.items()):
                elapsed = now - last_ts
                down_key = f"heartbeat_{source}"
                was_down = TARGET_IS_DOWN.get(down_key, False)
                if elapsed > HEARTBEAT_TIMEOUT_SEC and not was_down:
                    TARGET_IS_DOWN[down_key] = True
                    mins = int(elapsed // 60)
                    send_telegram(
                        f"🚨 <b>[Northflank Watchdog] Потеряна связь с {html.escape(source)}!</b>\n"
                        f"Сигнал жизни (Heartbeat) отсутствует уже <b>{mins} мин</b>.\n"
                        f"Возможно, отключено электричество или упали оба интернет-провайдера дома."
                    )


class RelayHandler(BaseHTTPRequestHandler):
    def _write_json(self, status: int, data: dict) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/health":
            self._write_json(200, {"status": "ok"})
            return

        if path == "/heartbeat":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            source = (qs.get("source") or ["homelab"])[0].strip()
            LAST_HEARTBEATS[source] = time.time()
            down_key = f"heartbeat_{source}"
            if TARGET_IS_DOWN.get(down_key, False):
                TARGET_IS_DOWN[down_key] = False
                send_telegram(
                    f"✅ <b>[Northflank Watchdog] Связь с {html.escape(source)} восстановлена!</b>\n"
                    f"Heartbeat получен."
                )
            self._write_json(200, {"ok": True, "source": source, "timestamp": time.time()})
            return

        if path == "/check":
            if not verify_token(self.headers):
                self._write_json(401, {"ok": False, "error": "Unauthorized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            host = (qs.get("host") or [""])[0].strip()
            port = int((qs.get("port") or ["443"])[0])
            if not host:
                self._write_json(400, {"ok": False, "error": "host required"})
                return
            tcp_ok, tls_ok = False, False
            try:
                with socket.create_connection((host, port), timeout=4):
                    tcp_ok = True
                if port == 443:
                    ctx = ssl.create_default_context()
                    with ctx.wrap_socket(socket.socket(), server_hostname=host) as s:
                        s.settimeout(4)
                        s.connect((host, port))
                        tls_ok = True
            except Exception:
                pass
            self._write_json(200, {"ok": True, "host": host, "port": port, "tcp": tcp_ok, "tls": tls_ok})
            return

        if path == "/fetch":
            if not verify_token(self.headers):
                self._write_json(401, {"ok": False, "error": "Unauthorized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            url = (qs.get("url") or [""])[0].strip()
            if not url.startswith(("http://", "https://")):
                self._write_json(400, {"ok": False, "error": "valid http(s) url required"})
                return
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "tg-notify-hub/1.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    body = r.read(32768).decode("utf-8", errors="replace")
                    self._write_json(200, {"ok": True, "status": r.status, "body": body})
            except Exception as e:
                self._write_json(502, {"ok": False, "error": str(e)})
            return

        self._write_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]

        if not verify_token(self.headers):
            self._write_json(401, {"ok": False, "error": "Unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write_json(400, {"ok": False, "error": "Invalid Content-Length"})
            return

        if length <= 0 or length > MAX_BODY_BYTES:
            self._write_json(400, {"ok": False, "error": "Invalid payload size"})
            return

        raw_body = self.rfile.read(length)

        # 1. Telegram API Reverse Proxy / Failover relay (/telegram/<method>)
        if path.startswith("/telegram/"):
            method = path.split("/telegram/")[1].strip()
            bot_token = (self.headers.get("X-Bot-Token") or TG_BOT_TOKEN).strip()
            if not bot_token:
                self._write_json(500, {"ok": False, "error": "Bot token not configured"})
                return

            tg_url = f"https://api.telegram.org/bot{bot_token}/{method}"
            req = urllib.request.Request(
                tg_url,
                data=raw_body,
                headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp_body = resp.read()
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp_body)))
                    self.end_headers()
                    self.wfile.write(resp_body)
            except urllib.error.HTTPError as exc:
                err_data = exc.read()
                self.send_response(exc.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_data)))
                self.end_headers()
                self.wfile.write(err_data)
            except Exception as exc:
                self._write_json(502, {"ok": False, "error": str(exc)})
            return

        # 2. Standard endpoints: /send and /webhook
        if path not in ("/send", "/webhook"):
            self._write_json(404, {"ok": False, "error": "Not found"})
            return

        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self._write_json(400, {"ok": False, "error": "Invalid JSON"})
            return

        if not isinstance(data, dict):
            self._write_json(400, {"ok": False, "error": "JSON object expected"})
            return

        if path == "/webhook":
            text = format_webhook_message(data)
            parse_mode = "HTML"
            reply_markup = None
        else:
            raw_text = data.get("text")
            if not raw_text or not isinstance(raw_text, str):
                self._write_json(400, {"ok": False, "error": "Field 'text' is required"})
                return
            source = data.get("source")
            parse_mode = data.get("parse_mode", "HTML")
            reply_markup = data.get("reply_markup")
            if source:
                prefix = f"<b>[{html.escape(str(source))}]</b> " if parse_mode == "HTML" else f"[{source}] "
                text = prefix + raw_text
            else:
                text = raw_text

        status, result = send_telegram(
            text=text,
            chat_id=data.get("chat_id"),
            bot_token=data.get("bot_token"),
            parse_mode=parse_mode,
            thread_id=data.get("thread_id"),
            silent=bool(data.get("silent", False)),
            reply_markup=reply_markup,
        )
        self._write_json(status, result)


if __name__ == "__main__":
    # Start background watchdog if targets or heartbeat configured
    if WATCHDOG_TARGETS or HEARTBEAT_TIMEOUT_SEC > 0:
        t = threading.Thread(target=watchdog_loop, daemon=True)
        t.start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), RelayHandler)
    print(f"tg-notify-hub listening on :{PORT}", flush=True)
    server.serve_forever()
