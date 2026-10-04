"""邮政路由平台的无第三方依赖 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from digital_trade_foundation.api import route as foundation_route
from digital_trade_foundation.errors import DomainError
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database

from .service import PostalService


def _reply(receipt, response: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    status = 200 if receipt.replayed else 201
    return status, {"request_id": receipt.request_id, "resource_type": receipt.resource_type,
                    "resource_id": receipt.resource_id, "replayed": receipt.replayed,
                    "result": response}


def route(service: PostalService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把邮政路由的 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    segments = [segment for segment in urlparse(path).path.split("/") if segment]
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and segments == ["health"]:
            valid, count = service.foundation.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and len(segments) == 2 and segments[0] == "postal":
            writes = {
                "commitments": service.register_commitment,
                "gateways": service.register_gateway,
                "gateway-status": service.set_gateway_status,
                "legs": service.register_leg,
                "leg-status": service.set_leg_status,
                "containers": service.register_container,
                "parcels": service.register_parcel,
                "declarations": service.add_declaration_version,
                "rules": service.publish_rule,
                "plans": service.generate_plan,
                "consolidations": service.consolidate,
                "seals": service.seal_container,
                "dispatches": service.dispatch_container,
                "receipts": service.receive_container,
                "deconsolidations": service.deconsolidate,
                "inspections": service.start_inspection,
                "releases": service.release_parcel,
                "deliveries": service.deliver_parcel,
                "confirmations": service.confirm,
                "timeout-scans": service.scan_timeouts,
            }
            handler = writes.get(segments[1])
            if handler is None:
                return 404, {"error": "route_not_found", "message": "接口不存在"}
            receipt, response = handler(actor_id=actor_id, **body)
            return _reply(receipt, response)
        if method == "GET" and len(segments) == 4 and segments[0] == "postal" \
                and segments[1] == "parcels":
            if segments[3] == "trace":
                return 200, service.parcel_trace(actor_id=actor_id, parcel_id=segments[2])
            if segments[3] == "support-view":
                return 200, service.support_view(actor_id=actor_id, parcel_id=segments[2])
        if method == "GET" and len(segments) == 4 and segments[0] == "postal" \
                and segments[1] == "gateways" and segments[3] == "impact":
            return 200, service.gateway_impact(actor_id=actor_id, gateway_id=segments[2])
        if method == "GET" and len(segments) == 3 and segments[0] == "postal" \
                and segments[1] == "containers":
            return 200, service.container_view(actor_id=actor_id, container_id=segments[2])
        if method == "GET" and segments == ["postal", "legs"]:
            return 200, service.leg_capacity_view(actor_id=actor_id)
        status, payload = foundation_route(service.foundation, method, path, body, headers)
        if payload.get("error") != "route_not_found":
            return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为邮政路由调用。"""

    service: PostalService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动邮政路由平台 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动跨境邮政路由与责任编排平台")
    parser.add_argument("--database", default="postal.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = Database(args.database)
    foundation = DomainService(database)
    Handler.service = PostalService(database, foundation)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
