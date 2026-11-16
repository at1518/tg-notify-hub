#!/usr/bin/env python3
"""Minimal zero-dependency HTTP-to-Telegram notification relay for Northflank."""

from __future__ import annotations

import hmac
import html
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
TG_THREAD_ID = os.environ.get("TG_THREAD_ID", "")
MAX_BODY_BYTES = 65536


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


class RelayHandler(BaseHTTPRequestHandler):
    def _write_json(self, status: int, data: dict) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args) -> None:
        # Compact stdout logging for container logs
        print(f"{self.address_string()} - {format % args}", flush=True)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        if path == "/check":
            if not verify_token(self.headers):
                self._write_json(401, {"ok": False, "error": "Unauthorized"})
                return
            import socket, ssl, urllib.parse
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
        self._write_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path not in ("/send", "/webhook"):
            self._write_json(404, {"ok": False, "error": "Not found"})
            return

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

        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._write_json(400, {"ok": False, "error": "Invalid JSON"})
            return

        if not isinstance(data, dict):
            self._write_json(400, {"ok": False, "error": "JSON object expected"})
            return

        if path == "/webhook":
            text = format_webhook_message(data)
            parse_mode = "HTML"
        else:
            raw_text = data.get("text")
            if not raw_text or not isinstance(raw_text, str):
                self._write_json(400, {"ok": False, "error": "Field 'text' is required"})
                return
            source = data.get("source")
            parse_mode = data.get("parse_mode", "HTML")
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
        )
        self._write_json(status, result)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), RelayHandler)
    print(f"tg-notify-hub listening on :{PORT}", flush=True)
    server.serve_forever()
