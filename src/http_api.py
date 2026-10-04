"""HTTP 路由与统一错误输出。

覆盖：再保合约、赔案(补录/撤销/核定)、未决赔款准备金评估批次
(创建/刷新/封账/财务复核转正式)、状态统计与审计时间线。
"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


CONTRACT_RE = re.compile(r"^/api/contracts/(\d+)$")
CLAIM_RE = re.compile(r"^/api/claims/(\d+)$")
BATCH_RE = re.compile(r"^/api/batches/(\d+)$")
AUDIT_RE = re.compile(r"^/api/(contracts|claims|batches)/(\d+)/audit$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "reserve-batch/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 4 * 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if content_type.startswith(
                "application/json") else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        # ---------- GET ----------
        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/health":
                    self._send(200, {"status": "ok", "service": "reserve-batch",
                                     "database": service.repository.health()})
                    return
                if path == "/":
                    self._send(200, (static_dir / "index.html").read_bytes(), "text/html; charset=utf-8")
                    return
                query = parse_qs(parsed.query)

                if path == "/api/contracts":
                    items = service.list_contracts(
                        self._actor(), state=query.get("state", [None])[0],
                        limit=int(query.get("limit", ["200"])[0]))
                    self._send(200, {"items": items})
                    return
                match = CONTRACT_RE.match(path)
                if match:
                    self._send(200, service.get_contract_detail(self._actor(), int(match.group(1))))
                    return

                if path == "/api/batches":
                    items = service.list_batches(self._actor(), int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": items})
                    return
                match = BATCH_RE.match(path)
                if match:
                    self._send(200, service.get_batch_detail(self._actor(), int(match.group(1))))
                    return

                if path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return

                match = AUDIT_RE.match(path)
                if match:
                    entity = {"contracts": "contract", "claims": "claim", "batches": "batch"}[match.group(1)]
                    self._send(200, {"items": service.timeline(self._actor(), entity, int(match.group(2)))})
                    return

                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        # ---------- POST ----------
        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                body = self._body()

                if path == "/api/contracts":
                    self._send(201, service.create_contract(self._actor(), body.get("data", body)))
                    return

                match = re.match(r"^/api/contracts/(\d+)/claims$", path)
                if match:
                    self._send(201, service.register_claim(self._actor(), int(match.group(1)), body.get("data", body)))
                    return

                match = re.match(r"^/api/claims/(\d+)/amend$", path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    claim = service.amend_claim(
                        self._actor(), int(match.group(1)), version,
                        body.get("data", {}), body.get("change_reason", ""))
                    self._send(200, claim)
                    return

                if path == "/api/batches":
                    ibnr = float(body.get("ibnr_factor", 0.1))
                    discount = float(body.get("discount_factor", 1.0))
                    result = service.create_batch(
                        self._actor(), body.get("period", ""),
                        body.get("idempotency_key", ""), ibnr, discount,
                        body.get("contract_ids"))
                    self._send(201, result)
                    return

                match = re.match(r"^/api/batches/(\d+)/refresh$", path)
                if match:
                    self._send(200, service.refresh_batch(self._actor(), int(match.group(1))))
                    return
                match = re.match(r"^/api/batches/(\d+)/close$", path)
                if match:
                    self._send(200, service.close_batch(self._actor(), int(match.group(1))))
                    return
                match = re.match(r"^/api/batches/(\d+)/post$", path)
                if match:
                    result = service.review_and_post(
                        self._actor(), int(match.group(1)),
                        body.get("adjustments", {}), bool(body.get("confirmed", False)))
                    self._send(200, result)
                    return

                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
