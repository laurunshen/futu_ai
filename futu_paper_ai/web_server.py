from __future__ import annotations

import json
import math
import mimetypes
import socket
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .auto_trader import AutoTrader
from .config import AppConfig, PROJECT_ROOT, public_config
from .futu_client import FutuPaperClient, _load_futu
from .models import OrderIntent
from .watchlist import load_watchlist


STATIC_ROOT = PROJECT_ROOT / "web"
DECISION_LOG_ROOT = PROJECT_ROOT / "data" / "decisions"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "item"):
        return _jsonable(value.item())
    return str(value)


def _doctor_payload(config: AppConfig) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        futu = _load_futu(config.use_system_home)
        checks.append(
            {
                "name": "futu-api",
                "ok": True,
                "details": {
                    "ret_ok": futu.RET_OK,
                    "trd_env": futu.TrdEnv.SIMULATE,
                    "markets": [futu.TrdMarket.US, futu.TrdMarket.HK, futu.TrdMarket.CN],
                },
            }
        )
    except Exception as exc:
        checks.append({"name": "futu-api", "ok": False, "error": str(exc)})

    target = f"{config.opend_host}:{config.opend_port}"
    try:
        with socket.create_connection((config.opend_host, config.opend_port), timeout=2.0):
            pass
        checks.append({"name": "OpenD", "ok": True, "target": target})
    except OSError as exc:
        checks.append({"name": "OpenD", "ok": False, "target": target, "error": str(exc)})

    checks.append({"name": "Paper", "ok": True, "details": "TrdEnv.SIMULATE"})
    return {"ok": all(check["ok"] for check in checks), "config": public_config(config), "checks": checks}


def _read_decisions(limit: int) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    if not DECISION_LOG_ROOT.exists():
        return []

    entries: deque[dict[str, Any]] = deque(maxlen=limit)
    for log_path in sorted(DECISION_LOG_ROOT.glob("*.jsonl")):
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entry.setdefault("log_date", log_path.stem)
                entries.append(entry)
    return list(reversed(entries))


class PaperWebHandler(BaseHTTPRequestHandler):
    server_version = "FutuPaperAI/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api_get(parsed.path, parse_qs(parsed.query))
            return
        self._serve_static(parsed.path)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        self._serve_static(parsed.path, head_only=True)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        self._handle_api_post(parsed.path)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    @property
    def config(self) -> AppConfig:
        return AppConfig.from_env()

    @property
    def client(self) -> FutuPaperClient:
        return FutuPaperClient(self.config)

    def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        try:
            if path == "/api/status":
                self._send_json(_doctor_payload(self.config))
            elif path == "/api/snapshot":
                codes = self._query_list(query, "codes")
                self._send_json(self.client.snapshot(codes))
            elif path == "/api/account":
                market = self._query_one(query, "market", "US").upper()
                currency = self._query_one(query, "currency", "USD").upper()
                self._send_json(self.client.account(market, currency))
            elif path == "/api/positions":
                market = self._query_one(query, "market", "US").upper()
                self._send_json(self.client.positions(market))
            elif path == "/api/config":
                self._send_json({"ok": True, "config": public_config(self.config)})
            elif path == "/api/watchlist":
                markets = set(self._query_list(query, "markets")) if query.get("markets") else None
                items = load_watchlist(markets=markets)
                self._send_json({"ok": True, "count": len(items), "items": [item.__dict__ for item in items]})
            elif path == "/api/decisions":
                limit = self._query_int(query, "limit", 20)
                entries = _read_decisions(limit)
                self._send_json({"ok": True, "count": len(entries), "entries": entries})
            else:
                self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_api_post(self, path: str) -> None:
        try:
            payload = self._read_json()
            if path == "/api/validate":
                intent = OrderIntent.from_dict(payload)
                decision = self.client.validate(intent)
                self._send_json({"ok": decision.approved, "intent": intent.to_dict(), "violations": decision.violations})
            elif path == "/api/place":
                intent_payload = payload.get("intent", payload)
                execute = bool(payload.get("execute", False))
                intent = OrderIntent.from_dict(intent_payload)
                self._send_json(self.client.place_order(intent, execute=execute))
            elif path == "/api/ai/once":
                execute = bool(payload.get("execute", False))
                notes = payload.get("notes") or []
                if not isinstance(notes, list):
                    raise ValueError("notes must be a list")
                result = AutoTrader(self.config).run_once(execute=execute, notes=[str(note) for note in notes])
                self._send_json(result.__dict__)
            else:
                self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _serve_static(self, path: str, head_only: bool = False) -> None:
        target = "index.html" if path in {"", "/"} else path.lstrip("/")
        file_path = (STATIC_ROOT / target).resolve()
        if STATIC_ROOT.resolve() not in file_path.parents and file_path != STATIC_ROOT.resolve():
            self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        if not file_path.exists() or not file_path.is_file():
            self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
            return

        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _query_one(self, query: dict[str, list[str]], key: str, default: str) -> str:
        values = query.get(key)
        if not values:
            return default
        return values[0]

    def _query_list(self, query: dict[str, list[str]], key: str) -> list[str]:
        raw_values = query.get(key, [])
        codes: list[str] = []
        for raw in raw_values:
            codes.extend(item.strip().upper() for item in raw.split(",") if item.strip())
        if not codes:
            raise ValueError(f"Missing query parameter: {key}")
        return codes

    def _query_int(self, query: dict[str, list[str]], key: str, default: int) -> int:
        values = query.get(key)
        if not values:
            return default
        try:
            return int(values[0])
        except ValueError as exc:
            raise ValueError(f"Invalid integer query parameter: {key}") from exc

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(_jsonable(payload), ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_web_server(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), PaperWebHandler)
    print(f"Futu paper AI web console: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping web console")
    finally:
        server.server_close()
