"""跨境邮政编排平台的 HTTP/JSON 边界，可与基础服务合并挂载。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from digital_trade_foundation.api import route as foundation_route
from digital_trade_foundation.errors import DomainError, ValidationError
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database

from .service import PostalService


def _receipt(result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return (200 if result.get("replayed") else 201), result


def _query(parsed, field: str) -> str:
    value = parse_qs(parsed.query).get(field, [""])[0]
    if not value:
        raise ValidationError(f"{field} 不能为空")
    return value


def route(service: PostalService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到邮政领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    target = parsed.path
    try:
        if method == "POST":
            writes = {
                "/postal/operators": service.register_operator,
                "/postal/nodes": service.register_node,
                "/postal/nodes/status": service.set_node_status,
                "/postal/segments": service.register_segment,
                "/postal/segments/capacity": service.update_segment_capacity,
                "/postal/segments/status": service.set_segment_status,
                "/postal/commitments": service.register_commitment,
                "/postal/rules": service.publish_rule,
                "/postal/parcels": service.register_parcel,
                "/postal/parcels/declarations": service.submit_declaration,
                "/postal/parcels/deliver": service.deliver_parcel,
                "/postal/parcels/reroute": service.reroute_parcel,
                "/postal/containers": service.create_container,
                "/postal/containers/load": service.load_parcel,
                "/postal/containers/unload": service.unload_parcel,
                "/postal/containers/seal": service.seal_container,
                "/postal/containers/arrive": service.arrive_container,
                "/postal/containers/inspect": service.open_inspection,
                "/postal/containers/inspect/close": service.close_inspection,
                "/postal/handovers/confirm": service.confirm_handover,
                "/postal/timeouts/evaluate": service.evaluate_timeouts,
            }
            handler = writes.get(target)
            if handler:
                return _receipt(handler(actor_id=actor_id, **body))
        if method == "GET":
            if target == "/postal/parcels/trace":
                return 200, service.parcel_trace(actor_id=actor_id,
                                                 parcel_id=_query(parsed, "parcel_id"))
            if target == "/postal/parcels/support-view":
                return 200, service.support_view(actor_id=actor_id,
                                                 parcel_id=_query(parsed, "parcel_id"))
            if target == "/postal/containers/lineage":
                return 200, service.container_lineage(actor_id=actor_id,
                                                      container_id=_query(parsed, "container_id"))
            if target == "/postal/nodes/impact":
                return 200, service.node_impact(actor_id=actor_id,
                                                node_id=_query(parsed, "node_id"))
            if target == "/postal/waitlist":
                node_id = parse_qs(parsed.query).get("node_id", [None])[0]
                return 200, service.list_waitlist(actor_id=actor_id, node_id=node_id)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def main() -> int:
    """启动合并基础服务与邮政编排平台的本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动跨境邮政路由与责任编排平台")
    parser.add_argument("--database", default="postal.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    foundation = DomainService(database)
    postal = PostalService(database)

    class CombinedHandler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
                return
            headers = {"X-Actor-Id": self.headers.get("X-Actor-Id", "")}
            if self.path.startswith("/postal"):
                status, payload = route(postal, self.command, self.path, body, headers)
            else:
                status, payload = foundation_route(foundation, self.command, self.path,
                                                   body, headers)
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

    server = ThreadingHTTPServer((args.host, args.port), CombinedHandler)
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
