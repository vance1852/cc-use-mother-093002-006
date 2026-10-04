"""跨境邮政路由与责任编排的领域服务。

在基础服务（组织、操作者、幂等回执、哈希串联审计）之上实现：
- 包裹事实、申报及证明版本、辖区规则、服务承诺、口岸、运输区段、容器容量与交接资格的登记；
- 带理由的中转方案、稳定优先级的候补路线、原子容量预留，重放事件不会多占资源；
- 合包、拆包、封袋、转运、查验、改道、签收的全生命周期事件与包裹-容器双向谱系；
- 规则或证明变化只重估尚未完成且确实命中的对象，完成的交接与时间记录不可重写；
- 节点、承运方、合规各自确认交接，客户支持只能查询可披露的状态与原因；
- 从包裹回看全部规则与责任人，或从口岸关闭正向推导受影响承诺与改道结果；
- 全部状态持久化在 SQLite，进程重启后在途容器、候补路线与超时责任保持一致。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from typing import Any, Callable

from digital_trade_foundation.audit import append_event, canonical_json, digest
from digital_trade_foundation.clock import Clock, SystemClock
from digital_trade_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from digital_trade_foundation.models import WriteReceipt
from digital_trade_foundation.storage import Database

from .models import (
    NODE_STATUSES,
    PUBLIC_STATUS_TEXT,
    ROLES,
    RULE_SCOPES,
    RULE_TYPES,
    SEGMENT_STATES,
    TERMINAL_PARCEL_STATES,
)
from .planning import ParcelFacts, RuleInfo, SegmentInfo, assess_path, evaluate_rule, plan_routes
from .schema import POSTAL_SCHEMA

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")


class PostalService:
    """协调邮政路由、权限、幂等、事务与审计规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.database.connection.executescript(POSTAL_SCHEMA)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _operator(self, connection, operator_id: str):
        row = connection.execute(
            "SELECT * FROM postal_operators WHERE operator_id=?", (operator_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, operator, *roles: str) -> None:
        if operator["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _node(self, connection, node_id: str):
        row = connection.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        if row is None:
            raise NotFoundError("口岸节点不存在")
        return row

    def _segment(self, connection, segment_id: str):
        row = connection.execute("SELECT * FROM segments WHERE segment_id=?", (segment_id,)).fetchone()
        if row is None:
            raise NotFoundError("运输区段不存在")
        return row

    def _parcel(self, connection, parcel_id: str):
        row = connection.execute("SELECT * FROM parcels WHERE parcel_id=?", (parcel_id,)).fetchone()
        if row is None:
            raise NotFoundError("包裹不存在")
        return row

    def _container(self, connection, container_id: str):
        row = connection.execute(
            "SELECT * FROM containers WHERE container_id=?", (container_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("容器不存在")
        return row

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> tuple[WriteReceipt, dict[str, Any]]:
        """与基础服务共用 request_receipts 表；命中回执时直接返回，不重复执行副作用。"""

        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True), \
                json.loads(row["response_json"])
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False), response

    @staticmethod
    def _result(receipt: WriteReceipt, response: dict[str, Any]) -> dict[str, Any]:
        return {**response, "request_id": receipt.request_id, "replayed": receipt.replayed}

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _parcel_event(self, connection, parcel_id: str, action: str,
                      detail: dict[str, Any], actor_id: str, now: str) -> None:
        connection.execute(
            "INSERT INTO parcel_events(parcel_id,action,detail_json,actor_id,occurred_at) "
            "VALUES(?,?,?,?,?)",
            (parcel_id, action, canonical_json(detail), actor_id, now),
        )

    def _container_event(self, connection, container_id: str, action: str,
                         detail: dict[str, Any], actor_id: str, now: str) -> None:
        connection.execute(
            "INSERT INTO container_events(container_id,action,detail_json,actor_id,occurred_at) "
            "VALUES(?,?,?,?,?)",
            (container_id, action, canonical_json(detail), actor_id, now),
        )

    # ------------------------------------------------------------------
    # 登记：操作者、口岸、区段、承诺、规则
    # ------------------------------------------------------------------

    def register_operator(self, *, request_id: str, actor_id: str, operator_id: str,
                          display_name: str, role: str,
                          node_id: str | None = None, carrier_id: str | None = None) -> dict[str, Any]:
        """登记邮政平台操作者；只有基础服务的 admin 可以登记。"""

        payload = {"actor_id": actor_id, "operator_id": operator_id, "display_name": display_name,
                   "role": role, "node_id": node_id, "carrier_id": carrier_id}
        with self.database.transaction(immediate=True) as connection:
            admin = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
            if admin is None or not admin["active"] or admin["role"] != "admin":
                raise PermissionDenied("只有基础服务管理员可以登记邮政操作者")
            operator_id = self._identifier(operator_id, "operator_id")
            display_name = self._text(display_name, "display_name")
            if role not in ROLES:
                raise ValidationError("role 不在允许范围内")
            if role == "node" and not node_id:
                raise ValidationError("节点操作员必须绑定 node_id")
            if role == "carrier" and not carrier_id:
                raise ValidationError("承运方操作员必须绑定 carrier_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                if role == "node":
                    self._node(connection, node_id)
                try:
                    connection.execute(
                        "INSERT INTO postal_operators(operator_id,display_name,role,node_id,carrier_id,"
                        "active,created_by,created_at) VALUES(?,?,?,?,?,1,?,?)",
                        (operator_id, display_name, role, node_id, carrier_id, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("操作者编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="postal.operator.registered",
                            resource_type="postal_operator", resource_id=operator_id,
                            detail={"role": role, "node_id": node_id, "carrier_id": carrier_id})
                return "postal_operator", operator_id, {"operator_id": operator_id, "role": role}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.register_operator",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def register_node(self, *, request_id: str, actor_id: str, node_id: str,
                      name: str, jurisdiction: str, kind: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "node_id": node_id, "name": name,
                   "jurisdiction": jurisdiction, "kind": kind}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "admin")
            node_id = self._identifier(node_id, "node_id")
            name = self._text(name, "name")
            jurisdiction = self._text(jurisdiction, "jurisdiction", 80)
            kind = self._text(kind, "kind", 40)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO nodes(node_id,name,jurisdiction,kind,status,created_at) "
                        "VALUES(?,?,?,?,'open',?)",
                        (node_id, name, jurisdiction, kind, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("口岸节点编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="postal.node.registered",
                            resource_type="node", resource_id=node_id,
                            detail={"name": name, "jurisdiction": jurisdiction, "kind": kind})
                return "node", node_id, {"node_id": node_id, "status": "open"}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.register_node",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def register_segment(self, *, request_id: str, actor_id: str, segment_id: str,
                         from_node: str, to_node: str, carrier_id: str, mode: str,
                         capacity: int, transit_hours: int, priority: int = 100) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "segment_id": segment_id, "from_node": from_node,
                   "to_node": to_node, "carrier_id": carrier_id, "mode": mode,
                   "capacity": capacity, "transit_hours": transit_hours, "priority": priority}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "admin")
            segment_id = self._identifier(segment_id, "segment_id")
            carrier_id = self._identifier(carrier_id, "carrier_id")
            mode = self._text(mode, "mode", 40)
            if from_node == to_node:
                raise ValidationError("区段起点与终点不能相同")
            if int(capacity) < 1 or int(transit_hours) < 1 or int(priority) < 0:
                raise ValidationError("capacity/transit_hours 必须为正，priority 不能为负")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._node(connection, from_node)
                self._node(connection, to_node)
                try:
                    connection.execute(
                        "INSERT INTO segments(segment_id,from_node,to_node,carrier_id,mode,capacity,"
                        "transit_hours,priority,status,created_at) VALUES(?,?,?,?,?,?,?,?,'active',?)",
                        (segment_id, from_node, to_node, carrier_id, mode, int(capacity),
                         int(transit_hours), int(priority), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("运输区段编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="postal.segment.registered",
                            resource_type="segment", resource_id=segment_id,
                            detail={"from_node": from_node, "to_node": to_node,
                                    "carrier_id": carrier_id, "capacity": int(capacity)})
                return "segment", segment_id, {"segment_id": segment_id, "status": "active"}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.register_segment",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def update_segment_capacity(self, *, request_id: str, actor_id: str,
                                segment_id: str, capacity: int) -> dict[str, Any]:
        """调整区段容量（如航班缩减）；已持有的预留继续有效，只限制新的预留。"""

        payload = {"actor_id": actor_id, "segment_id": segment_id, "capacity": capacity}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "admin")
            segment_id = self._identifier(segment_id, "segment_id")
            if int(capacity) < 0:
                raise ValidationError("capacity 不能为负")

            def create() -> tuple[str, str, dict[str, Any]]:
                segment = self._segment(connection, segment_id)
                held = self._held_units(connection, segment_id)
                connection.execute("UPDATE segments SET capacity=? WHERE segment_id=?",
                                   (int(capacity), segment_id))
                self._audit(connection, actor_id=actor_id, action="postal.segment.capacity_updated",
                            resource_type="segment", resource_id=segment_id,
                            detail={"old_capacity": segment["capacity"], "capacity": int(capacity),
                                    "held": held})
                return "segment", segment_id, {
                    "segment_id": segment_id, "capacity": int(capacity),
                    "held": held, "oversubscribed": held > int(capacity),
                }

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.update_segment_capacity",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def set_segment_status(self, *, request_id: str, actor_id: str,
                           segment_id: str, status: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "segment_id": segment_id, "status": status}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "admin")
            segment_id = self._identifier(segment_id, "segment_id")
            if status not in SEGMENT_STATES:
                raise ValidationError("status 不在允许范围内")

            def create() -> tuple[str, str, dict[str, Any]]:
                segment = self._segment(connection, segment_id)
                now = self._now()
                connection.execute("UPDATE segments SET status=? WHERE segment_id=?",
                                   (status, segment_id))
                affected: list[dict[str, Any]] = []
                promoted: list[str] = []
                if status == "suspended":
                    affected = self._disrupt_segment(connection, segment, actor_id, now)
                elif status == "active":
                    promoted = self._process_waitlist(connection, None, actor_id)
                self._audit(connection, actor_id=actor_id, action="postal.segment.status_changed",
                            resource_type="segment", resource_id=segment_id,
                            detail={"status": status, "affected": len(affected),
                                    "promoted": promoted})
                return "segment", segment_id, {"segment_id": segment_id, "status": status,
                                               "affected": affected, "promoted": promoted}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.set_segment_status",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def register_commitment(self, *, request_id: str, actor_id: str, commitment_id: str,
                            product: str, max_transit_hours: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "product": product,
                   "max_transit_hours": max_transit_hours}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "admin")
            commitment_id = self._identifier(commitment_id, "commitment_id")
            product = self._text(product, "product")
            if int(max_transit_hours) < 1:
                raise ValidationError("max_transit_hours 必须为正")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO commitments(commitment_id,product,max_transit_hours,created_at) "
                        "VALUES(?,?,?,?)",
                        (commitment_id, product, int(max_transit_hours), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("服务承诺编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="postal.commitment.registered",
                            resource_type="commitment", resource_id=commitment_id,
                            detail={"product": product, "max_transit_hours": int(max_transit_hours)})
                return "commitment", commitment_id, {"commitment_id": commitment_id}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.register_commitment",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def publish_rule(self, *, request_id: str, actor_id: str, rule_id: str, jurisdiction: str,
                     scope: str, rule_type: str, expression: dict[str, Any]) -> dict[str, Any]:
        """发布辖区规则新版本；只重估尚未完成且确实命中规则的包裹。"""

        payload = {"actor_id": actor_id, "rule_id": rule_id, "jurisdiction": jurisdiction,
                   "scope": scope, "rule_type": rule_type, "expression": expression}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "admin", "compliance")
            rule_id = self._identifier(rule_id, "rule_id")
            jurisdiction = self._text(jurisdiction, "jurisdiction", 80)
            if scope not in RULE_SCOPES:
                raise ValidationError("scope 不在允许范围内")
            if rule_type not in RULE_TYPES:
                raise ValidationError("rule_type 不在允许范围内")
            self._validate_expression(rule_type, expression)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                row = connection.execute(
                    "SELECT MAX(version) AS version FROM rules WHERE rule_id=?", (rule_id,)
                ).fetchone()
                version = (row["version"] or 0) + 1
                connection.execute("UPDATE rules SET status='retired' WHERE rule_id=?", (rule_id,))
                connection.execute(
                    "INSERT INTO rules(rule_id,version,jurisdiction,scope,rule_type,expression_json,"
                    "status,created_by,created_at) VALUES(?,?,?,?,?,?, 'active',?,?)",
                    (rule_id, version, jurisdiction, scope, rule_type,
                     canonical_json(expression), actor_id, now),
                )
                rule = connection.execute(
                    "SELECT * FROM rules WHERE rule_id=? AND version=?", (rule_id, version)
                ).fetchone()
                affected = self._reevaluate_rule(connection, rule, actor_id, now)
                self._audit(connection, actor_id=actor_id, action="postal.rule.published",
                            resource_type="rule", resource_id=f"{rule_id}@{version}",
                            detail={"jurisdiction": jurisdiction, "scope": scope,
                                    "rule_type": rule_type, "affected": affected})
                return "rule", f"{rule_id}@{version}", {
                    "rule_id": rule_id, "version": version, "affected": affected,
                }

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.publish_rule",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def _validate_expression(self, rule_type: str, expression: dict[str, Any]) -> None:
        if not isinstance(expression, dict):
            raise ValidationError("expression 必须是对象")
        if rule_type == "prohibited_category":
            categories = expression.get("categories")
            if not isinstance(categories, list) or not categories \
                    or not all(isinstance(item, str) and item for item in categories):
                raise ValidationError("prohibited_category 需要非空 categories 列表")
        elif rule_type == "value_cap":
            max_value = expression.get("max_value")
            if not isinstance(max_value, (int, float)) or max_value < 0:
                raise ValidationError("value_cap 需要非负的 max_value")
            if not expression.get("currency"):
                raise ValidationError("value_cap 需要 currency")
        elif rule_type == "requires_proof":
            if not expression.get("proof_kind"):
                raise ValidationError("requires_proof 需要 proof_kind")
            categories = expression.get("categories")
            if categories is not None and not isinstance(categories, list):
                raise ValidationError("categories 必须是列表")

    # ------------------------------------------------------------------
    # 包裹登记与申报版本
    # ------------------------------------------------------------------

    def register_parcel(self, *, request_id: str, actor_id: str, parcel_id: str,
                        origin_node: str, destination_node: str, category: str,
                        declared_value: float, currency: str, weight_grams: int,
                        commitment_id: str, proofs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        proofs = proofs or []
        payload = {"actor_id": actor_id, "parcel_id": parcel_id, "origin_node": origin_node,
                   "destination_node": destination_node, "category": category,
                   "declared_value": declared_value, "currency": currency,
                   "weight_grams": weight_grams, "commitment_id": commitment_id, "proofs": proofs}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "node")
            parcel_id = self._identifier(parcel_id, "parcel_id")
            category = self._text(category, "category", 80)
            currency = self._text(currency, "currency", 8)
            if float(declared_value) < 0 or int(weight_grams) < 1:
                raise ValidationError("declared_value 不能为负，weight_grams 必须为正")
            self._validate_proofs(proofs)
            if origin_node == destination_node:
                raise ValidationError("起点与目的节点不能相同")
            if operator["node_id"] != origin_node:
                raise PermissionDenied("只能在所属节点收寄包裹")

            def create() -> tuple[str, str, dict[str, Any]]:
                origin = self._node(connection, origin_node)
                destination = self._node(connection, destination_node)
                commitment = connection.execute(
                    "SELECT * FROM commitments WHERE commitment_id=?", (commitment_id,)
                ).fetchone()
                if commitment is None:
                    raise NotFoundError("服务承诺不存在")
                now = self._now()
                deadline = (self.clock.now()
                            + timedelta(hours=int(commitment["max_transit_hours"]))
                            ).isoformat().replace("+00:00", "Z")
                try:
                    connection.execute(
                        "INSERT INTO parcels(parcel_id,origin_node,destination_node,origin_jurisdiction,"
                        "destination_jurisdiction,category,declared_value,currency,weight_grams,"
                        "commitment_id,state,current_node,current_container,declaration_version,"
                        "accepted_at,deadline_at,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?, 'accepted',?,NULL,1,?,?,?)",
                        (parcel_id, origin_node, destination_node, origin["jurisdiction"],
                         destination["jurisdiction"], category, float(declared_value), currency,
                         int(weight_grams), commitment_id, origin_node, now, deadline, now),
                    )
                except Exception as exc:
                    raise ConflictError("包裹编号已经存在") from exc
                connection.execute(
                    "INSERT INTO declarations(parcel_id,version,declared_value,currency,category,"
                    "proofs_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (parcel_id, 1, float(declared_value), currency, category,
                     canonical_json(proofs), actor_id, now),
                )
                self._parcel_event(connection, parcel_id, "accepted",
                                   {"origin_node": origin_node, "destination_node": destination_node,
                                    "commitment_id": commitment_id, "deadline_at": deadline},
                                   actor_id, now)
                parcel = self._parcel(connection, parcel_id)
                outcome = self._replan(connection, parcel, "初始规划", actor_id,
                                       waitlist_on_failure=True)
                self._audit(connection, actor_id=actor_id, action="postal.parcel.registered",
                            resource_type="parcel", resource_id=parcel_id,
                            detail={"origin_node": origin_node, "destination_node": destination_node,
                                    "deadline_at": deadline, "plan": outcome})
                return "parcel", parcel_id, {"parcel_id": parcel_id, "deadline_at": deadline,
                                             "plan": outcome}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.register_parcel",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def _validate_proofs(self, proofs: list[dict[str, Any]]) -> None:
        if not isinstance(proofs, list):
            raise ValidationError("proofs 必须是列表")
        for proof in proofs:
            if not isinstance(proof, dict) or not proof.get("kind"):
                raise ValidationError("每份证明必须包含 kind")

    def submit_declaration(self, *, request_id: str, actor_id: str, parcel_id: str,
                           declared_value: float, currency: str, category: str,
                           proofs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """提交新的申报/证明版本，只重估该包裹本身。"""

        proofs = proofs or []
        payload = {"actor_id": actor_id, "parcel_id": parcel_id, "declared_value": declared_value,
                   "currency": currency, "category": category, "proofs": proofs}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            parcel_id = self._identifier(parcel_id, "parcel_id")
            category = self._text(category, "category", 80)
            currency = self._text(currency, "currency", 8)
            if float(declared_value) < 0:
                raise ValidationError("declared_value 不能为负")
            self._validate_proofs(proofs)

            def create() -> tuple[str, str, dict[str, Any]]:
                parcel = self._parcel(connection, parcel_id)
                if parcel["state"] in TERMINAL_PARCEL_STATES:
                    raise ConflictError("包裹已完成，申报与证明不可再变更")
                if not (operator["role"] in ("admin", "compliance")
                        or (operator["role"] == "node"
                            and operator["node_id"] == parcel["current_node"])):
                    raise PermissionDenied("只有当前节点、合规或管理员可以变更申报")
                now = self._now()
                version = parcel["declaration_version"] + 1
                connection.execute(
                    "INSERT INTO declarations(parcel_id,version,declared_value,currency,category,"
                    "proofs_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (parcel_id, version, float(declared_value), currency, category,
                     canonical_json(proofs), actor_id, now),
                )
                connection.execute(
                    "UPDATE parcels SET declaration_version=?,declared_value=?,currency=?,category=? "
                    "WHERE parcel_id=?",
                    (version, float(declared_value), currency, category, parcel_id),
                )
                self._parcel_event(connection, parcel_id, "declaration_updated",
                                   {"version": version}, actor_id, now)
                updated = self._parcel(connection, parcel_id)
                self._reevaluate_parcel(connection, updated, actor_id, now)
                open_rule_holds = self._open_hold_count(connection, parcel_id, rule_only=True)
                if open_rule_holds == 0 and updated["current_container"] is None \
                        and updated["state"] not in TERMINAL_PARCEL_STATES:
                    try:
                        self._replan(connection, updated, "申报版本更新", actor_id,
                                     waitlist_on_failure=True)
                    except ValidationError:
                        pass
                open_holds = self._open_hold_count(connection, parcel_id)
                self._audit(connection, actor_id=actor_id, action="postal.declaration.submitted",
                            resource_type="parcel", resource_id=parcel_id,
                            detail={"version": version, "open_holds": open_holds})
                return "parcel", parcel_id, {"parcel_id": parcel_id,
                                             "declaration_version": version,
                                             "open_holds": open_holds}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.submit_declaration",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    # ------------------------------------------------------------------
    # 容器生命周期：建袋、合包、拆包、封袋、转运、查验
    # ------------------------------------------------------------------

    def create_container(self, *, request_id: str, actor_id: str, container_id: str,
                         segment_id: str, capacity: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "container_id": container_id,
                   "segment_id": segment_id, "capacity": capacity}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "node")
            container_id = self._identifier(container_id, "container_id")
            segment_id = self._identifier(segment_id, "segment_id")
            if int(capacity) < 1:
                raise ValidationError("capacity 必须为正")

            def create() -> tuple[str, str, dict[str, Any]]:
                segment = self._segment(connection, segment_id)
                if segment["from_node"] != operator["node_id"]:
                    raise PermissionDenied("只能在区段起点节点建袋")
                if segment["status"] != "active":
                    raise ConflictError("区段已暂停，不能建袋")
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO containers(container_id,node_id,segment_id,destination_node,"
                        "capacity,loaded_count,state,created_at) VALUES(?,?,?,?,?,0,'open',?)",
                        (container_id, operator["node_id"], segment_id, segment["to_node"],
                         int(capacity), now),
                    )
                except Exception as exc:
                    raise ConflictError("容器编号已经存在") from exc
                self._container_event(connection, container_id, "created",
                                      {"segment_id": segment_id, "capacity": int(capacity)},
                                      actor_id, now)
                self._audit(connection, actor_id=actor_id, action="postal.container.created",
                            resource_type="container", resource_id=container_id,
                            detail={"segment_id": segment_id, "capacity": int(capacity)})
                return "container", container_id, {"container_id": container_id, "state": "open"}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.create_container",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def load_parcel(self, *, request_id: str, actor_id: str,
                    container_id: str, parcel_id: str) -> dict[str, Any]:
        """合包：包裹只能装入与其方案下一程一致的容器，容量原子扣减。"""

        payload = {"actor_id": actor_id, "container_id": container_id, "parcel_id": parcel_id}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "node")
            container_id = self._identifier(container_id, "container_id")
            parcel_id = self._identifier(parcel_id, "parcel_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                container = self._container(connection, container_id)
                parcel = self._parcel(connection, parcel_id)
                if operator["node_id"] != container["node_id"]:
                    raise PermissionDenied("只能在容器所在节点合包")
                if container["state"] != "open":
                    raise ConflictError("容器不在打开状态，不能合包")
                if parcel["current_container"]:
                    raise ConflictError("包裹已在其他容器内")
                if parcel["state"] not in ("planned", "arrived"):
                    raise ConflictError("包裹当前状态不能合包")
                if parcel["current_node"] != container["node_id"]:
                    raise ValidationError("包裹不在容器所在节点")
                if self._open_hold_count(connection, parcel_id):
                    raise ConflictError("包裹存在未解除的扣留，不能合包")
                if self._waiting_entry(connection, parcel_id):
                    raise ConflictError("包裹在候补中，不能合包")
                leg = self._next_leg(connection, parcel_id)
                if leg is None:
                    raise ValidationError("包裹没有待执行的路线方案")
                if leg["segment_id"] != container["segment_id"]:
                    raise ValidationError("包裹下一程区段与容器不一致，不能任意合袋")
                now = self._now()
                cursor = connection.execute(
                    "UPDATE containers SET loaded_count=loaded_count+1 "
                    "WHERE container_id=? AND state='open' AND loaded_count<capacity",
                    (container_id,),
                )
                if cursor.rowcount == 0:
                    raise ConflictError("容器容量不足，合包失败")
                connection.execute(
                    "INSERT INTO container_membership(parcel_id,container_id,action,node_id,reason,"
                    "actor_id,occurred_at) VALUES(?,?,'loaded',?,?,?,?)",
                    (parcel_id, container_id, container["node_id"], "合包", actor_id, now),
                )
                connection.execute(
                    "UPDATE parcels SET state='loaded',current_container=? WHERE parcel_id=?",
                    (container_id, parcel_id),
                )
                self._parcel_event(connection, parcel_id, "loaded",
                                   {"container_id": container_id,
                                    "segment_id": container["segment_id"]}, actor_id, now)
                self._container_event(connection, container_id, "parcel_loaded",
                                      {"parcel_id": parcel_id}, actor_id, now)
                self._audit(connection, actor_id=actor_id, action="postal.container.loaded",
                            resource_type="container", resource_id=container_id,
                            detail={"parcel_id": parcel_id})
                loaded = container["loaded_count"] + 1
                return "container", container_id, {"container_id": container_id,
                                                   "parcel_id": parcel_id,
                                                   "loaded_count": loaded}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.load_parcel",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def unload_parcel(self, *, request_id: str, actor_id: str, container_id: str,
                      parcel_id: str, reason: str = "拆包") -> dict[str, Any]:
        """拆包：从容器中取出包裹；从已封袋容器拆包会解封并释放区段预留。"""

        payload = {"actor_id": actor_id, "container_id": container_id,
                   "parcel_id": parcel_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "node")
            container_id = self._identifier(container_id, "container_id")
            parcel_id = self._identifier(parcel_id, "parcel_id")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                container = self._container(connection, container_id)
                parcel = self._parcel(connection, parcel_id)
                if operator["node_id"] != container["node_id"]:
                    raise PermissionDenied("只能在容器所在节点拆包")
                if container["state"] not in ("open", "sealed", "arrived"):
                    raise ConflictError("当前状态不允许拆包")
                if parcel["current_container"] != container_id:
                    raise ValidationError("包裹不在该容器内")
                now = self._now()
                if container["state"] == "sealed":
                    connection.execute(
                        "UPDATE containers SET state='open' WHERE container_id=?", (container_id,))
                    connection.execute(
                        "UPDATE reservations SET state='released',released_at=? "
                        "WHERE container_id=? AND state='held'", (now, container_id))
                    connection.execute(
                        "UPDATE handovers SET state='cancelled' "
                        "WHERE container_id=? AND kind='dispatch' AND state='pending'",
                        (container_id,))
                    self._container_event(connection, container_id, "unsealed",
                                          {"reason": reason}, actor_id, now)
                connection.execute(
                    "INSERT INTO container_membership(parcel_id,container_id,action,node_id,reason,"
                    "actor_id,occurred_at) VALUES(?,?,'unloaded',?,?,?,?)",
                    (parcel_id, container_id, container["node_id"], reason, actor_id, now),
                )
                connection.execute(
                    "UPDATE containers SET loaded_count=loaded_count-1 "
                    "WHERE container_id=? AND loaded_count>0", (container_id,))
                has_plan = connection.execute(
                    "SELECT 1 FROM route_plans WHERE parcel_id=? AND state='active'",
                    (parcel_id,),
                ).fetchone()
                connection.execute(
                    "UPDATE parcels SET state=?,current_container=NULL,current_node=? "
                    "WHERE parcel_id=?",
                    ("planned" if has_plan else "arrived", container["node_id"], parcel_id),
                )
                self._parcel_event(connection, parcel_id, "unloaded",
                                   {"container_id": container_id, "reason": reason}, actor_id, now)
                refreshed = self._container(connection, container_id)
                if refreshed["state"] == "arrived" and refreshed["loaded_count"] == 0:
                    connection.execute(
                        "UPDATE containers SET state='opened' WHERE container_id=?", (container_id,))
                    self._container_event(connection, container_id, "opened", {}, actor_id, now)
                    refreshed = self._container(connection, container_id)
                updated = self._parcel(connection, parcel_id)
                if updated["state"] not in TERMINAL_PARCEL_STATES \
                        and updated["current_node"] != updated["destination_node"] \
                        and self._open_hold_count(connection, parcel_id) == 0 \
                        and not self._route_usable(connection, parcel_id):
                    self._replan(connection, updated, "到达后原路线不可用", actor_id,
                                 waitlist_on_failure=True)
                self._audit(connection, actor_id=actor_id, action="postal.container.unloaded",
                            resource_type="container", resource_id=container_id,
                            detail={"parcel_id": parcel_id, "reason": reason})
                return "parcel", parcel_id, {"parcel_id": parcel_id,
                                             "container_id": container_id,
                                             "container_state": refreshed["state"]}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.unload_parcel",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def seal_container(self, *, request_id: str, actor_id: str,
                       container_id: str) -> dict[str, Any]:
        """封袋：原子占用区段容量并生成待三方确认的发运交接。"""

        payload = {"actor_id": actor_id, "container_id": container_id}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "node")
            container_id = self._identifier(container_id, "container_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                container = self._container(connection, container_id)
                if operator["node_id"] != container["node_id"]:
                    raise PermissionDenied("只能在容器所在节点封袋")
                if container["state"] != "open":
                    raise ConflictError("只有打开的容器可以封袋")
                if container["loaded_count"] < 1:
                    raise ValidationError("空袋不能封发")
                node = self._node(connection, container["node_id"])
                if node["status"] == "closed":
                    raise ConflictError("口岸已关闭，无法封袋发运")
                segment = self._segment(connection, container["segment_id"])
                if segment["status"] != "active":
                    raise ConflictError("区段已暂停，无法封袋发运")
                held = self._held_units(connection, segment["segment_id"])
                if held + 1 > segment["capacity"]:
                    raise ConflictError("区段容量不足，无法预留")
                existing = connection.execute(
                    "SELECT 1 FROM reservations WHERE container_id=? AND state='held'",
                    (container_id,),
                ).fetchone()
                if existing:
                    raise ConflictError("容器已持有区段预留")
                now = self._now()
                reservation_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO reservations(reservation_id,segment_id,container_id,units,state,"
                    "created_at) VALUES(?,?,?,1,'held',?)",
                    (reservation_id, segment["segment_id"], container_id, now),
                )
                connection.execute("UPDATE containers SET state='sealed' WHERE container_id=?",
                                   (container_id,))
                handover_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO handovers(handover_id,kind,container_id,segment_id,node_id,"
                    "carrier_id,state,created_at) VALUES(?,?,?,?,?,?, 'pending',?)",
                    (handover_id, "dispatch", container_id, segment["segment_id"],
                     segment["from_node"], segment["carrier_id"], now),
                )
                self._container_event(connection, container_id, "sealed",
                                      {"handover_id": handover_id,
                                       "reservation_id": reservation_id}, actor_id, now)
                self._audit(connection, actor_id=actor_id, action="postal.container.sealed",
                            resource_type="container", resource_id=container_id,
                            detail={"segment_id": segment["segment_id"],
                                    "reservation_id": reservation_id})
                return "container", container_id, {"container_id": container_id,
                                                   "handover_id": handover_id,
                                                   "reservation_id": reservation_id}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.seal_container",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def confirm_handover(self, *, request_id: str, actor_id: str,
                         handover_id: str) -> dict[str, Any]:
        """节点、承运方、合规各自确认交接；完成后不可改写。"""

        payload = {"actor_id": actor_id, "handover_id": handover_id}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            handover_id = self._identifier(handover_id, "handover_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM handovers WHERE handover_id=?", (handover_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("交接记录不存在")
                if row["state"] == "completed":
                    raise ConflictError("交接已完成，确认不可改写")
                if row["state"] == "cancelled":
                    raise ConflictError("交接已取消")
                party = None
                if operator["role"] == "node" and operator["node_id"] == row["node_id"]:
                    party = "node"
                elif operator["role"] == "carrier" and operator["carrier_id"] == row["carrier_id"]:
                    party = "carrier"
                elif operator["role"] == "compliance":
                    party = "compliance"
                if party is None:
                    raise PermissionDenied("当前操作者不是该交接的确认方")
                now = self._now()
                connection.execute(
                    f"UPDATE handovers SET {party}_confirmed=1 WHERE handover_id=?",
                    (handover_id,),
                )
                updated = connection.execute(
                    "SELECT * FROM handovers WHERE handover_id=?", (handover_id,)
                ).fetchone()
                required = ("node", "carrier", "compliance") if updated["kind"] == "dispatch" \
                    else ("node", "carrier")
                completed = all(updated[f"{name}_confirmed"] for name in required)
                if completed:
                    if updated["kind"] == "dispatch":
                        node = self._node(connection, updated["node_id"])
                        if node["status"] == "closed":
                            raise ConflictError("口岸已关闭，无法完成发运交接")
                        self._complete_dispatch(connection, updated, actor_id, now)
                    else:
                        self._complete_arrival(connection, updated, actor_id, now)
                    connection.execute(
                        "UPDATE handovers SET state='completed',completed_at=? WHERE handover_id=?",
                        (now, handover_id),
                    )
                self._audit(connection, actor_id=actor_id, action="postal.handover.confirmed",
                            resource_type="handover", resource_id=handover_id,
                            detail={"party": party, "completed": completed})
                return "handover", handover_id, {
                    "handover_id": handover_id,
                    "state": "completed" if completed else "pending",
                    "completed": completed,
                }

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.confirm_handover",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def _complete_dispatch(self, connection, handover, actor_id: str, now: str) -> None:
        container = self._container(connection, handover["container_id"])
        connection.execute(
            "UPDATE containers SET state='in_transit' WHERE container_id=? AND state='sealed'",
            (container["container_id"],),
        )
        self._container_event(connection, container["container_id"], "departed",
                              {"handover_id": handover["handover_id"],
                               "segment_id": container["segment_id"]}, actor_id, now)
        members = connection.execute(
            "SELECT * FROM parcels WHERE current_container=?", (container["container_id"],)
        ).fetchall()
        for member in members:
            connection.execute("UPDATE parcels SET state='in_transit' WHERE parcel_id=?",
                               (member["parcel_id"],))
            plan = connection.execute(
                "SELECT * FROM route_plans WHERE parcel_id=? AND state='active'",
                (member["parcel_id"],),
            ).fetchone()
            if plan:
                leg = connection.execute(
                    "SELECT * FROM route_plan_legs WHERE plan_id=? AND state='pending' "
                    "ORDER BY leg_index LIMIT 1", (plan["plan_id"],),
                ).fetchone()
                if leg and leg["segment_id"] == container["segment_id"]:
                    connection.execute(
                        "UPDATE route_plan_legs SET state='in_use' "
                        "WHERE plan_id=? AND leg_index=?",
                        (plan["plan_id"], leg["leg_index"]),
                    )
            self._parcel_event(connection, member["parcel_id"], "departed",
                               {"container_id": container["container_id"],
                                "segment_id": container["segment_id"]}, actor_id, now)

    def arrive_container(self, *, request_id: str, actor_id: str,
                         container_id: str) -> dict[str, Any]:
        """承运方申报到港，生成待节点确认的到达交接。"""

        payload = {"actor_id": actor_id, "container_id": container_id}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "carrier")
            container_id = self._identifier(container_id, "container_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                container = self._container(connection, container_id)
                segment = self._segment(connection, container["segment_id"])
                if operator["carrier_id"] != segment["carrier_id"]:
                    raise PermissionDenied("只有承运该区段的承运方可以申报到港")
                if container["state"] != "in_transit":
                    raise ConflictError("容器不在运输途中")
                existing = connection.execute(
                    "SELECT * FROM handovers WHERE container_id=? AND kind='arrival' "
                    "AND state='pending'", (container_id,),
                ).fetchone()
                if existing:
                    return "handover", existing["handover_id"], {
                        "handover_id": existing["handover_id"],
                        "container_id": container_id, "existing": True,
                    }
                now = self._now()
                handover_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO handovers(handover_id,kind,container_id,segment_id,node_id,"
                    "carrier_id,carrier_confirmed,state,created_at) "
                    "VALUES(?,?,?,?,?,?,1,'pending',?)",
                    (handover_id, "arrival", container_id, segment["segment_id"],
                     segment["to_node"], segment["carrier_id"], now),
                )
                self._container_event(connection, container_id, "arrival_reported",
                                      {"handover_id": handover_id}, actor_id, now)
                self._audit(connection, actor_id=actor_id, action="postal.container.arrived",
                            resource_type="container", resource_id=container_id,
                            detail={"handover_id": handover_id})
                return "handover", handover_id, {"handover_id": handover_id,
                                                 "container_id": container_id}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.arrive_container",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def _complete_arrival(self, connection, handover, actor_id: str, now: str) -> None:
        container = self._container(connection, handover["container_id"])
        segment = self._segment(connection, container["segment_id"])
        connection.execute(
            "UPDATE containers SET state='arrived',node_id=? "
            "WHERE container_id=? AND state='in_transit'",
            (segment["to_node"], container["container_id"]),
        )
        connection.execute(
            "UPDATE reservations SET state='released',released_at=? "
            "WHERE container_id=? AND state='held'", (now, container["container_id"]))
        self._container_event(connection, container["container_id"], "arrived",
                              {"node_id": segment["to_node"],
                               "handover_id": handover["handover_id"]}, actor_id, now)
        members = connection.execute(
            "SELECT * FROM parcels WHERE current_container=?", (container["container_id"],)
        ).fetchall()
        for member in members:
            connection.execute(
                "UPDATE parcels SET state='arrived',current_node=? WHERE parcel_id=?",
                (segment["to_node"], member["parcel_id"]),
            )
            plan = connection.execute(
                "SELECT * FROM route_plans WHERE parcel_id=? AND state='active'",
                (member["parcel_id"],),
            ).fetchone()
            if plan:
                connection.execute(
                    "UPDATE route_plan_legs SET state='completed' "
                    "WHERE plan_id=? AND segment_id=? AND state='in_use'",
                    (plan["plan_id"], segment["segment_id"]),
                )
            self._parcel_event(connection, member["parcel_id"], "arrived",
                               {"container_id": container["container_id"],
                                "node_id": segment["to_node"]}, actor_id, now)

    def open_inspection(self, *, request_id: str, actor_id: str, container_id: str,
                        reason: str) -> dict[str, Any]:
        """合规发起查验：袋内包裹一并挂起，责任从该节点开始记录。"""

        payload = {"actor_id": actor_id, "container_id": container_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "compliance")
            container_id = self._identifier(container_id, "container_id")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                container = self._container(connection, container_id)
                if container["state"] not in ("sealed", "arrived"):
                    raise ConflictError("只有已封袋或已到港的容器可以查验")
                now = self._now()
                connection.execute(
                    "UPDATE containers SET state='inspecting',inspection_return_state=? "
                    "WHERE container_id=?", (container["state"], container_id))
                members = connection.execute(
                    "SELECT * FROM parcels WHERE current_container=?", (container_id,)
                ).fetchall()
                held = []
                for member in members:
                    connection.execute(
                        "INSERT INTO responsibility_records(record_id,parcel_id,container_id,stage,"
                        "responsible_party,reason,node_id,started_at) VALUES(?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, member["parcel_id"], container_id, "inspection",
                         f"compliance@{container['node_id']}", reason, container["node_id"], now),
                    )
                    self._open_hold(connection, member["parcel_id"], "inspection", container_id,
                                    f"口岸查验：{reason}", "customs_inspection", now, actor_id)
                    self._parcel_event(connection, member["parcel_id"], "inspection_hold",
                                       {"container_id": container_id, "reason": reason},
                                       actor_id, now)
                    held.append(member["parcel_id"])
                self._container_event(connection, container_id, "inspection_opened",
                                      {"reason": reason, "held": held}, actor_id, now)
                self._audit(connection, actor_id=actor_id, action="postal.inspection.opened",
                            resource_type="container", resource_id=container_id,
                            detail={"reason": reason, "held": held})
                return "container", container_id, {"container_id": container_id,
                                                   "held_parcels": held}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.open_inspection",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def close_inspection(self, *, request_id: str, actor_id: str, container_id: str,
                         results: list[dict[str, Any]]) -> dict[str, Any]:
        """结束查验：逐包裹放行或扣留，查验责任即时闭环。"""

        payload = {"actor_id": actor_id, "container_id": container_id, "results": results}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "compliance")
            container_id = self._identifier(container_id, "container_id")
            if not isinstance(results, list) or not results:
                raise ValidationError("results 必须是非空列表")

            def create() -> tuple[str, str, dict[str, Any]]:
                container = self._container(connection, container_id)
                if container["state"] != "inspecting":
                    raise ConflictError("容器不在查验状态")
                members = {
                    row["parcel_id"]: row for row in connection.execute(
                        "SELECT * FROM parcels WHERE current_container=?", (container_id,)
                    ).fetchall()
                }
                outcome_by_parcel: dict[str, str] = {}
                note_by_parcel: dict[str, str] = {}
                for item in results:
                    if not isinstance(item, dict) or item.get("outcome") not in ("pass", "flag"):
                        raise ValidationError("每项查验结果必须包含 pass/flag 的 outcome")
                    parcel_id = self._identifier(str(item.get("parcel_id", "")), "parcel_id")
                    outcome_by_parcel[parcel_id] = item["outcome"]
                    note_by_parcel[parcel_id] = str(item.get("note") or "查验扣留")
                if set(outcome_by_parcel) != set(members):
                    raise ValidationError("查验结果必须覆盖袋内全部包裹")
                now = self._now()
                flagged: list[str] = []
                for parcel_id, outcome in outcome_by_parcel.items():
                    self._release_holds(connection, parcel_id, "inspection", now, actor_id)
                    if outcome == "flag":
                        flagged.append(parcel_id)
                        connection.execute(
                            "INSERT INTO container_membership(parcel_id,container_id,action,"
                            "node_id,reason,actor_id,occurred_at) "
                            "VALUES(?,?,'unloaded',?,?,?,?)",
                            (parcel_id, container_id, container["node_id"],
                             note_by_parcel[parcel_id], actor_id, now),
                        )
                        connection.execute(
                            "UPDATE containers SET loaded_count=loaded_count-1 "
                            "WHERE container_id=? AND loaded_count>0", (container_id,))
                        connection.execute(
                            "UPDATE parcels SET state='exception',current_container=NULL "
                            "WHERE parcel_id=?", (parcel_id,))
                        connection.execute(
                            "UPDATE route_plans SET state='abandoned' "
                            "WHERE parcel_id=? AND state='active'", (parcel_id,))
                        connection.execute(
                            "INSERT INTO responsibility_records(record_id,parcel_id,container_id,"
                            "stage,responsible_party,reason,node_id,started_at,ended_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (uuid.uuid4().hex, parcel_id, container_id, "inspection",
                             f"compliance@{container['node_id']}", note_by_parcel[parcel_id],
                             container["node_id"], now, now),
                        )
                        self._parcel_event(connection, parcel_id, "inspection_flagged",
                                           {"container_id": container_id,
                                            "note": note_by_parcel[parcel_id]}, actor_id, now)
                    else:
                        self._parcel_event(connection, parcel_id, "inspection_passed",
                                           {"container_id": container_id}, actor_id, now)
                connection.execute(
                    "UPDATE responsibility_records SET ended_at=? "
                    "WHERE container_id=? AND stage='inspection' AND ended_at IS NULL",
                    (now, container_id))
                return_state = container["inspection_return_state"] or "arrived"
                refreshed = self._container(connection, container_id)
                new_state = return_state
                if return_state == "arrived" and refreshed["loaded_count"] == 0:
                    new_state = "opened"
                connection.execute(
                    "UPDATE containers SET state=?,inspection_return_state=NULL "
                    "WHERE container_id=?", (new_state, container_id))
                self._container_event(connection, container_id, "inspection_closed",
                                      {"flagged": flagged}, actor_id, now)
                self._audit(connection, actor_id=actor_id, action="postal.inspection.closed",
                            resource_type="container", resource_id=container_id,
                            detail={"flagged": flagged})
                return "container", container_id, {"container_id": container_id,
                                                   "state": new_state, "flagged": flagged}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.close_inspection",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    # ------------------------------------------------------------------
    # 签收、改道、超时责任
    # ------------------------------------------------------------------

    def deliver_parcel(self, *, request_id: str, actor_id: str,
                       parcel_id: str) -> dict[str, Any]:
        """签收：写入一次性时间记录与承诺结果，超时包裹先落实责任归属。"""

        payload = {"actor_id": actor_id, "parcel_id": parcel_id}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "node")
            parcel_id = self._identifier(parcel_id, "parcel_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                parcel = self._parcel(connection, parcel_id)
                if operator["node_id"] != parcel["destination_node"]:
                    raise PermissionDenied("只能在目的节点签收")
                if parcel["state"] in TERMINAL_PARCEL_STATES:
                    raise ConflictError("包裹已完成，签收不可重复或改写")
                if parcel["current_node"] != parcel["destination_node"]:
                    raise ValidationError("包裹尚未到达目的节点")
                if parcel["current_container"]:
                    raise ValidationError("包裹仍在容器内，请先拆包")
                if self._open_hold_count(connection, parcel_id):
                    raise ConflictError("包裹存在未解除的扣留，不能签收")
                now = self._now()
                outcome = "met" if now <= parcel["deadline_at"] else "breached"
                if outcome == "breached":
                    self._ensure_overdue_record(connection, parcel, now)
                connection.execute(
                    "UPDATE parcels SET state='delivered',delivered_at=?,commitment_outcome=? "
                    "WHERE parcel_id=?", (now, outcome, parcel_id))
                connection.execute(
                    "UPDATE responsibility_records SET ended_at=? "
                    "WHERE parcel_id=? AND ended_at IS NULL", (now, parcel_id))
                connection.execute(
                    "UPDATE route_plans SET state='completed' WHERE parcel_id=? AND state='active'",
                    (parcel_id,))
                self._parcel_event(connection, parcel_id, "delivered",
                                   {"outcome": outcome, "deadline_at": parcel["deadline_at"]},
                                   operator["operator_id"], now)
                self._audit(connection, actor_id=actor_id, action="postal.parcel.delivered",
                            resource_type="parcel", resource_id=parcel_id,
                            detail={"outcome": outcome})
                return "parcel", parcel_id, {"parcel_id": parcel_id, "outcome": outcome,
                                             "delivered_at": now}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.deliver_parcel",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def reroute_parcel(self, *, request_id: str, actor_id: str, parcel_id: str,
                       reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "parcel_id": parcel_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            parcel_id = self._identifier(parcel_id, "parcel_id")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                parcel = self._parcel(connection, parcel_id)
                if not (operator["role"] in ("admin", "compliance")
                        or (operator["role"] == "node"
                            and operator["node_id"] == parcel["current_node"])):
                    raise PermissionDenied("只有当前节点、合规或管理员可以发起改道")
                outcome = self._replan(connection, parcel, reason, actor_id,
                                       waitlist_on_failure=True)
                self._audit(connection, actor_id=actor_id, action="postal.parcel.rerouted",
                            resource_type="parcel", resource_id=parcel_id,
                            detail={"reason": reason, "outcome": outcome})
                return "parcel", parcel_id, {"parcel_id": parcel_id, **outcome}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.reroute_parcel",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def evaluate_timeouts(self, *, request_id: str, actor_id: str) -> dict[str, Any]:
        """扫描超时包裹并落实当前持有方责任；重复执行不会产生重复记录。"""

        payload = {"actor_id": actor_id}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "admin", "node", "carrier", "compliance")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                scanned = 0
                opened = 0
                rows = connection.execute(
                    "SELECT * FROM parcels WHERE state NOT IN ('delivered','exception') "
                    "AND deadline_at<?", (now,),
                ).fetchall()
                for parcel in rows:
                    scanned += 1
                    if self._ensure_overdue_record(connection, parcel, now):
                        opened += 1
                scan_id = uuid.uuid4().hex
                self._audit(connection, actor_id=actor_id, action="postal.timeouts.evaluated",
                            resource_type="timeout_scan", resource_id=scan_id,
                            detail={"scanned": scanned, "records_opened": opened})
                return "timeout_scan", scan_id, {"scanned": scanned, "records_opened": opened}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.evaluate_timeouts",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def _ensure_overdue_record(self, connection, parcel, now: str) -> bool:
        stage, party, container_id = self._current_holder(connection, parcel)
        open_records = connection.execute(
            "SELECT * FROM responsibility_records WHERE parcel_id=? "
            "AND reason='commitment_overdue' AND ended_at IS NULL", (parcel["parcel_id"],),
        ).fetchall()
        for record in open_records:
            if record["stage"] == stage and record["responsible_party"] == party:
                return False
        for record in open_records:
            connection.execute(
                "UPDATE responsibility_records SET ended_at=? WHERE record_id=?",
                (now, record["record_id"]))
        connection.execute(
            "INSERT INTO responsibility_records(record_id,parcel_id,container_id,stage,"
            "responsible_party,reason,node_id,started_at) VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, parcel["parcel_id"], container_id, stage, party,
             "commitment_overdue", parcel["current_node"], now),
        )
        return True

    def _current_holder(self, connection, parcel) -> tuple[str, str, str | None]:
        if parcel["current_container"]:
            container = self._container(connection, parcel["current_container"])
            if container["state"] == "in_transit":
                segment = self._segment(connection, container["segment_id"])
                return "carrier_transit", f"carrier:{segment['carrier_id']}", \
                    container["container_id"]
            if container["state"] == "inspecting":
                return "inspection", f"compliance@{container['node_id']}", \
                    container["container_id"]
            return "node_hold", f"node:{container['node_id']}", container["container_id"]
        if self._waiting_entry(connection, parcel["parcel_id"]):
            return "waitlist", f"node:{parcel['current_node']}", None
        return "node_hold", f"node:{parcel['current_node']}", None

    # ------------------------------------------------------------------
    # 口岸状态与影响推导
    # ------------------------------------------------------------------

    def set_node_status(self, *, request_id: str, actor_id: str, node_id: str,
                        status: str, reason: str) -> dict[str, Any]:
        """开/关/限流口岸；关闭时正向推导受影响承诺并触发改道或候补。"""

        payload = {"actor_id": actor_id, "node_id": node_id, "status": status, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            operator = self._operator(connection, actor_id)
            self._require(operator, "admin", "compliance")
            node_id = self._identifier(node_id, "node_id")
            if status not in NODE_STATUSES:
                raise ValidationError("status 不在允许范围内")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._node(connection, node_id)
                now = self._now()
                connection.execute(
                    "INSERT INTO node_status_events(event_id,node_id,status,reason,actor_id,"
                    "occurred_at) VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, node_id, status, reason, actor_id, now),
                )
                connection.execute("UPDATE nodes SET status=? WHERE node_id=?",
                                   (status, node_id))
                report_id = None
                affected: list[dict[str, Any]] = []
                promoted: list[str] = []
                if status == "closed":
                    affected = self._disrupt_node(connection, node_id, reason, actor_id, now)
                    report_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO impact_reports(report_id,node_id,trigger,detail_json,"
                        "created_at) VALUES(?,?,?,?,?)",
                        (report_id, node_id, "node_closed", canonical_json({
                            "node_id": node_id, "status": status, "reason": reason,
                            "affected": affected, "generated_at": now,
                        }), now),
                    )
                elif status == "open":
                    promoted = self._process_waitlist(connection, None, actor_id)
                self._audit(connection, actor_id=actor_id, action="postal.node.status_changed",
                            resource_type="node", resource_id=node_id,
                            detail={"status": status, "reason": reason,
                                    "affected": len(affected), "promoted": promoted})
                return "node", node_id, {"node_id": node_id, "status": status,
                                         "affected": affected, "report_id": report_id,
                                         "promoted": promoted}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="postal.set_node_status",
                                                 payload=payload, create=create)
            return self._result(receipt, response)

    def _disrupt_node(self, connection, node_id: str, reason: str,
                      actor_id: str, now: str) -> list[dict[str, Any]]:
        affected: list[dict[str, Any]] = []
        containers = connection.execute(
            "SELECT * FROM containers WHERE node_id=? AND state IN ('open','sealed')", (node_id,)
        ).fetchall()
        for container in containers:
            affected.append({"type": "container", "container_id": container["container_id"],
                             "state": container["state"], "note": "发运受阻"})
        inbound = connection.execute(
            "SELECT c.* FROM containers c JOIN segments s ON c.segment_id=s.segment_id "
            "WHERE c.state='in_transit' AND s.to_node=?", (node_id,)
        ).fetchall()
        for container in inbound:
            affected.append({"type": "inbound_container",
                             "container_id": container["container_id"],
                             "note": "到达后需改道"})
        parcels = connection.execute(
            "SELECT * FROM parcels WHERE state NOT IN ('delivered','exception')"
        ).fetchall()
        for parcel in parcels:
            entry = {"type": "parcel", "parcel_id": parcel["parcel_id"],
                     "commitment_id": parcel["commitment_id"],
                     "deadline_at": parcel["deadline_at"]}
            if parcel["current_container"]:
                container = self._container(connection, parcel["current_container"])
                segment = self._segment(connection, container["segment_id"])
                if (container["node_id"] == node_id and container["state"] in ("open", "sealed")) \
                        or (container["state"] == "in_transit"
                            and segment["to_node"] == node_id):
                    affected.append({**entry, "action": "blocked_in_container",
                                     "container_id": container["container_id"]})
                continue
            plan = connection.execute(
                "SELECT * FROM route_plans WHERE parcel_id=? AND state='active'",
                (parcel["parcel_id"],),
            ).fetchone()
            if plan is None:
                continue
            legs = connection.execute(
                "SELECT * FROM route_plan_legs WHERE plan_id=? AND state='pending'",
                (plan["plan_id"],),
            ).fetchall()
            hit = any(leg["from_node"] == node_id or leg["to_node"] == node_id for leg in legs)
            if not hit:
                continue
            outcome = self._replan(connection, parcel, f"口岸关闭 {node_id}", actor_id,
                                   waitlist_on_failure=True)
            affected.append({**entry, "action": outcome["outcome"],
                             **({"plan_version": outcome["version"]}
                                if outcome["outcome"] == "planned" else {})})
        return affected

    def _disrupt_segment(self, connection, segment, actor_id: str,
                         now: str) -> list[dict[str, Any]]:
        affected: list[dict[str, Any]] = []
        containers = connection.execute(
            "SELECT * FROM containers WHERE segment_id=? AND state IN ('open','sealed')",
            (segment["segment_id"],),
        ).fetchall()
        for container in containers:
            affected.append({"type": "container", "container_id": container["container_id"],
                             "state": container["state"], "note": "区段暂停"})
        parcels = connection.execute(
            "SELECT * FROM parcels WHERE state NOT IN ('delivered','exception') "
            "AND current_container IS NULL"
        ).fetchall()
        for parcel in parcels:
            plan = connection.execute(
                "SELECT * FROM route_plans WHERE parcel_id=? AND state='active'",
                (parcel["parcel_id"],),
            ).fetchone()
            if plan is None:
                continue
            hit = connection.execute(
                "SELECT 1 FROM route_plan_legs WHERE plan_id=? AND segment_id=? "
                "AND state='pending'", (plan["plan_id"], segment["segment_id"]),
            ).fetchone()
            if not hit:
                continue
            outcome = self._replan(connection, parcel,
                                   f"区段暂停 {segment['segment_id']}", actor_id,
                                   waitlist_on_failure=True)
            affected.append({"type": "parcel", "parcel_id": parcel["parcel_id"],
                             "commitment_id": parcel["commitment_id"],
                             "deadline_at": parcel["deadline_at"],
                             "action": outcome["outcome"]})
        return affected

    def _process_waitlist(self, connection, node_id: str | None, actor_id: str) -> list[str]:
        """按候补中位次重试规划；node_id 为 None 时重试全部节点。"""

        promoted: list[str] = []
        query = ("SELECT * FROM waitlist_entries WHERE state='waiting'")
        parameters: list[Any] = []
        if node_id:
            query += " AND node_id=?"
            parameters.append(node_id)
        query += " ORDER BY created_at, entry_id"
        entries = connection.execute(query, parameters).fetchall()
        for entry in entries:
            parcel = connection.execute(
                "SELECT * FROM parcels WHERE parcel_id=?", (entry["parcel_id"],)
            ).fetchone()
            if parcel is None or parcel["state"] in TERMINAL_PARCEL_STATES \
                    or parcel["current_container"]:
                continue
            try:
                outcome = self._replan(connection, parcel, "候补重试", actor_id,
                                       waitlist_on_failure=True)
            except ValidationError:
                continue
            if outcome["outcome"] == "planned":
                promoted.append(parcel["parcel_id"])
        return promoted

    # ------------------------------------------------------------------
    # 规划与重估（内部）
    # ------------------------------------------------------------------

    def _replan(self, connection, parcel, reason: str, actor_id: str,
                waitlist_on_failure: bool) -> dict[str, Any]:
        parcel_id = parcel["parcel_id"]
        now = self._now()
        if parcel["state"] in TERMINAL_PARCEL_STATES:
            raise ValidationError("包裹已完成，不能改道")
        if parcel["current_container"]:
            raise ValidationError("包裹在容器内，需先拆包再改道")
        if parcel["current_node"] == parcel["destination_node"]:
            return {"outcome": "at_destination"}
        active = connection.execute(
            "SELECT * FROM route_plans WHERE parcel_id=? AND state='active'", (parcel_id,)
        ).fetchone()
        row = connection.execute(
            "SELECT MAX(version) AS version FROM route_plans WHERE parcel_id=?", (parcel_id,)
        ).fetchone()
        version = (row["version"] or 0) + 1
        if active:
            connection.execute("UPDATE route_plans SET state='superseded' WHERE plan_id=?",
                               (active["plan_id"],))
            connection.execute(
                "UPDATE route_plan_legs SET state='skipped' WHERE plan_id=? AND state='pending'",
                (active["plan_id"],))
        segments, jurisdictions, statuses, rules = self._planning_inputs(connection)
        facts = self._parcel_facts(connection, parcel)
        standbys = connection.execute(
            "SELECT * FROM standby_routes WHERE parcel_id=? AND state='ready' ORDER BY rank",
            (parcel_id,),
        ).fetchall()
        for standby in standbys:
            leg_ids = json.loads(standby["legs_json"])
            legs = [segments.get(item) for item in leg_ids]
            if not legs or any(item is None for item in legs) \
                    or legs[0].from_node != parcel["current_node"]:
                connection.execute(
                    "UPDATE standby_routes SET state='stale' WHERE standby_id=?",
                    (standby["standby_id"],))
                continue
            ok, evaluations, notes = assess_path(legs, facts, jurisdictions, statuses, rules)
            self._record_evaluations(connection, parcel_id, None, evaluations, now)
            if not ok:
                connection.execute(
                    "UPDATE standby_routes SET state='failed' WHERE standby_id=?",
                    (standby["standby_id"],))
                continue
            plan_id = self._create_plan(
                connection, parcel, version, legs,
                f"启用候补方案（稳定优先级 {standby['rank']}）：" + ("；".join(notes) or "路径可用"),
                reason, actor_id, now)
            connection.execute("UPDATE standby_routes SET state='promoted' WHERE standby_id=?",
                               (standby["standby_id"],))
            connection.execute(
                "UPDATE standby_routes SET state='stale' WHERE parcel_id=? AND state='ready'",
                (parcel_id,))
            self._after_plan_created(connection, parcel, plan_id, version, reason, actor_id, now)
            return {"outcome": "planned", "plan_id": plan_id, "version": version,
                    "via": f"standby:{standby['rank']}"}
        result = plan_routes(facts, parcel["current_node"], parcel["destination_node"],
                             list(segments.values()), jurisdictions, statuses, rules)
        connection.execute(
            "UPDATE standby_routes SET state='stale' WHERE parcel_id=? AND state='ready'",
            (parcel_id,))
        if result["primary"] is None:
            self._record_evaluations(connection, parcel_id, None, result["evaluations"], now)
            if waitlist_on_failure:
                self._ensure_waitlisted(connection, parcel, reason, actor_id, now)
                return {"outcome": "waitlisted"}
            return {"outcome": "blocked"}
        plan_id = self._create_plan(connection, parcel, version, result["primary"]["legs"],
                                    result["summary"], reason, actor_id, now)
        self._record_evaluations(connection, parcel_id, plan_id, result["evaluations"], now)
        for rank, standby in enumerate(result["standbys"], start=1):
            connection.execute(
                "INSERT INTO standby_routes(standby_id,parcel_id,plan_version,rank,legs_json,"
                "reason_json,state,created_at) VALUES(?,?,?,?,?,?, 'ready',?)",
                (uuid.uuid4().hex, parcel_id, version, rank,
                 canonical_json([item.segment_id for item in standby["legs"]]),
                 canonical_json({"total_hours": standby["total_hours"],
                                 "notes": standby["notes"]}), now),
            )
        self._after_plan_created(connection, parcel, plan_id, version, reason, actor_id, now)
        return {"outcome": "planned", "plan_id": plan_id, "version": version, "via": "computed"}

    def _create_plan(self, connection, parcel, version: int, legs: list[SegmentInfo],
                     summary: str, reason: str, actor_id: str, now: str) -> str:
        plan_id = uuid.uuid4().hex
        total_hours = sum(item.transit_hours for item in legs)
        connection.execute(
            "INSERT INTO route_plans(plan_id,parcel_id,version,state,reason_json,created_at) "
            "VALUES(?,?,?, 'active',?,?)",
            (plan_id, parcel["parcel_id"], version,
             canonical_json({"trigger": reason, "summary": summary,
                             "total_hours": total_hours}), now),
        )
        for index, segment in enumerate(legs):
            leg_reason = (
                f"第{index + 1}程：区段{segment.segment_id}"
                f"（{segment.from_node}→{segment.to_node}），承运方{segment.carrier_id}，"
                f"方式{segment.mode}，预计{segment.transit_hours}小时，"
                f"稳定优先级{segment.priority}"
            )
            connection.execute(
                "INSERT INTO route_plan_legs(plan_id,leg_index,segment_id,from_node,to_node,"
                "carrier_id,transit_hours,state,reason) VALUES(?,?,?,?,?,?,?, 'pending',?)",
                (plan_id, index, segment.segment_id, segment.from_node, segment.to_node,
                 segment.carrier_id, segment.transit_hours, leg_reason),
            )
        return plan_id

    def _after_plan_created(self, connection, parcel, plan_id: str, version: int,
                            reason: str, actor_id: str, now: str) -> None:
        parcel_id = parcel["parcel_id"]
        connection.execute(
            "UPDATE parcels SET state='planned' WHERE parcel_id=? "
            "AND state IN ('accepted','arrived','planned')", (parcel_id,))
        entry = self._waiting_entry(connection, parcel_id)
        if entry:
            connection.execute(
                "UPDATE waitlist_entries SET state='promoted',resolved_at=? WHERE entry_id=?",
                (now, entry["entry_id"]))
        connection.execute(
            "UPDATE responsibility_records SET ended_at=? "
            "WHERE parcel_id=? AND stage='waitlist' AND ended_at IS NULL", (now, parcel_id))
        self._parcel_event(connection, parcel_id, "replanned",
                           {"reason": reason, "plan_id": plan_id, "version": version},
                           actor_id, now)

    def _ensure_waitlisted(self, connection, parcel, reason: str,
                           actor_id: str, now: str) -> str:
        existing = self._waiting_entry(connection, parcel["parcel_id"])
        if existing:
            return existing["entry_id"]
        entry_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO waitlist_entries(entry_id,parcel_id,node_id,reason,state,created_at) "
            "VALUES(?,?,?,?, 'waiting',?)",
            (entry_id, parcel["parcel_id"], parcel["current_node"], reason, now),
        )
        connection.execute(
            "INSERT INTO responsibility_records(record_id,parcel_id,container_id,stage,"
            "responsible_party,reason,node_id,started_at) VALUES(?,?,NULL,?,?,?,?,?)",
            (uuid.uuid4().hex, parcel["parcel_id"], "waitlist",
             f"node:{parcel['current_node']}", "awaiting_route", parcel["current_node"], now),
        )
        self._parcel_event(connection, parcel["parcel_id"], "waitlisted",
                           {"reason": reason, "node_id": parcel["current_node"]}, actor_id, now)
        return entry_id

    def _route_usable(self, connection, parcel_id: str) -> bool:
        plan = connection.execute(
            "SELECT * FROM route_plans WHERE parcel_id=? AND state='active'", (parcel_id,)
        ).fetchone()
        if plan is None:
            return False
        leg = connection.execute(
            "SELECT * FROM route_plan_legs WHERE plan_id=? AND state='pending' "
            "ORDER BY leg_index LIMIT 1", (plan["plan_id"],),
        ).fetchone()
        if leg is None:
            return True
        segment = self._segment(connection, leg["segment_id"])
        if segment["status"] != "active":
            return False
        for node_id in (leg["from_node"], leg["to_node"]):
            if self._node(connection, node_id)["status"] == "closed":
                return False
        return True

    def _next_leg(self, connection, parcel_id: str):
        plan = connection.execute(
            "SELECT * FROM route_plans WHERE parcel_id=? AND state='active'", (parcel_id,)
        ).fetchone()
        if plan is None:
            return None
        return connection.execute(
            "SELECT * FROM route_plan_legs WHERE plan_id=? AND state='pending' "
            "ORDER BY leg_index LIMIT 1", (plan["plan_id"],),
        ).fetchone()

    def _planning_inputs(self, connection):
        segments: dict[str, SegmentInfo] = {}
        for row in connection.execute("SELECT * FROM segments").fetchall():
            segments[row["segment_id"]] = SegmentInfo(
                row["segment_id"], row["from_node"], row["to_node"], row["carrier_id"],
                row["mode"], row["transit_hours"], row["priority"], row["status"])
        jurisdictions: dict[str, str] = {}
        statuses: dict[str, str] = {}
        for row in connection.execute("SELECT * FROM nodes").fetchall():
            jurisdictions[row["node_id"]] = row["jurisdiction"]
            statuses[row["node_id"]] = row["status"]
        rules = [
            RuleInfo(row["rule_id"], row["version"], row["jurisdiction"], row["scope"],
                     row["rule_type"], json.loads(row["expression_json"]))
            for row in connection.execute("SELECT * FROM rules WHERE status='active'").fetchall()
        ]
        return segments, jurisdictions, statuses, rules

    def _parcel_facts(self, connection, parcel) -> ParcelFacts:
        declaration = connection.execute(
            "SELECT * FROM declarations WHERE parcel_id=? AND version=?",
            (parcel["parcel_id"], parcel["declaration_version"]),
        ).fetchone()
        proof_kinds = set()
        if declaration:
            for proof in json.loads(declaration["proofs_json"]):
                if isinstance(proof, dict) and proof.get("kind"):
                    proof_kinds.add(str(proof["kind"]))
        return ParcelFacts(
            parcel_id=parcel["parcel_id"], category=parcel["category"],
            declared_value=parcel["declared_value"], currency=parcel["currency"],
            proof_kinds=frozenset(proof_kinds), origin_node=parcel["origin_node"],
            destination_node=parcel["destination_node"])

    def _remaining_scopes(self, connection, parcel) -> list[tuple[str, str, str]]:
        plan = connection.execute(
            "SELECT * FROM route_plans WHERE parcel_id=? AND state='active'",
            (parcel["parcel_id"],),
        ).fetchone()
        sequence = [parcel["current_node"]]
        if plan:
            legs = connection.execute(
                "SELECT * FROM route_plan_legs WHERE plan_id=? AND state='pending' "
                "ORDER BY leg_index", (plan["plan_id"],),
            ).fetchall()
            sequence.extend(leg["to_node"] for leg in legs)
        if parcel["current_node"] != parcel["destination_node"] \
                and parcel["destination_node"] not in sequence:
            sequence.append(parcel["destination_node"])
        result = []
        for index, node_id in enumerate(sequence):
            node = self._node(connection, node_id)
            if index == 0 and node_id == parcel["origin_node"]:
                scope = "origin"
            elif node_id == parcel["destination_node"]:
                scope = "destination"
            else:
                scope = "transit"
            result.append((node_id, node["jurisdiction"], scope))
        return result

    def _record_evaluations(self, connection, parcel_id: str, plan_id: str | None,
                            evaluations: list[dict[str, Any]], now: str) -> None:
        for item in evaluations:
            connection.execute(
                "INSERT INTO rule_evaluations(parcel_id,plan_id,rule_id,rule_version,jurisdiction,"
                "scope,result,reason,evaluated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (parcel_id, plan_id, item["rule_id"], item["version"], item["jurisdiction"],
                 item["scope"], item["result"], item["reason"], now),
            )

    def _reevaluate_rule(self, connection, rule, actor_id: str, now: str) -> list[str]:
        rule_info = RuleInfo(rule["rule_id"], rule["version"], rule["jurisdiction"],
                             rule["scope"], rule["rule_type"],
                             json.loads(rule["expression_json"]))
        affected: list[str] = []
        candidates = connection.execute(
            "SELECT * FROM parcels WHERE state NOT IN ('delivered','exception')"
        ).fetchall()
        for parcel in candidates:
            facts = self._parcel_facts(connection, parcel)
            for _node, jurisdiction, scope in self._remaining_scopes(connection, parcel):
                if jurisdiction != rule_info.jurisdiction or scope != rule_info.scope:
                    continue
                result, reason = evaluate_rule(rule_info, facts, scope)
                if result == "not_applicable":
                    continue
                self._record_evaluations(connection, parcel["parcel_id"], None, [{
                    "rule_id": rule_info.rule_id, "version": rule_info.version,
                    "jurisdiction": jurisdiction, "scope": scope,
                    "result": result, "reason": reason,
                }], now)
                source = f"rule:{rule_info.rule_id}"
                if result == "violation":
                    self._open_hold(connection, parcel["parcel_id"], source, rule_info.rule_id,
                                    f"规则{rule_info.rule_id}@v{rule_info.version}：{reason}",
                                    "compliance_review", now, actor_id)
                    affected.append(parcel["parcel_id"])
                else:
                    self._release_holds(connection, parcel["parcel_id"], source, now, actor_id)
        for parcel_id in dict.fromkeys(affected):
            parcel = self._parcel(connection, parcel_id)
            if parcel["current_container"] is None \
                    and parcel["state"] not in TERMINAL_PARCEL_STATES:
                try:
                    self._replan(connection, parcel,
                                 f"规则{rule_info.rule_id}@v{rule_info.version}生效",
                                 actor_id, waitlist_on_failure=False)
                except ValidationError:
                    pass
        return sorted(set(affected))

    def _reevaluate_parcel(self, connection, parcel, actor_id: str, now: str) -> None:
        facts = self._parcel_facts(connection, parcel)
        _segments, _jurisdictions, _statuses, rules = self._planning_inputs(connection)
        scopes = self._remaining_scopes(connection, parcel)
        seen_sources: set[str] = set()
        for rule in rules:
            for _node, jurisdiction, scope in scopes:
                if jurisdiction != rule.jurisdiction or scope != rule.scope:
                    continue
                result, reason = evaluate_rule(rule, facts, scope)
                if result == "not_applicable":
                    continue
                self._record_evaluations(connection, parcel["parcel_id"], None, [{
                    "rule_id": rule.rule_id, "version": rule.version,
                    "jurisdiction": jurisdiction, "scope": scope,
                    "result": result, "reason": reason,
                }], now)
                source = f"rule:{rule.rule_id}"
                seen_sources.add(source)
                if result == "violation":
                    self._open_hold(connection, parcel["parcel_id"], source, rule.rule_id,
                                    f"规则{rule.rule_id}@v{rule.version}：{reason}",
                                    "compliance_review", now, actor_id)
                else:
                    self._release_holds(connection, parcel["parcel_id"], source, now, actor_id)
        open_sources = {
            row["source"] for row in connection.execute(
                "SELECT source FROM parcel_holds WHERE parcel_id=? AND state='open' "
                "AND source LIKE 'rule:%'", (parcel["parcel_id"],),
            ).fetchall()
        }
        for source in open_sources - seen_sources:
            self._release_holds(connection, parcel["parcel_id"], source, now, actor_id)

    def _open_hold(self, connection, parcel_id: str, source: str, source_id: str,
                   reason: str, public_reason: str, now: str, actor_id: str) -> bool:
        existing = connection.execute(
            "SELECT 1 FROM parcel_holds WHERE parcel_id=? AND source=? AND state='open'",
            (parcel_id, source),
        ).fetchone()
        if existing:
            return False
        connection.execute(
            "INSERT INTO parcel_holds(hold_id,parcel_id,source,source_id,reason,public_reason,"
            "state,opened_at) VALUES(?,?,?,?,?,?, 'open',?)",
            (uuid.uuid4().hex, parcel_id, source, source_id, reason, public_reason, now),
        )
        self._parcel_event(connection, parcel_id, "hold_opened",
                           {"source": source, "reason": reason}, actor_id, now)
        return True

    def _release_holds(self, connection, parcel_id: str, source: str,
                       now: str, actor_id: str) -> int:
        rows = connection.execute(
            "SELECT * FROM parcel_holds WHERE parcel_id=? AND source=? AND state='open'",
            (parcel_id, source),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE parcel_holds SET state='released',released_at=? WHERE hold_id=?",
                (now, row["hold_id"]))
            self._parcel_event(connection, parcel_id, "hold_released",
                               {"source": source}, actor_id, now)
        return len(rows)

    def _open_hold_count(self, connection, parcel_id: str, rule_only: bool = False) -> int:
        query = "SELECT COUNT(*) AS count FROM parcel_holds WHERE parcel_id=? AND state='open'"
        if rule_only:
            query += " AND source LIKE 'rule:%'"
        return connection.execute(query, (parcel_id,)).fetchone()["count"]

    def _waiting_entry(self, connection, parcel_id: str):
        return connection.execute(
            "SELECT * FROM waitlist_entries WHERE parcel_id=? AND state='waiting'",
            (parcel_id,),
        ).fetchone()

    def _held_units(self, connection, segment_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(units),0) AS units FROM reservations "
            "WHERE segment_id=? AND state='held'", (segment_id,),
        ).fetchone()
        return int(row["units"])

    # ------------------------------------------------------------------
    # 查询：追溯、谱系、影响、候补、客户支持视图
    # ------------------------------------------------------------------

    def parcel_trace(self, *, actor_id: str, parcel_id: str) -> dict[str, Any]:
        """从一个包裹回看全部规则评估、容器谱系、交接与责任人。"""

        connection = self.database.connection
        operator = self._operator(connection, actor_id)
        if operator["role"] == "support":
            raise PermissionDenied("客户支持只能查询可披露视图")
        parcel = self._parcel(connection, parcel_id)
        plans = []
        for plan in connection.execute(
                "SELECT * FROM route_plans WHERE parcel_id=? ORDER BY version",
                (parcel_id,)).fetchall():
            legs = [dict(row) for row in connection.execute(
                "SELECT * FROM route_plan_legs WHERE plan_id=? ORDER BY leg_index",
                (plan["plan_id"],)).fetchall()]
            plans.append({"plan": {**dict(plan), "reason": json.loads(plan["reason_json"])},
                          "legs": legs})
        memberships = [dict(row) for row in connection.execute(
            "SELECT * FROM container_membership WHERE parcel_id=? ORDER BY seq",
            (parcel_id,)).fetchall()]
        container_ids = sorted({row["container_id"] for row in memberships})
        handovers = []
        if container_ids:
            marks = ",".join("?" for _ in container_ids)
            handovers = [dict(row) for row in connection.execute(
                f"SELECT * FROM handovers WHERE container_id IN ({marks}) "
                "ORDER BY created_at, handover_id", container_ids).fetchall()]
        return {
            "parcel": dict(parcel),
            "declarations": [dict(row) for row in connection.execute(
                "SELECT * FROM declarations WHERE parcel_id=? ORDER BY version",
                (parcel_id,)).fetchall()],
            "plans": plans,
            "standbys": [dict(row) for row in connection.execute(
                "SELECT * FROM standby_routes WHERE parcel_id=? ORDER BY created_at, rank",
                (parcel_id,)).fetchall()],
            "containers": memberships,
            "handovers": handovers,
            "holds": [dict(row) for row in connection.execute(
                "SELECT * FROM parcel_holds WHERE parcel_id=? ORDER BY opened_at",
                (parcel_id,)).fetchall()],
            "rule_evaluations": [dict(row) for row in connection.execute(
                "SELECT * FROM rule_evaluations WHERE parcel_id=? ORDER BY seq",
                (parcel_id,)).fetchall()],
            "responsibility": [dict(row) for row in connection.execute(
                "SELECT * FROM responsibility_records WHERE parcel_id=? ORDER BY started_at",
                (parcel_id,)).fetchall()],
            "waitlist": [dict(row) for row in connection.execute(
                "SELECT * FROM waitlist_entries WHERE parcel_id=? ORDER BY created_at",
                (parcel_id,)).fetchall()],
            "events": [dict(row) for row in connection.execute(
                "SELECT * FROM parcel_events WHERE parcel_id=? ORDER BY seq",
                (parcel_id,)).fetchall()],
        }

    def container_lineage(self, *, actor_id: str, container_id: str) -> dict[str, Any]:
        """从容器一侧查看双向谱系：当前与历史装载、交接与预留。"""

        connection = self.database.connection
        operator = self._operator(connection, actor_id)
        if operator["role"] == "support":
            raise PermissionDenied("客户支持只能查询可披露视图")
        container = self._container(connection, container_id)
        current = [row["parcel_id"] for row in connection.execute(
            "SELECT parcel_id FROM parcels WHERE current_container=? ORDER BY parcel_id",
            (container_id,)).fetchall()]
        return {
            "container": dict(container),
            "current_members": current,
            "membership_history": [dict(row) for row in connection.execute(
                "SELECT * FROM container_membership WHERE container_id=? ORDER BY seq",
                (container_id,)).fetchall()],
            "handovers": [dict(row) for row in connection.execute(
                "SELECT * FROM handovers WHERE container_id=? ORDER BY created_at",
                (container_id,)).fetchall()],
            "reservations": [dict(row) for row in connection.execute(
                "SELECT * FROM reservations WHERE container_id=? ORDER BY created_at",
                (container_id,)).fetchall()],
            "events": [dict(row) for row in connection.execute(
                "SELECT * FROM container_events WHERE container_id=? ORDER BY seq",
                (container_id,)).fetchall()],
        }

    def node_impact(self, *, actor_id: str, node_id: str) -> dict[str, Any]:
        """从口岸正向推导：状态事件、影响报告、候补与在途进港容器。"""

        connection = self.database.connection
        operator = self._operator(connection, actor_id)
        if operator["role"] == "support":
            raise PermissionDenied("客户支持只能查询可披露视图")
        node = self._node(connection, node_id)
        reports = []
        for row in connection.execute(
                "SELECT * FROM impact_reports WHERE node_id=? ORDER BY created_at",
                (node_id,)).fetchall():
            reports.append({"report_id": row["report_id"], "trigger": row["trigger"],
                            "created_at": row["created_at"],
                            "detail": json.loads(row["detail_json"])})
        inbound = [dict(row) for row in connection.execute(
            "SELECT c.* FROM containers c JOIN segments s ON c.segment_id=s.segment_id "
            "WHERE c.state='in_transit' AND s.to_node=?", (node_id,)).fetchall()]
        return {
            "node": dict(node),
            "status_events": [dict(row) for row in connection.execute(
                "SELECT * FROM node_status_events WHERE node_id=? ORDER BY occurred_at",
                (node_id,)).fetchall()],
            "reports": reports,
            "waitlist": self._waitlist_rows(connection, node_id),
            "inbound_containers": inbound,
        }

    def list_waitlist(self, *, actor_id: str, node_id: str | None = None) -> dict[str, Any]:
        connection = self.database.connection
        operator = self._operator(connection, actor_id)
        if operator["role"] == "support":
            raise PermissionDenied("客户支持只能查询可披露视图")
        return {"items": self._waitlist_rows(connection, node_id)}

    def _waitlist_rows(self, connection, node_id: str | None) -> list[dict[str, Any]]:
        query = "SELECT * FROM waitlist_entries WHERE state='waiting'"
        parameters: list[Any] = []
        if node_id:
            query += " AND node_id=?"
            parameters.append(node_id)
        query += " ORDER BY node_id, created_at, entry_id"
        rows = [dict(row) for row in connection.execute(query, parameters).fetchall()]
        positions: dict[str, int] = {}
        for row in rows:
            positions[row["node_id"]] = positions.get(row["node_id"], 0) + 1
            row["position"] = positions[row["node_id"]]
        return rows

    def support_view(self, *, actor_id: str, parcel_id: str) -> dict[str, Any]:
        """客户支持可披露视图：只有状态、可披露原因、所在口岸与承诺信息。"""

        connection = self.database.connection
        self._operator(connection, actor_id)
        parcel = self._parcel(connection, parcel_id)
        node = self._node(connection, parcel["current_node"])
        hold = connection.execute(
            "SELECT * FROM parcel_holds WHERE parcel_id=? AND state='open' "
            "ORDER BY opened_at LIMIT 1", (parcel_id,),
        ).fetchone()
        waiting = self._waiting_entry(connection, parcel_id)
        if parcel["state"] == "delivered":
            code = "delivered"
        elif parcel["state"] == "exception":
            code = "exception"
        elif hold:
            code = hold["public_reason"]
        elif waiting:
            code = "capacity_wait"
        elif parcel["state"] == "in_transit":
            code = "in_transit"
        else:
            code = "processing"
        last_event = connection.execute(
            "SELECT occurred_at FROM parcel_events WHERE parcel_id=? ORDER BY seq DESC LIMIT 1",
            (parcel_id,),
        ).fetchone()
        commitment = connection.execute(
            "SELECT * FROM commitments WHERE commitment_id=?", (parcel["commitment_id"],),
        ).fetchone()
        return {
            "parcel_id": parcel_id,
            "status_code": code,
            "status_text": PUBLIC_STATUS_TEXT[code],
            "location": node["name"],
            "updated_at": last_event["occurred_at"] if last_event else parcel["accepted_at"],
            "commitment": {
                "product": commitment["product"] if commitment else None,
                "deadline_at": parcel["deadline_at"],
                "outcome": parcel["commitment_outcome"],
            },
        }
