"""跨境邮政路由与责任编排的领域服务。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from digital_trade_foundation.audit import append_event, canonical_json
from digital_trade_foundation.clock import Clock
from digital_trade_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from digital_trade_foundation.models import WriteReceipt
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database

from .rules import active_rules, evaluate_path, find_paths, selector_matches
from .storage import install


OPERATOR_ROLES = ("admin", "operator")
NODE_ROLES = ("admin", "operator", "node")
CARRIER_ROLES = ("admin", "carrier")
COMPLIANCE_ROLES = ("admin", "compliance")
READER_ROLES = ("admin", "operator", "auditor", "node", "carrier", "compliance", "reviewer")
PARTY_ROLES = {"node": "node", "carrier": "carrier", "compliance": "compliance"}
FINAL_PARCEL_STATUS = "delivered"

SUPPORT_STATUS = {
    "registered": "已收寄，等待发运",
    "planned": "已收寄，等待发运",
    "consolidated": "已装袋，等待发运",
    "in_transit": "运输途中",
    "arrived": "已到达中转口岸",
    "inspection": "口岸查验中，时效将顺延",
    "held": "暂存待处理",
    "delivered": "已签收",
}


class PostalService:
    """在基础服务的权限、幂等、事务与审计边界上编排邮政路由。"""

    def __init__(self, database: Database, foundation: DomainService,
                 clock: Clock | None = None) -> None:
        install(database)
        self.database = database
        self.foundation = foundation
        self.clock = clock or foundation.clock

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _write(self, connection, *, request_id: str, action: str,
               payload: dict[str, Any], create) -> tuple[WriteReceipt, dict[str, Any]]:
        receipt = self.foundation._idempotent(connection, request_id=request_id,
                                              action=action, payload=payload, create=create)
        row = connection.execute(
            "SELECT response_json FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        return receipt, json.loads(row["response_json"])

    def _parcel(self, connection, parcel_id: str):
        row = connection.execute(
            "SELECT * FROM postal_parcels WHERE parcel_id=?", (parcel_id,)).fetchone()
        if row is None:
            raise NotFoundError("包裹不存在")
        return row

    def _declaration(self, connection, parcel_id: str, version: int | None = None) -> dict[str, Any]:
        if version is None:
            row = connection.execute(
                "SELECT * FROM postal_declarations WHERE parcel_id=? ORDER BY version DESC LIMIT 1",
                (parcel_id,)).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM postal_declarations WHERE parcel_id=? AND version=?",
                (parcel_id, version)).fetchone()
        if row is None:
            raise NotFoundError("申报版本不存在")
        return {"version": row["version"],
                "declared_value_minor": row["declared_value_minor"],
                "currency": row["currency"],
                "item_category": row["item_category"],
                "proofs": json.loads(row["proofs_json"])}

    def _gateway_map(self, connection) -> dict[str, dict[str, Any]]:
        rows = connection.execute("SELECT * FROM postal_gateways").fetchall()
        return {row["gateway_id"]: dict(row) for row in rows}

    def _leg_rows(self, connection) -> list[dict[str, Any]]:
        rows = connection.execute("SELECT * FROM postal_legs").fetchall()
        return [dict(row) for row in rows]

    def _remaining_capacity(self, connection) -> dict[str, int]:
        remaining: dict[str, int] = {}
        for row in connection.execute("SELECT leg_id, capacity FROM postal_legs"):
            remaining[row["leg_id"]] = row["capacity"]
        for row in connection.execute(
                "SELECT leg_id, SUM(units) AS used FROM postal_reservations "
                "WHERE status IN ('held','consumed') GROUP BY leg_id"):
            remaining[row["leg_id"]] = remaining.get(row["leg_id"], 0) - row["used"]
        return remaining

    def _active_rule_dicts(self, connection) -> list[dict[str, Any]]:
        rows = connection.execute("SELECT * FROM postal_rules WHERE status='active'").fetchall()
        return active_rules(rows)

    def _lineage(self, connection, *, parcel_id: str, container_id: str | None,
                 action: str, gateway_id: str | None, leg_id: str | None,
                 actor_id: str, detail: dict[str, Any]) -> int:
        connection.execute(
            "INSERT INTO postal_lineage(event_id,parcel_id,container_id,action,gateway_id,leg_id,"
            "actor_id,detail_json,occurred_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, parcel_id, container_id, action, gateway_id, leg_id,
             actor_id, canonical_json(detail), self._now()))
        return connection.execute("SELECT last_insert_rowid() AS seq").fetchone()["seq"]

    def _open_responsibility(self, connection, *, responsibility_id: str, parcel_id: str,
                             party_type: str, party_id: str, reason: str) -> bool:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO postal_responsibilities(responsibility_id,parcel_id,party_type,"
            "party_id,reason,status,opened_at) VALUES(?,?,?,?,?,'open',?)",
            (responsibility_id, parcel_id, party_type, party_id, reason, self._now()))
        return cursor.rowcount > 0

    def _resolve_responsibilities(self, connection, parcel_id: str, prefix: str | None = None) -> None:
        if prefix is None:
            connection.execute(
                "UPDATE postal_responsibilities SET status='resolved', resolved_at=? "
                "WHERE parcel_id=? AND status='open'", (self._now(), parcel_id))
        else:
            connection.execute(
                "UPDATE postal_responsibilities SET status='resolved', resolved_at=? "
                "WHERE parcel_id=? AND status='open' AND responsibility_id LIKE ?",
                (self._now(), parcel_id, f"{prefix}%"))

    def _container_of(self, connection, parcel_id: str):
        return connection.execute(
            "SELECT c.* FROM postal_containers c JOIN postal_container_items i "
            "ON c.container_id=i.container_id WHERE i.parcel_id=?", (parcel_id,)).fetchone()

    def _active_plan(self, connection, parcel_id: str):
        return connection.execute(
            "SELECT * FROM postal_plans WHERE parcel_id=? AND status='active'", (parcel_id,)).fetchone()

    def _next_leg(self, connection, parcel_id: str) -> str | None:
        plan = self._active_plan(connection, parcel_id)
        if plan is None:
            return None
        held = {row["leg_id"] for row in connection.execute(
            "SELECT leg_id FROM postal_reservations WHERE plan_id=? AND status='held'",
            (plan["plan_id"],))}
        for leg_id in json.loads(plan["legs_json"]):
            if leg_id in held:
                return leg_id
        return None

    def _origin_gateways(self, connection, parcel) -> list[str]:
        rows = connection.execute(
            "SELECT gateway_id FROM postal_gateways WHERE region=? AND status!='closed' "
            "ORDER BY gateway_id", (parcel["origin_region"],)).fetchall()
        return [row["gateway_id"] for row in rows]

    def _validate_declaration(self, declared_value_minor: int, currency: str,
                              item_category: str, proofs: Any) -> list[dict[str, Any]]:
        if not isinstance(declared_value_minor, int) or declared_value_minor < 0:
            raise ValidationError("declared_value_minor 必须是非负整数")
        currency = self.foundation._text(currency, "currency", 8)
        item_category = self.foundation._text(item_category, "item_category", 60)
        if proofs is None:
            proofs = []
        if not isinstance(proofs, list):
            raise ValidationError("proofs 必须是数组")
        normalized = []
        for proof in proofs:
            if not isinstance(proof, dict) or not proof.get("type"):
                raise ValidationError("每份证明必须包含 type")
            normalized.append({"type": str(proof["type"]).strip(),
                               "reference": str(proof.get("reference", "")).strip()})
        return normalized

    # ------------------------------------------------------------------
    # 方案生成与改道
    # ------------------------------------------------------------------
    def _create_plan(self, connection, *, parcel, declaration: dict[str, Any],
                     request_id: str, actor_id: str, cause: str,
                     origin_gateway_ids: list[str],
                     excluded_gateways: set[str] | None = None,
                     excluded_legs: set[str] | None = None) -> tuple[str | None, list[dict[str, Any]]]:
        gateways = self._gateway_map(connection)
        legs = self._leg_rows(connection)
        remaining = self._remaining_capacity(connection)
        rules = self._active_rule_dicts(connection)
        parcel_fact = {"origin_region": parcel["origin_region"],
                       "destination_country": parcel["destination_country"]}
        paths = find_paths(legs, gateways, list(origin_gateway_ids),
                           parcel["destination_country"],
                           excluded_gateways=excluded_gateways,
                           excluded_legs=excluded_legs, remaining=remaining)
        viable: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        rejections: list[dict[str, Any]] = []
        for path in paths:
            ok, reasons = evaluate_path(rules, parcel_fact, declaration, gateways, path["gateway_ids"])
            if ok:
                viable.append((path, reasons))
            else:
                for reason in reasons:
                    if reason["code"] == "rule_blocked":
                        rejections.append({**reason, "legs": [leg["leg_id"] for leg in path["legs"]]})
        if not viable:
            return None, rejections or [{"code": "no_path", "message": "没有可用的口岸或运力"}]

        primary, primary_reasons = viable[0]
        version = connection.execute(
            "SELECT COUNT(*) AS count FROM postal_plans WHERE parcel_id=?",
            (parcel["parcel_id"],)).fetchone()["count"] + 1
        plan_id = f"plan-{parcel['parcel_id']}-v{version}"
        leg_ids = [leg["leg_id"] for leg in primary["legs"]]
        reasons = [{"code": "cause", "message": cause}]
        for gateway_id in primary["gateway_ids"]:
            gateway = gateways[gateway_id]
            if gateway["status"] == "restricted":
                reasons.append({"code": "gateway_restricted", "gateway_id": gateway_id,
                                "message": f"口岸 {gateway_id} 受限，时效可能波动"})
        reasons.extend(primary_reasons)
        for leg in primary["legs"]:
            reasons.append({"code": "capacity_reserved", "leg_id": leg["leg_id"],
                            "carrier_id": leg["carrier_id"],
                            "message": f"区段 {leg['leg_id']} 已为包裹原子预留 1 件运力"})
        for rank, (backup, _) in enumerate(viable[1:4], start=1):
            reasons.append({"code": "backup_route", "rank": rank,
                            "legs": [leg["leg_id"] for leg in backup["legs"]],
                            "message": f"备用路线 {rank} 按稳定优先级排序"})
        connection.execute(
            "INSERT INTO postal_plans(plan_id,parcel_id,version,origin_gateway,legs_json,"
            "reasons_json,status,created_at) VALUES(?,?,?,?,?,?, 'active', ?)",
            (plan_id, parcel["parcel_id"], version, primary["gateway_ids"][0],
             canonical_json(leg_ids), canonical_json(reasons), self._now()))
        for rank, (backup, backup_reasons) in enumerate(viable[1:4], start=1):
            connection.execute(
                "INSERT INTO postal_plan_alternatives(plan_id,rank,legs_json,reasons_json) VALUES(?,?,?,?)",
                (plan_id, rank, canonical_json([leg["leg_id"] for leg in backup["legs"]]),
                 canonical_json(backup_reasons)))
        for leg_id in leg_ids:
            if remaining.get(leg_id, 0) < 1:
                raise ConflictError(f"区段 {leg_id} 运力不足，无法完成原子预留")
            connection.execute(
                "INSERT INTO postal_reservations(reservation_id,request_id,plan_id,leg_id,units,"
                "status,created_at) VALUES(?,?,?,?,1,'held',?)",
                (f"rsv-{plan_id}-{leg_id}", f"{request_id}#{leg_id}", plan_id, leg_id, self._now()))
            remaining[leg_id] -= 1
        return plan_id, reasons

    def _release_held_reservations(self, connection, plan_id: str) -> None:
        connection.execute(
            "UPDATE postal_reservations SET status='released' WHERE plan_id=? AND status='held'",
            (plan_id,))

    def _hold_parcel(self, connection, *, parcel, actor_id: str, cause: str,
                     reasons: list[dict[str, Any]]) -> None:
        connection.execute("UPDATE postal_parcels SET status='held' WHERE parcel_id=?",
                           (parcel["parcel_id"],))
        self._lineage(connection, parcel_id=parcel["parcel_id"], container_id=None,
                      action="held", gateway_id=parcel["current_gateway"], leg_id=None,
                      actor_id=actor_id, detail={"cause": cause, "reasons": reasons})
        party_id = parcel["current_gateway"]
        if not party_id:
            origins = self._origin_gateways(connection, parcel)
            party_id = origins[0] if origins else parcel["origin_region"]
        self._open_responsibility(
            connection, responsibility_id=f"hold-{parcel['parcel_id']}-{party_id}",
            parcel_id=parcel["parcel_id"], party_type="node", party_id=party_id, reason=cause)

    def _replan_parcel(self, connection, *, parcel_id: str, actor_id: str, cause: str,
                       request_id: str, excluded_gateways: set[str] | None = None,
                       excluded_legs: set[str] | None = None) -> dict[str, Any]:
        parcel = self._parcel(connection, parcel_id)
        if parcel["status"] == FINAL_PARCEL_STATUS:
            return {"parcel_id": parcel_id, "result": "completed_untouched"}
        if parcel["status"] == "inspection":
            return {"parcel_id": parcel_id, "result": "skipped", "reason": "查验中的包裹不改道"}
        container = self._container_of(connection, parcel_id)
        if container is not None:
            if container["state"] == "in_transit":
                return {"parcel_id": parcel_id, "result": "skipped", "reason": "运输途中不可改道"}
            connection.execute(
                "DELETE FROM postal_container_items WHERE container_id=? AND parcel_id=?",
                (container["container_id"], parcel_id))
            self._lineage(connection, parcel_id=parcel_id, container_id=container["container_id"],
                          action="deconsolidated", gateway_id=container["current_gateway"],
                          leg_id=None, actor_id=actor_id, detail={"cause": cause})
        plan = self._active_plan(connection, parcel_id)
        old_plan_id = plan["plan_id"] if plan else None
        if plan is not None:
            connection.execute("UPDATE postal_plans SET status='superseded' WHERE plan_id=?",
                               (old_plan_id,))
            self._release_held_reservations(connection, old_plan_id)
        declaration = self._declaration(connection, parcel_id)
        origins = [parcel["current_gateway"]] if parcel["current_gateway"] \
            else self._origin_gateways(connection, parcel)
        plan_id, reasons = self._create_plan(
            connection, parcel=parcel, declaration=declaration, request_id=request_id,
            actor_id=actor_id, cause=cause, origin_gateway_ids=origins,
            excluded_gateways=excluded_gateways, excluded_legs=excluded_legs)
        if plan_id is None:
            self._hold_parcel(connection, parcel=parcel, actor_id=actor_id,
                              cause=cause, reasons=reasons)
            return {"parcel_id": parcel_id, "result": "held", "reasons": reasons}
        origin_gateway = connection.execute(
            "SELECT origin_gateway FROM postal_plans WHERE plan_id=?",
            (plan_id,)).fetchone()["origin_gateway"]
        connection.execute(
            "UPDATE postal_parcels SET status='planned', current_gateway=? WHERE parcel_id=?",
            (origin_gateway, parcel_id))
        self._lineage(connection, parcel_id=parcel_id, container_id=None, action="rerouted",
                      gateway_id=parcel["current_gateway"], leg_id=None, actor_id=actor_id,
                      detail={"cause": cause, "from_plan": old_plan_id, "to_plan": plan_id})
        return {"parcel_id": parcel_id, "result": "rerouted", "plan_id": plan_id}

    def _cascade(self, connection, *, actor_id: str, cause: str, request_id: str,
                 excluded_gateways: set[str] | None = None,
                 excluded_legs: set[str] | None = None,
                 only_legs: set[str] | None = None) -> list[dict[str, Any]]:
        """让规则、口岸或区段的变化只命中尚未完成且剩余路径确实受影响的对象。"""

        clauses = ["p.status='active'", "r.status='held'"]
        parameters: list[Any] = []
        if excluded_gateways:
            marks = ",".join("?" for _ in excluded_gateways)
            clauses.append(f"(l.from_gateway IN ({marks}) OR l.to_gateway IN ({marks}))")
            parameters.extend(sorted(excluded_gateways))
            parameters.extend(sorted(excluded_gateways))
        if only_legs:
            marks = ",".join("?" for _ in only_legs)
            clauses.append(f"r.leg_id IN ({marks})")
            parameters.extend(sorted(only_legs))
        rows = connection.execute(
            "SELECT DISTINCT pa.parcel_id FROM postal_parcels pa "
            "JOIN postal_plans p ON p.parcel_id=pa.parcel_id "
            "JOIN postal_reservations r ON r.plan_id=p.plan_id "
            "JOIN postal_legs l ON l.leg_id=r.leg_id "
            f"WHERE pa.status!='delivered' AND {' AND '.join(clauses)} ORDER BY pa.parcel_id",
            parameters).fetchall()
        results = []
        for index, row in enumerate(rows):
            results.append(self._replan_parcel(
                connection, parcel_id=row["parcel_id"], actor_id=actor_id, cause=cause,
                request_id=f"{request_id}-c{index}",
                excluded_gateways=excluded_gateways, excluded_legs=excluded_legs))
        return results

    # ------------------------------------------------------------------
    # 主数据登记
    # ------------------------------------------------------------------
    def register_commitment(self, *, request_id: str, actor_id: str, commitment_id: str,
                            description: str, promised_hours: int) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "description": description, "promised_hours": promised_hours}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *OPERATOR_ROLES)
            commitment_id = self.foundation._identifier(commitment_id, "commitment_id")
            description = self.foundation._text(description, "description")
            if not isinstance(promised_hours, int) or promised_hours <= 0:
                raise ValidationError("promised_hours 必须是正整数")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO postal_commitments(commitment_id,description,promised_hours,"
                        "created_at) VALUES(?,?,?,?)",
                        (commitment_id, description, promised_hours, self._now()))
                except Exception as exc:
                    raise ConflictError("服务承诺编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="postal.commitment.registered",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"promised_hours": promised_hours}, occurred_at=self._now())
                return "commitment", commitment_id, {"commitment_id": commitment_id}

            return self._write(connection, request_id=request_id,
                               action="register_commitment", payload=payload, create=create)

    def register_gateway(self, *, request_id: str, actor_id: str, gateway_id: str, name: str,
                         country: str, region: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "gateway_id": gateway_id, "name": name,
                   "country": country, "region": region}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *OPERATOR_ROLES)
            gateway_id = self.foundation._identifier(gateway_id, "gateway_id")
            name = self.foundation._text(name, "name")
            country = self.foundation._text(country, "country", 60)
            region = self.foundation._text(region, "region", 60)

            def create():
                try:
                    connection.execute(
                        "INSERT INTO postal_gateways(gateway_id,name,country,region,status,"
                        "updated_at) VALUES(?,?,?,?,'open',?)",
                        (gateway_id, name, country, region, self._now()))
                except Exception as exc:
                    raise ConflictError("口岸编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="postal.gateway.registered",
                             resource_type="gateway", resource_id=gateway_id,
                             detail={"country": country, "region": region}, occurred_at=self._now())
                return "gateway", gateway_id, {"gateway_id": gateway_id}

            return self._write(connection, request_id=request_id,
                               action="register_gateway", payload=payload, create=create)

    def set_gateway_status(self, *, request_id: str, actor_id: str, gateway_id: str,
                           status: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "gateway_id": gateway_id, "status": status}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *OPERATOR_ROLES)
            if status not in ("open", "restricted", "closed"):
                raise ValidationError("status 必须是 open、restricted 或 closed")
            gateway = connection.execute(
                "SELECT * FROM postal_gateways WHERE gateway_id=?", (gateway_id,)).fetchone()
            if gateway is None:
                raise NotFoundError("口岸不存在")

            def create():
                connection.execute("UPDATE postal_gateways SET status=?, updated_at=? "
                                   "WHERE gateway_id=?", (status, self._now(), gateway_id))
                affected: list[dict[str, Any]] = []
                if status == "closed":
                    affected = self._cascade(connection, actor_id=actor_id,
                                             cause=f"gateway_closed:{gateway_id}",
                                             request_id=request_id,
                                             excluded_gateways={gateway_id})
                append_event(connection, actor_id=actor_id, action="postal.gateway.status_changed",
                             resource_type="gateway", resource_id=gateway_id,
                             detail={"status": status, "affected": affected},
                             occurred_at=self._now())
                return "gateway", gateway_id, {"gateway_id": gateway_id, "status": status,
                                               "affected": affected}

            return self._write(connection, request_id=request_id,
                               action="set_gateway_status", payload=payload, create=create)

    def register_leg(self, *, request_id: str, actor_id: str, leg_id: str, from_gateway: str,
                     to_gateway: str, carrier_id: str, capacity: int,
                     priority: int) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "leg_id": leg_id, "from_gateway": from_gateway,
                   "to_gateway": to_gateway, "carrier_id": carrier_id,
                   "capacity": capacity, "priority": priority}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *OPERATOR_ROLES)
            leg_id = self.foundation._identifier(leg_id, "leg_id")
            carrier_id = self.foundation._identifier(carrier_id, "carrier_id")
            if not isinstance(capacity, int) or capacity < 0:
                raise ValidationError("capacity 必须是非负整数")
            if not isinstance(priority, int):
                raise ValidationError("priority 必须是整数")
            gateways = self._gateway_map(connection)
            if from_gateway not in gateways or to_gateway not in gateways:
                raise NotFoundError("区段端点口岸不存在")
            if from_gateway == to_gateway:
                raise ValidationError("区段两端不能是同一口岸")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO postal_legs(leg_id,from_gateway,to_gateway,carrier_id,"
                        "capacity,priority,status,updated_at) VALUES(?,?,?,?,?,?,'active',?)",
                        (leg_id, from_gateway, to_gateway, carrier_id, capacity, priority,
                         self._now()))
                except Exception as exc:
                    raise ConflictError("区段编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="postal.leg.registered",
                             resource_type="leg", resource_id=leg_id,
                             detail={"from_gateway": from_gateway, "to_gateway": to_gateway,
                                     "carrier_id": carrier_id, "capacity": capacity,
                                     "priority": priority}, occurred_at=self._now())
                return "leg", leg_id, {"leg_id": leg_id}

            return self._write(connection, request_id=request_id,
                               action="register_leg", payload=payload, create=create)

    def set_leg_status(self, *, request_id: str, actor_id: str, leg_id: str, status: str,
                       capacity: int | None = None) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "leg_id": leg_id, "status": status, "capacity": capacity}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *OPERATOR_ROLES)
            if status not in ("active", "reduced", "suspended"):
                raise ValidationError("status 必须是 active、reduced 或 suspended")
            leg = connection.execute("SELECT * FROM postal_legs WHERE leg_id=?",
                                   (leg_id,)).fetchone()
            if leg is None:
                raise NotFoundError("区段不存在")
            new_capacity = leg["capacity"] if capacity is None else capacity
            if not isinstance(new_capacity, int) or new_capacity < 0:
                raise ValidationError("capacity 必须是非负整数")

            def create():
                connection.execute(
                    "UPDATE postal_legs SET status=?, capacity=?, updated_at=? WHERE leg_id=?",
                    (status, new_capacity, self._now(), leg_id))
                affected: list[dict[str, Any]] = []
                if status == "suspended":
                    affected = self._cascade(connection, actor_id=actor_id,
                                             cause=f"leg_suspended:{leg_id}",
                                             request_id=request_id,
                                             excluded_legs={leg_id}, only_legs={leg_id})
                else:
                    used = connection.execute(
                        "SELECT COALESCE(SUM(units),0) AS used FROM postal_reservations "
                        "WHERE leg_id=? AND status IN ('held','consumed')", (leg_id,)).fetchone()["used"]
                    excess = used - new_capacity
                    if excess > 0:
                        rows = connection.execute(
                            "SELECT reservation_id, plan_id FROM postal_reservations "
                            "WHERE leg_id=? AND status='held' ORDER BY created_at DESC, "
                            "reservation_id DESC", (leg_id,)).fetchall()
                        bumped_plan_ids = []
                        for row in rows[:excess]:
                            connection.execute(
                                "UPDATE postal_reservations SET status='released' "
                                "WHERE reservation_id=?", (row["reservation_id"],))
                            bumped_plan_ids.append(row["plan_id"])
                        parcel_rows = connection.execute(
                            "SELECT DISTINCT parcel_id FROM postal_plans "
                            f"WHERE plan_id IN ({','.join('?' for _ in bumped_plan_ids)}) "
                            "ORDER BY parcel_id", bumped_plan_ids).fetchall() if bumped_plan_ids else []
                        for index, parcel_row in enumerate(parcel_rows):
                            affected.append(self._replan_parcel(
                                connection, parcel_id=parcel_row["parcel_id"], actor_id=actor_id,
                                cause=f"leg_reduced:{leg_id}",
                                request_id=f"{request_id}-b{index}"))
                append_event(connection, actor_id=actor_id, action="postal.leg.status_changed",
                             resource_type="leg", resource_id=leg_id,
                             detail={"status": status, "capacity": new_capacity,
                                     "affected": affected}, occurred_at=self._now())
                return "leg", leg_id, {"leg_id": leg_id, "status": status,
                                       "capacity": new_capacity, "affected": affected}

            return self._write(connection, request_id=request_id,
                               action="set_leg_status", payload=payload, create=create)

    def register_container(self, *, request_id: str, actor_id: str, container_id: str,
                           capacity: int, gateway_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "container_id": container_id,
                   "capacity": capacity, "gateway_id": gateway_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *NODE_ROLES)
            container_id = self.foundation._identifier(container_id, "container_id")
            if not isinstance(capacity, int) or capacity <= 0:
                raise ValidationError("capacity 必须是正整数")
            if connection.execute("SELECT 1 FROM postal_gateways WHERE gateway_id=?",
                                  (gateway_id,)).fetchone() is None:
                raise NotFoundError("口岸不存在")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO postal_containers(container_id,capacity,state,current_gateway,"
                        "created_at) VALUES(?,?,'open',?,?)",
                        (container_id, capacity, gateway_id, self._now()))
                except Exception as exc:
                    raise ConflictError("容器编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="postal.container.registered",
                             resource_type="container", resource_id=container_id,
                             detail={"capacity": capacity, "gateway_id": gateway_id},
                             occurred_at=self._now())
                return "container", container_id, {"container_id": container_id}

            return self._write(connection, request_id=request_id,
                               action="register_container", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 包裹、申报与规则
    # ------------------------------------------------------------------
    def register_parcel(self, *, request_id: str, actor_id: str, parcel_id: str,
                        origin_region: str, destination_country: str, weight_grams: int,
                        commitment_id: str, declared_value_minor: int, currency: str,
                        item_category: str, proofs: Any = None) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "parcel_id": parcel_id, "origin_region": origin_region,
                   "destination_country": destination_country, "weight_grams": weight_grams,
                   "commitment_id": commitment_id, "declared_value_minor": declared_value_minor,
                   "currency": currency, "item_category": item_category, "proofs": proofs}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *NODE_ROLES)
            parcel_id = self.foundation._identifier(parcel_id, "parcel_id")
            origin_region = self.foundation._text(origin_region, "origin_region", 60)
            destination_country = self.foundation._text(destination_country, "destination_country", 60)
            if not isinstance(weight_grams, int) or weight_grams <= 0:
                raise ValidationError("weight_grams 必须是正整数")
            if connection.execute("SELECT 1 FROM postal_commitments WHERE commitment_id=?",
                                  (commitment_id,)).fetchone() is None:
                raise NotFoundError("服务承诺不存在")
            normalized_proofs = self._validate_declaration(
                declared_value_minor, currency, item_category, proofs)

            def create():
                try:
                    connection.execute(
                        "INSERT INTO postal_parcels(parcel_id,origin_region,destination_country,"
                        "weight_grams,commitment_id,status,current_gateway,declaration_version,"
                        "created_by,created_at) VALUES(?,?,?,?,?,'registered',NULL,1,?,?)",
                        (parcel_id, origin_region, destination_country, weight_grams,
                         commitment_id, actor_id, self._now()))
                    connection.execute(
                        "INSERT INTO postal_declarations(parcel_id,version,declared_value_minor,"
                        "currency,item_category,proofs_json,created_by,created_at) "
                        "VALUES(?,1,?,?,?,?,?,?)",
                        (parcel_id, declared_value_minor, currency, item_category,
                         canonical_json(normalized_proofs), actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("包裹编号已经存在") from exc
                self._lineage(connection, parcel_id=parcel_id, container_id=None,
                              action="registered", gateway_id=None, leg_id=None,
                              actor_id=actor_id,
                              detail={"origin_region": origin_region,
                                      "destination_country": destination_country})
                append_event(connection, actor_id=actor_id, action="postal.parcel.registered",
                             resource_type="parcel", resource_id=parcel_id,
                             detail={"commitment_id": commitment_id}, occurred_at=self._now())
                return "parcel", parcel_id, {"parcel_id": parcel_id}

            return self._write(connection, request_id=request_id,
                               action="register_parcel", payload=payload, create=create)

    def add_declaration_version(self, *, request_id: str, actor_id: str, parcel_id: str,
                                declared_value_minor: int, currency: str, item_category: str,
                                proofs: Any = None) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "parcel_id": parcel_id,
                   "declared_value_minor": declared_value_minor, "currency": currency,
                   "item_category": item_category, "proofs": proofs}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *NODE_ROLES)
            parcel = self._parcel(connection, parcel_id)
            if parcel["status"] == FINAL_PARCEL_STATUS:
                raise ConflictError("已签收的包裹不可改写申报")
            normalized_proofs = self._validate_declaration(
                declared_value_minor, currency, item_category, proofs)

            def create():
                version = parcel["declaration_version"] + 1
                connection.execute(
                    "INSERT INTO postal_declarations(parcel_id,version,declared_value_minor,"
                    "currency,item_category,proofs_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (parcel_id, version, declared_value_minor, currency, item_category,
                     canonical_json(normalized_proofs), actor_id, self._now()))
                connection.execute(
                    "UPDATE postal_parcels SET declaration_version=? WHERE parcel_id=?",
                    (version, parcel_id))
                declaration = {"version": version, "declared_value_minor": declared_value_minor,
                               "currency": currency, "item_category": item_category,
                               "proofs": normalized_proofs}
                outcome = {"parcel_id": parcel_id, "version": version, "result": "accepted"}
                plan = self._active_plan(connection, parcel_id)
                if plan is not None:
                    gateways = self._gateway_map(connection)
                    rules = self._active_rule_dicts(connection)
                    remaining_gateways = self._remaining_gateways(connection, parcel, plan)
                    ok, reasons = evaluate_path(
                        rules, {"origin_region": parcel["origin_region"],
                                "destination_country": parcel["destination_country"]},
                        declaration, gateways, remaining_gateways)
                    if not ok:
                        result = self._replan_parcel(
                            connection, parcel_id=parcel_id, actor_id=actor_id,
                            cause=f"declaration_changed:v{version}",
                            request_id=f"{request_id}-rp")
                        outcome["result"] = result["result"]
                        outcome["reasons"] = reasons
                append_event(connection, actor_id=actor_id,
                             action="postal.declaration.version_added",
                             resource_type="parcel", resource_id=parcel_id,
                             detail={"version": version, "outcome": outcome},
                             occurred_at=self._now())
                return "parcel", parcel_id, outcome

            return self._write(connection, request_id=request_id,
                               action="add_declaration_version", payload=payload, create=create)

    def publish_rule(self, *, request_id: str, actor_id: str, rule_id: str, jurisdiction: str,
                     rule_scope: str, rule_type: str, selector: dict[str, Any],
                     constraint: dict[str, Any]) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "rule_id": rule_id, "jurisdiction": jurisdiction,
                   "rule_scope": rule_scope, "rule_type": rule_type,
                   "selector": selector, "constraint": constraint}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *COMPLIANCE_ROLES)
            rule_id = self.foundation._identifier(rule_id, "rule_id")
            jurisdiction = self.foundation._text(jurisdiction, "jurisdiction", 60)
            if rule_scope not in ("origin", "destination", "transit"):
                raise ValidationError("rule_scope 必须是 origin、destination 或 transit")
            if rule_type not in ("prohibited_category", "value_threshold",
                                 "proof_required", "co_bag_restriction"):
                raise ValidationError("rule_type 不在允许范围内")
            if not isinstance(selector, dict) or not isinstance(constraint, dict):
                raise ValidationError("selector 与 constraint 必须是对象")

            def create():
                row = connection.execute(
                    "SELECT MAX(version) AS version FROM postal_rules WHERE rule_id=?",
                    (rule_id,)).fetchone()
                version = (row["version"] or 0) + 1
                connection.execute(
                    "UPDATE postal_rules SET status='superseded' WHERE rule_id=? AND status='active'",
                    (rule_id,))
                connection.execute(
                    "INSERT INTO postal_rules(rule_id,version,jurisdiction,rule_scope,rule_type,"
                    "selector_json,constraint_json,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'active',?,?)",
                    (rule_id, version, jurisdiction, rule_scope, rule_type,
                     canonical_json(selector), canonical_json(constraint), actor_id, self._now()))
                affected = self._recheck_rule_hits(
                    connection, actor_id=actor_id, request_id=request_id,
                    rule={"rule_id": rule_id, "version": version, "jurisdiction": jurisdiction,
                          "rule_scope": rule_scope, "rule_type": rule_type,
                          "selector": selector, "constraint": constraint})
                append_event(connection, actor_id=actor_id, action="postal.rule.published",
                             resource_type="rule", resource_id=rule_id,
                             detail={"version": version, "jurisdiction": jurisdiction,
                                     "rule_scope": rule_scope, "rule_type": rule_type,
                                     "affected": affected}, occurred_at=self._now())
                return "rule", f"{rule_id}@{version}", {"rule_id": rule_id, "version": version,
                                                        "affected": affected}

            return self._write(connection, request_id=request_id,
                               action="publish_rule", payload=payload, create=create)

    def _remaining_gateways(self, connection, parcel, plan) -> list[str]:
        """计算包裹尚未走过的口岸序列，已完成的交接不参与评估。"""

        consumed = {row["leg_id"] for row in connection.execute(
            "SELECT leg_id FROM postal_reservations WHERE plan_id=? AND status='consumed'",
            (plan["plan_id"],))}
        gateways = [parcel["current_gateway"] or plan["origin_gateway"]]
        for leg_id in json.loads(plan["legs_json"]):
            if leg_id in consumed:
                continue
            leg = connection.execute("SELECT * FROM postal_legs WHERE leg_id=?",
                                     (leg_id,)).fetchone()
            gateways.append(leg["to_gateway"])
        return gateways

    def _recheck_rule_hits(self, connection, *, actor_id: str, request_id: str,
                           rule: dict[str, Any]) -> list[dict[str, Any]]:
        """规则变化只影响尚未完成且当前申报确实命中的包裹。"""

        rows = connection.execute(
            "SELECT * FROM postal_parcels WHERE status NOT IN ('delivered','registered') "
            "ORDER BY parcel_id").fetchall()
        gateways = self._gateway_map(connection)
        rules = self._active_rule_dicts(connection)
        affected = []
        for index, parcel in enumerate(rows):
            declaration = self._declaration(connection, parcel["parcel_id"])
            if not selector_matches(rule["selector"], declaration):
                continue
            plan = self._active_plan(connection, parcel["parcel_id"])
            if plan is None:
                continue
            remaining = self._remaining_gateways(connection, parcel, plan)
            relevant = False
            if rule["rule_scope"] == "destination":
                relevant = rule["jurisdiction"] == parcel["destination_country"]
            elif rule["rule_scope"] == "origin":
                relevant = rule["jurisdiction"] == parcel["origin_region"]
            else:
                transit = {gateways[gid]["country"] for gid in remaining[1:-1]}
                relevant = rule["jurisdiction"] in transit
            if not relevant:
                continue
            ok, _ = evaluate_path(
                rules, {"origin_region": parcel["origin_region"],
                        "destination_country": parcel["destination_country"]},
                declaration, gateways, remaining)
            if ok:
                continue
            result = self._replan_parcel(
                connection, parcel_id=parcel["parcel_id"], actor_id=actor_id,
                cause=f"rule_changed:{rule['rule_id']}@v{rule['version']}",
                request_id=f"{request_id}-r{index}")
            affected.append({"parcel_id": parcel["parcel_id"], "result": result["result"]})
        return affected

    # ------------------------------------------------------------------
    # 路由方案
    # ------------------------------------------------------------------
    def generate_plan(self, *, request_id: str, actor_id: str,
                      parcel_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "parcel_id": parcel_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *OPERATOR_ROLES)
            parcel = self._parcel(connection, parcel_id)

            def create():
                if parcel["status"] == FINAL_PARCEL_STATUS:
                    raise ConflictError("已签收的包裹不可再生成方案")
                if self._container_of(connection, parcel_id) is not None:
                    raise ConflictError("包裹已在容器内，请先拆包")
                if self._active_plan(connection, parcel_id) is not None:
                    raise ConflictError("包裹已有在途方案，请使用改道流程")
                declaration = self._declaration(connection, parcel_id)
                origins = [parcel["current_gateway"]] if parcel["current_gateway"] \
                    else self._origin_gateways(connection, parcel)
                if not origins:
                    self._hold_parcel(connection, parcel=parcel, actor_id=actor_id,
                                      cause="no_origin_gateway",
                                      reasons=[{"code": "no_origin_gateway",
                                                "message": "来源区域没有可用口岸"}])
                    return "plan", parcel_id, {"parcel_id": parcel_id, "result": "held",
                                               "reasons": ["no_origin_gateway"]}
                plan_id, reasons = self._create_plan(
                    connection, parcel=parcel, declaration=declaration, request_id=request_id,
                    actor_id=actor_id, cause="initial_plan", origin_gateway_ids=origins)
                if plan_id is None:
                    self._hold_parcel(connection, parcel=parcel, actor_id=actor_id,
                                      cause="no_viable_route", reasons=reasons)
                    return "plan", parcel_id, {"parcel_id": parcel_id, "result": "held",
                                               "reasons": reasons}
                origin_gateway = connection.execute(
                    "SELECT origin_gateway FROM postal_plans WHERE plan_id=?",
                    (plan_id,)).fetchone()["origin_gateway"]
                connection.execute(
                    "UPDATE postal_parcels SET status='planned', current_gateway=? "
                    "WHERE parcel_id=?", (origin_gateway, parcel_id))
                self._lineage(connection, parcel_id=parcel_id, container_id=None,
                              action="planned", gateway_id=origin_gateway, leg_id=None,
                              actor_id=actor_id, detail={"plan_id": plan_id})
                append_event(connection, actor_id=actor_id, action="postal.plan.generated",
                             resource_type="plan", resource_id=plan_id,
                             detail={"parcel_id": parcel_id, "reasons": reasons},
                             occurred_at=self._now())
                return "plan", plan_id, {"parcel_id": parcel_id, "plan_id": plan_id,
                                         "result": "planned", "reasons": reasons}

            return self._write(connection, request_id=request_id,
                               action="generate_plan", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 合包、封袋、转运与交接
    # ------------------------------------------------------------------
    def consolidate(self, *, request_id: str, actor_id: str, container_id: str,
                    parcel_ids: list[str]) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "container_id": container_id, "parcel_ids": parcel_ids}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *NODE_ROLES)
            container = connection.execute(
                "SELECT * FROM postal_containers WHERE container_id=?", (container_id,)).fetchone()
            if container is None:
                raise NotFoundError("容器不存在")
            if container["state"] != "open":
                raise ConflictError("只有敞口容器可以合包")
            if not isinstance(parcel_ids, list) or not parcel_ids:
                raise ValidationError("parcel_ids 必须是非空数组")
            if len(set(parcel_ids)) != len(parcel_ids):
                raise ValidationError("parcel_ids 存在重复")

            def create():
                rules = self._active_rule_dicts(connection)
                declarations = {}
                next_legs = set()
                for pid in parcel_ids:
                    parcel = self._parcel(connection, pid)
                    if parcel["status"] not in ("planned", "arrived"):
                        raise ConflictError(f"包裹 {pid} 当前状态不可合包")
                    if parcel["current_gateway"] != container["current_gateway"]:
                        raise ConflictError(f"包裹 {pid} 不在容器所在口岸")
                    if self._container_of(connection, pid) is not None:
                        raise ConflictError(f"包裹 {pid} 已在其他容器内")
                    declaration = self._declaration(connection, pid)
                    declarations[pid] = declaration
                    for rule in rules:
                        if rule["rule_type"] != "value_threshold":
                            continue
                        if rule["jurisdiction"] != parcel["destination_country"]:
                            continue
                        if selector_matches(rule["selector"], declaration) and \
                                rule["constraint"].get("dedicated_container"):
                            raise ConflictError(f"包裹 {pid} 按规则必须使用独立容器")
                    next_leg = self._next_leg(connection, pid)
                    if next_leg is None:
                        raise ConflictError(f"包裹 {pid} 没有待执行的区段")
                    next_legs.add(next_leg)
                if len(next_legs) != 1:
                    raise ConflictError("同一容器的包裹必须共用下一段区段")
                for left in parcel_ids:
                    for right in parcel_ids:
                        if left >= right:
                            continue
                        for rule in rules:
                            if rule["rule_type"] != "co_bag_restriction":
                                continue
                            if rule["jurisdiction"] not in ("*",) and \
                                    rule["jurisdiction"] != self._parcel(
                                        connection, left)["destination_country"]:
                                continue
                            if selector_matches(rule["selector"], declarations[left]) and \
                                    declarations[right]["item_category"] in \
                                    rule["constraint"].get("incompatible_categories", []):
                                raise ConflictError(
                                    f"包裹 {left} 与 {right} 的品类按规则不可同袋")
                            if selector_matches(rule["selector"], declarations[right]) and \
                                    declarations[left]["item_category"] in \
                                    rule["constraint"].get("incompatible_categories", []):
                                raise ConflictError(
                                    f"包裹 {right} 与 {left} 的品类按规则不可同袋")
                used = connection.execute(
                    "SELECT COUNT(*) AS count FROM postal_container_items WHERE container_id=?",
                    (container_id,)).fetchone()["count"]
                if used + len(parcel_ids) > container["capacity"]:
                    raise ConflictError("容器容量不足，合包被拒绝")
                for pid in parcel_ids:
                    connection.execute(
                        "INSERT INTO postal_container_items(container_id,parcel_id,added_at) "
                        "VALUES(?,?,?)", (container_id, pid, self._now()))
                    connection.execute(
                        "UPDATE postal_parcels SET status='consolidated' WHERE parcel_id=?", (pid,))
                    self._lineage(connection, parcel_id=pid, container_id=container_id,
                                  action="consolidated", gateway_id=container["current_gateway"],
                                  leg_id=None, actor_id=actor_id,
                                  detail={"shared_leg": next(iter(next_legs))})
                append_event(connection, actor_id=actor_id, action="postal.container.consolidated",
                             resource_type="container", resource_id=container_id,
                             detail={"parcel_ids": parcel_ids}, occurred_at=self._now())
                return "container", container_id, {"container_id": container_id,
                                                   "parcel_ids": parcel_ids}

            return self._write(connection, request_id=request_id,
                               action="consolidate", payload=payload, create=create)

    def seal_container(self, *, request_id: str, actor_id: str,
                       container_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "container_id": container_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *NODE_ROLES)
            container = connection.execute(
                "SELECT * FROM postal_containers WHERE container_id=?", (container_id,)).fetchone()
            if container is None:
                raise NotFoundError("容器不存在")

            def create():
                if container["state"] != "open":
                    raise ConflictError("只有敞口容器可以封袋")
                items = connection.execute(
                    "SELECT parcel_id FROM postal_container_items WHERE container_id=? "
                    "ORDER BY parcel_id", (container_id,)).fetchall()
                if not items:
                    raise ConflictError("空容器不可封袋")
                connection.execute(
                    "UPDATE postal_containers SET state='sealed' WHERE container_id=?",
                    (container_id,))
                for item in items:
                    self._lineage(connection, parcel_id=item["parcel_id"],
                                  container_id=container_id, action="sealed",
                                  gateway_id=container["current_gateway"], leg_id=None,
                                  actor_id=actor_id, detail={})
                append_event(connection, actor_id=actor_id, action="postal.container.sealed",
                             resource_type="container", resource_id=container_id,
                             detail={"parcels": len(items)}, occurred_at=self._now())
                return "container", container_id, {"container_id": container_id,
                                                   "sealed_parcels": len(items)}

            return self._write(connection, request_id=request_id,
                               action="seal_container", payload=payload, create=create)

    def dispatch_container(self, *, request_id: str, actor_id: str, container_id: str,
                           leg_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "container_id": container_id, "leg_id": leg_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *NODE_ROLES)
            container = connection.execute(
                "SELECT * FROM postal_containers WHERE container_id=?", (container_id,)).fetchone()
            if container is None:
                raise NotFoundError("容器不存在")
            leg = connection.execute("SELECT * FROM postal_legs WHERE leg_id=?",
                                     (leg_id,)).fetchone()
            if leg is None:
                raise NotFoundError("区段不存在")

            def create():
                if container["state"] != "sealed":
                    raise ConflictError("只有已封袋的容器可以发运")
                if leg["status"] == "suspended":
                    raise ConflictError("区段已停运")
                if container["current_gateway"] != leg["from_gateway"]:
                    raise ConflictError("容器不在区段起点口岸")
                items = connection.execute(
                    "SELECT parcel_id FROM postal_container_items WHERE container_id=? "
                    "ORDER BY parcel_id", (container_id,)).fetchall()
                last_seq = connection.execute(
                    "SELECT COALESCE(MAX(sequence),0) AS seq FROM postal_lineage"
                ).fetchone()["seq"]
                handover_id = f"hh-{container_id}-{leg_id}-{last_seq + len(items)}"
                for item in items:
                    pid = item["parcel_id"]
                    next_leg = self._next_leg(connection, pid)
                    if next_leg != leg_id:
                        raise ConflictError(f"包裹 {pid} 的待执行区段不是 {leg_id}")
                    connection.execute(
                        "UPDATE postal_reservations SET status='consumed' WHERE reservation_id IN "
                        "(SELECT r.reservation_id FROM postal_reservations r JOIN postal_plans p "
                        "ON r.plan_id=p.plan_id WHERE p.parcel_id=? AND p.status='active' "
                        "AND r.leg_id=? AND r.status='held')", (pid, leg_id))
                    connection.execute(
                        "UPDATE postal_parcels SET status='in_transit' WHERE parcel_id=?", (pid,))
                    self._lineage(
                        connection, parcel_id=pid, container_id=container_id, action="departed",
                        gateway_id=leg["from_gateway"], leg_id=leg_id, actor_id=actor_id,
                        detail={"carrier_id": leg["carrier_id"], "handover_id": handover_id})
                connection.execute(
                    "UPDATE postal_containers SET state='in_transit', current_gateway=NULL "
                    "WHERE container_id=?", (container_id,))
                party = PARTY_ROLES.get(actor.role, "node")
                connection.execute(
                    "INSERT INTO postal_confirmations(resource_type,resource_id,party,actor_id,"
                    "decision,note,created_at) VALUES('handover',?,?,?,'confirmed','发运交接',?)",
                    (handover_id, party, actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="postal.container.dispatched",
                             resource_type="container", resource_id=container_id,
                             detail={"leg_id": leg_id, "handover_id": handover_id,
                                     "parcels": len(items)}, occurred_at=self._now())
                return "container", container_id, {"container_id": container_id,
                                                   "leg_id": leg_id,
                                                   "handover_id": handover_id}

            return self._write(connection, request_id=request_id,
                               action="dispatch_container", payload=payload, create=create)

    def receive_container(self, *, request_id: str, actor_id: str,
                          container_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "container_id": container_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *CARRIER_ROLES, *NODE_ROLES)
            container = connection.execute(
                "SELECT * FROM postal_containers WHERE container_id=?", (container_id,)).fetchone()
            if container is None:
                raise NotFoundError("容器不存在")

            def create():
                if container["state"] != "in_transit":
                    raise ConflictError("只有在途容器可以签收交接")
                departed = connection.execute(
                    "SELECT * FROM postal_lineage WHERE container_id=? AND action='departed' "
                    "ORDER BY sequence DESC LIMIT 1", (container_id,)).fetchone()
                if departed is None:
                    raise ConflictError("缺少发运记录，无法交接")
                leg = connection.execute("SELECT * FROM postal_legs WHERE leg_id=?",
                                         (departed["leg_id"],)).fetchone()
                gateway_id = leg["to_gateway"]
                handover_id = f"hh-{container_id}-{leg['leg_id']}-{departed['sequence']}"
                items = connection.execute(
                    "SELECT parcel_id FROM postal_container_items WHERE container_id=? "
                    "ORDER BY parcel_id", (container_id,)).fetchall()
                for item in items:
                    connection.execute(
                        "UPDATE postal_parcels SET status='arrived', current_gateway=? "
                        "WHERE parcel_id=?", (gateway_id, item["parcel_id"]))
                    self._lineage(connection, parcel_id=item["parcel_id"],
                                  container_id=container_id, action="arrived",
                                  gateway_id=gateway_id, leg_id=leg["leg_id"],
                                  actor_id=actor_id, detail={})
                connection.execute(
                    "UPDATE postal_containers SET state='arrived', current_gateway=? "
                    "WHERE container_id=?", (gateway_id, container_id))
                party = PARTY_ROLES.get(actor.role, "carrier")
                connection.execute(
                    "INSERT OR IGNORE INTO postal_confirmations(resource_type,resource_id,party,"
                    "actor_id,decision,note,created_at) VALUES('handover',?,?,?,'confirmed',"
                    "'到达交接',?)", (handover_id, party, actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="postal.container.received",
                             resource_type="container", resource_id=container_id,
                             detail={"gateway_id": gateway_id, "handover_id": handover_id,
                                     "parcels": len(items)}, occurred_at=self._now())
                return "container", container_id, {"container_id": container_id,
                                                   "gateway_id": gateway_id,
                                                   "handover_id": handover_id}

            return self._write(connection, request_id=request_id,
                               action="receive_container", payload=payload, create=create)

    def deconsolidate(self, *, request_id: str, actor_id: str, container_id: str,
                      parcel_ids: list[str]) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "container_id": container_id, "parcel_ids": parcel_ids}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *NODE_ROLES)
            container = connection.execute(
                "SELECT * FROM postal_containers WHERE container_id=?", (container_id,)).fetchone()
            if container is None:
                raise NotFoundError("容器不存在")
            if not isinstance(parcel_ids, list) or not parcel_ids:
                raise ValidationError("parcel_ids 必须是非空数组")

            def create():
                if container["state"] not in ("arrived", "open"):
                    raise ConflictError("只有到达或敞口的容器可以拆包")
                gateways = self._gateway_map(connection)
                removed = []
                for pid in parcel_ids:
                    item = connection.execute(
                        "SELECT * FROM postal_container_items WHERE container_id=? AND parcel_id=?",
                        (container_id, pid)).fetchone()
                    if item is None:
                        raise NotFoundError(f"包裹 {pid} 不在容器内")
                    parcel = self._parcel(connection, pid)
                    connection.execute(
                        "DELETE FROM postal_container_items WHERE container_id=? AND parcel_id=?",
                        (container_id, pid))
                    at_destination = gateways[parcel["current_gateway"]]["country"] == \
                        parcel["destination_country"]
                    connection.execute(
                        "UPDATE postal_parcels SET status=? WHERE parcel_id=?",
                        ("arrived" if at_destination else "planned", pid))
                    self._lineage(connection, parcel_id=pid, container_id=container_id,
                                  action="deconsolidated",
                                  gateway_id=container["current_gateway"], leg_id=None,
                                  actor_id=actor_id, detail={})
                    removed.append(pid)
                remaining = connection.execute(
                    "SELECT COUNT(*) AS count FROM postal_container_items WHERE container_id=?",
                    (container_id,)).fetchone()["count"]
                if remaining == 0 and container["state"] == "arrived":
                    connection.execute(
                        "UPDATE postal_containers SET state='open' WHERE container_id=?",
                        (container_id,))
                append_event(connection, actor_id=actor_id, action="postal.container.deconsolidated",
                             resource_type="container", resource_id=container_id,
                             detail={"parcel_ids": removed}, occurred_at=self._now())
                return "container", container_id, {"container_id": container_id,
                                                   "removed": removed}

            return self._write(connection, request_id=request_id,
                               action="deconsolidate", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查验与责任
    # ------------------------------------------------------------------
    def start_inspection(self, *, request_id: str, actor_id: str, container_id: str,
                         reason: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "container_id": container_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *COMPLIANCE_ROLES)
            container = connection.execute(
                "SELECT * FROM postal_containers WHERE container_id=?", (container_id,)).fetchone()
            if container is None:
                raise NotFoundError("容器不存在")
            reason = self.foundation._text(reason, "reason")

            def create():
                if container["state"] not in ("sealed", "arrived"):
                    raise ConflictError("只有在口岸的已封袋或到达容器可以查验")
                gateway_id = container["current_gateway"]
                connection.execute(
                    "UPDATE postal_containers SET state='inspection' WHERE container_id=?",
                    (container_id,))
                items = connection.execute(
                    "SELECT parcel_id FROM postal_container_items WHERE container_id=? "
                    "ORDER BY parcel_id", (container_id,)).fetchall()
                held = []
                for item in items:
                    pid = item["parcel_id"]
                    connection.execute(
                        "UPDATE postal_parcels SET status='inspection' WHERE parcel_id=?", (pid,))
                    self._lineage(connection, parcel_id=pid, container_id=container_id,
                                  action="inspection_started", gateway_id=gateway_id,
                                  leg_id=None, actor_id=actor_id, detail={"reason": reason})
                    self._open_responsibility(
                        connection, responsibility_id=f"insp-{container_id}-{pid}",
                        parcel_id=pid, party_type="node", party_id=gateway_id,
                        reason=f"inspection:{reason}")
                    held.append(pid)
                append_event(connection, actor_id=actor_id, action="postal.inspection.started",
                             resource_type="container", resource_id=container_id,
                             detail={"gateway_id": gateway_id, "reason": reason,
                                     "parcels": held}, occurred_at=self._now())
                return "container", container_id, {"container_id": container_id,
                                                   "gateway_id": gateway_id, "held": held}

            return self._write(connection, request_id=request_id,
                               action="start_inspection", payload=payload, create=create)

    def release_parcel(self, *, request_id: str, actor_id: str, container_id: str,
                       parcel_id: str, decision: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "container_id": container_id,
                   "parcel_id": parcel_id, "decision": decision}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *COMPLIANCE_ROLES)
            if decision not in ("released", "seized"):
                raise ValidationError("decision 必须是 released 或 seized")
            container = connection.execute(
                "SELECT * FROM postal_containers WHERE container_id=?", (container_id,)).fetchone()
            if container is None:
                raise NotFoundError("容器不存在")
            parcel = self._parcel(connection, parcel_id)

            def create():
                if parcel["status"] != "inspection":
                    raise ConflictError("包裹不在查验状态")
                if decision == "released":
                    connection.execute(
                        "UPDATE postal_parcels SET status='arrived' WHERE parcel_id=?",
                        (parcel_id,))
                    action = "inspection_released"
                else:
                    connection.execute(
                        "DELETE FROM postal_container_items WHERE container_id=? AND parcel_id=?",
                        (container_id, parcel_id))
                    connection.execute(
                        "UPDATE postal_parcels SET status='held' WHERE parcel_id=?", (parcel_id,))
                    plan = self._active_plan(connection, parcel_id)
                    if plan is not None:
                        connection.execute(
                            "UPDATE postal_plans SET status='cancelled' WHERE plan_id=?",
                            (plan["plan_id"],))
                        self._release_held_reservations(connection, plan["plan_id"])
                    action = "inspection_seized"
                self._lineage(connection, parcel_id=parcel_id, container_id=container_id,
                              action=action, gateway_id=container["current_gateway"],
                              leg_id=None, actor_id=actor_id, detail={"decision": decision})
                self._resolve_responsibilities(connection, parcel_id, f"insp-{container_id}-")
                party = PARTY_ROLES.get(actor.role, "compliance")
                connection.execute(
                    "INSERT OR IGNORE INTO postal_confirmations(resource_type,resource_id,party,"
                    "actor_id,decision,note,created_at) VALUES('inspection',?,?,?,'confirmed',?,?)",
                    (container_id, party, actor_id, f"查验放行:{decision}", self._now()))
                still_held = connection.execute(
                    "SELECT COUNT(*) AS count FROM postal_container_items i JOIN postal_parcels p "
                    "ON i.parcel_id=p.parcel_id WHERE i.container_id=? AND p.status='inspection'",
                    (container_id,)).fetchone()["count"]
                if still_held == 0 and container["state"] == "inspection":
                    left = connection.execute(
                        "SELECT COUNT(*) AS count FROM postal_container_items WHERE container_id=?",
                        (container_id,)).fetchone()["count"]
                    connection.execute(
                        "UPDATE postal_containers SET state=? WHERE container_id=?",
                        ("arrived" if left else "open", container_id))
                append_event(connection, actor_id=actor_id, action="postal.inspection.released",
                             resource_type="parcel", resource_id=parcel_id,
                             detail={"container_id": container_id, "decision": decision},
                             occurred_at=self._now())
                return "parcel", parcel_id, {"parcel_id": parcel_id, "decision": decision}

            return self._write(connection, request_id=request_id,
                               action="release_parcel", payload=payload, create=create)

    def deliver_parcel(self, *, request_id: str, actor_id: str,
                       parcel_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "parcel_id": parcel_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *NODE_ROLES)
            parcel = self._parcel(connection, parcel_id)

            def create():
                if parcel["status"] == FINAL_PARCEL_STATUS:
                    raise ConflictError("包裹已签收，记录不可重写")
                if parcel["status"] != "arrived":
                    raise ConflictError("包裹尚未到达目的口岸")
                if self._container_of(connection, parcel_id) is not None:
                    raise ConflictError("包裹仍在容器内，请先拆包")
                gateway = connection.execute(
                    "SELECT * FROM postal_gateways WHERE gateway_id=?",
                    (parcel["current_gateway"],)).fetchone()
                if gateway is None or gateway["country"] != parcel["destination_country"]:
                    raise ConflictError("包裹不在目的国口岸，不可签收")
                connection.execute(
                    "UPDATE postal_parcels SET status='delivered' WHERE parcel_id=?", (parcel_id,))
                plan = self._active_plan(connection, parcel_id)
                if plan is not None:
                    connection.execute(
                        "UPDATE postal_plans SET status='completed' WHERE plan_id=?",
                        (plan["plan_id"],))
                    self._release_held_reservations(connection, plan["plan_id"])
                self._lineage(connection, parcel_id=parcel_id, container_id=None,
                              action="delivered", gateway_id=parcel["current_gateway"],
                              leg_id=None, actor_id=actor_id, detail={})
                self._resolve_responsibilities(connection, parcel_id)
                append_event(connection, actor_id=actor_id, action="postal.parcel.delivered",
                             resource_type="parcel", resource_id=parcel_id,
                             detail={"gateway_id": parcel["current_gateway"]},
                             occurred_at=self._now())
                return "parcel", parcel_id, {"parcel_id": parcel_id, "status": "delivered"}

            return self._write(connection, request_id=request_id,
                               action="deliver_parcel", payload=payload, create=create)

    def confirm(self, *, request_id: str, actor_id: str, resource_type: str,
                resource_id: str, decision: str,
                note: str = "") -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "resource_type": resource_type,
                   "resource_id": resource_id, "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *PARTY_ROLES.keys())
            party = PARTY_ROLES[actor.role]
            resource_type = self.foundation._identifier(resource_type, "resource_type")
            resource_id = self.foundation._text(resource_id, "resource_id", 120)
            if decision not in ("confirmed", "rejected"):
                raise ValidationError("decision 必须是 confirmed 或 rejected")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO postal_confirmations(resource_type,resource_id,party,"
                        "actor_id,decision,note,created_at) VALUES(?,?,?,?,?,?,?)",
                        (resource_type, resource_id, party, actor_id, decision,
                         note or "", self._now()))
                except Exception as exc:
                    raise ConflictError("该方已对此交接作出确认，不可重复或改写") from exc
                append_event(connection, actor_id=actor_id, action="postal.confirmation.recorded",
                             resource_type=resource_type, resource_id=resource_id,
                             detail={"party": party, "decision": decision},
                             occurred_at=self._now())
                return resource_type, resource_id, {"resource_type": resource_type,
                                                    "resource_id": resource_id, "party": party}

            return self._write(connection, request_id=request_id,
                               action="confirm", payload=payload, create=create)

    def scan_timeouts(self, *, request_id: str,
                      actor_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, *OPERATOR_ROLES)

            def create():
                now = self.clock.now()
                opened = []
                rows = connection.execute(
                    "SELECT p.*, c.promised_hours FROM postal_parcels p JOIN postal_commitments c "
                    "ON p.commitment_id=c.commitment_id WHERE p.status!='delivered' "
                    "ORDER BY p.parcel_id").fetchall()
                for parcel in rows:
                    created = datetime.fromisoformat(parcel["created_at"].replace("Z", "+00:00"))
                    if created + timedelta(hours=parcel["promised_hours"]) > now:
                        continue
                    container = self._container_of(connection, parcel["parcel_id"])
                    if container is not None and container["state"] == "in_transit":
                        departed = connection.execute(
                            "SELECT leg_id FROM postal_lineage WHERE parcel_id=? AND "
                            "action='departed' ORDER BY sequence DESC LIMIT 1",
                            (parcel["parcel_id"],)).fetchone()
                        leg = connection.execute("SELECT * FROM postal_legs WHERE leg_id=?",
                                                 (departed["leg_id"],)).fetchone()
                        party_type, party_id = "carrier", leg["carrier_id"]
                    else:
                        party_type = "node"
                        party_id = parcel["current_gateway"] or parcel["origin_region"]
                    responsibility_id = f"tmo-{parcel['parcel_id']}-{party_type}-{party_id}"
                    connection.execute(
                        "UPDATE postal_responsibilities SET status='resolved', resolved_at=? "
                        "WHERE parcel_id=? AND status='open' AND responsibility_id LIKE 'tmo-%' "
                        "AND responsibility_id!=?",
                        (self._now(), parcel["parcel_id"], responsibility_id))
                    if self._open_responsibility(
                            connection, responsibility_id=responsibility_id,
                            parcel_id=parcel["parcel_id"], party_type=party_type,
                            party_id=party_id, reason="commitment_timeout"):
                        opened.append({"parcel_id": parcel["parcel_id"],
                                       "party_type": party_type, "party_id": party_id})
                append_event(connection, actor_id=actor_id, action="postal.timeout.scanned",
                             resource_type="commitment", resource_id="*",
                             detail={"opened": opened}, occurred_at=self._now())
                return "commitment", "timeout-scan", {"opened": opened}

            return self._write(connection, request_id=request_id,
                               action="scan_timeouts", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询：回溯、正向推导与可披露视图
    # ------------------------------------------------------------------
    def _require_reader(self, connection, actor_id: str):
        actor = self.foundation._actor(connection, actor_id)
        self.foundation._require(actor, *READER_ROLES)
        return actor

    def parcel_trace(self, *, actor_id: str, parcel_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            self._require_reader(connection, actor_id)
            parcel = self._parcel(connection, parcel_id)
            declarations = [self._declaration(connection, parcel_id, row["version"])
                            for row in connection.execute(
                                "SELECT version FROM postal_declarations WHERE parcel_id=? "
                                "ORDER BY version", (parcel_id,))]
            plans = []
            for row in connection.execute(
                    "SELECT * FROM postal_plans WHERE parcel_id=? ORDER BY version", (parcel_id,)):
                alternatives = [{"rank": alt["rank"], "legs": json.loads(alt["legs_json"]),
                                 "reasons": json.loads(alt["reasons_json"])}
                                for alt in connection.execute(
                                    "SELECT * FROM postal_plan_alternatives WHERE plan_id=? "
                                    "ORDER BY rank", (row["plan_id"],))]
                plans.append({"plan_id": row["plan_id"], "version": row["version"],
                              "status": row["status"], "legs": json.loads(row["legs_json"]),
                              "reasons": json.loads(row["reasons_json"]),
                              "alternatives": alternatives})
            lineage = [{"sequence": row["sequence"], "action": row["action"],
                        "container_id": row["container_id"], "gateway_id": row["gateway_id"],
                        "leg_id": row["leg_id"], "actor_id": row["actor_id"],
                        "detail": json.loads(row["detail_json"]),
                        "occurred_at": row["occurred_at"]}
                       for row in connection.execute(
                           "SELECT * FROM postal_lineage WHERE parcel_id=? ORDER BY sequence",
                           (parcel_id,))]
            responsibilities = [dict(row) for row in connection.execute(
                "SELECT * FROM postal_responsibilities WHERE parcel_id=? ORDER BY opened_at",
                (parcel_id,))]
            rules = self._active_rule_dicts(connection)
            declaration = declarations[-1]
            applied_rules = [rule for rule in rules
                             if selector_matches(rule["selector"], declaration)
                             and (rule["jurisdiction"] in
                                  (parcel["origin_region"], parcel["destination_country"])
                                  or rule["rule_scope"] == "transit")]
            handover_ids = [event["detail"]["handover_id"] for event in lineage
                            if event["detail"].get("handover_id")]
            containers = {event["container_id"] for event in lineage if event["container_id"]}
            confirmations = []
            for row in connection.execute("SELECT * FROM postal_confirmations"):
                if row["resource_id"] in handover_ids or \
                        (row["resource_type"] == "inspection" and row["resource_id"] in containers):
                    confirmations.append(dict(row))
            return {"parcel": dict(parcel), "declarations": declarations, "plans": plans,
                    "lineage": lineage, "responsibilities": responsibilities,
                    "applied_rules": applied_rules, "confirmations": confirmations}

    def support_view(self, *, actor_id: str, parcel_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            self.foundation._actor(connection, actor_id)
            parcel = self._parcel(connection, parcel_id)
            last = connection.execute(
                "SELECT * FROM postal_lineage WHERE parcel_id=? ORDER BY sequence DESC LIMIT 1",
                (parcel_id,)).fetchone()
            location = None
            if parcel["current_gateway"]:
                gateway = connection.execute(
                    "SELECT country FROM postal_gateways WHERE gateway_id=?",
                    (parcel["current_gateway"],)).fetchone()
                location = gateway["country"] if gateway else None
            return {"parcel_id": parcel_id,
                    "status": SUPPORT_STATUS.get(parcel["status"], "处理中"),
                    "location_country": location,
                    "updated_at": last["occurred_at"] if last else parcel["created_at"]}

    def gateway_impact(self, *, actor_id: str, gateway_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            self._require_reader(connection, actor_id)
            gateway = connection.execute(
                "SELECT * FROM postal_gateways WHERE gateway_id=?", (gateway_id,)).fetchone()
            if gateway is None:
                raise NotFoundError("口岸不存在")
            rows = connection.execute(
                "SELECT DISTINCT pa.parcel_id, pa.status, pa.commitment_id FROM postal_parcels pa "
                "JOIN postal_plans p ON p.parcel_id=pa.parcel_id AND p.status='active' "
                "JOIN postal_reservations r ON r.plan_id=p.plan_id AND r.status='held' "
                "JOIN postal_legs l ON l.leg_id=r.leg_id "
                "WHERE pa.status!='delivered' AND (l.from_gateway=? OR l.to_gateway=?) "
                "ORDER BY pa.parcel_id", (gateway_id, gateway_id)).fetchall()
            affected = []
            commitments: dict[str, dict[str, Any]] = {}
            for row in rows:
                last = connection.execute(
                    "SELECT action, detail_json, occurred_at FROM postal_lineage "
                    "WHERE parcel_id=? AND action IN ('rerouted','held') "
                    "ORDER BY sequence DESC LIMIT 1", (row["parcel_id"],)).fetchone()
                affected.append({
                    "parcel_id": row["parcel_id"], "status": row["status"],
                    "commitment_id": row["commitment_id"],
                    "last_outcome": {"action": last["action"],
                                     "detail": json.loads(last["detail_json"]),
                                     "occurred_at": last["occurred_at"]} if last else None})
                commitment = connection.execute(
                    "SELECT * FROM postal_commitments WHERE commitment_id=?",
                    (row["commitment_id"],)).fetchone()
                entry = commitments.setdefault(row["commitment_id"], {
                    "commitment_id": row["commitment_id"],
                    "promised_hours": commitment["promised_hours"], "at_risk_parcels": 0})
                entry["at_risk_parcels"] += 1
            held_here = connection.execute(
                "SELECT COUNT(*) AS count FROM postal_responsibilities "
                "WHERE party_id=? AND status='open'", (gateway_id,)).fetchone()["count"]
            return {"gateway": dict(gateway), "affected_parcels": affected,
                    "affected_commitments": list(commitments.values()),
                    "open_responsibilities_here": held_here}

    def container_view(self, *, actor_id: str, container_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            self._require_reader(connection, actor_id)
            container = connection.execute(
                "SELECT * FROM postal_containers WHERE container_id=?",
                (container_id,)).fetchone()
            if container is None:
                raise NotFoundError("容器不存在")
            items = [dict(row) for row in connection.execute(
                "SELECT p.parcel_id, p.status, p.destination_country, p.current_gateway "
                "FROM postal_container_items i JOIN postal_parcels p ON i.parcel_id=p.parcel_id "
                "WHERE i.container_id=? ORDER BY p.parcel_id", (container_id,))]
            events = [{"sequence": row["sequence"], "parcel_id": row["parcel_id"],
                       "action": row["action"], "gateway_id": row["gateway_id"],
                       "leg_id": row["leg_id"], "actor_id": row["actor_id"],
                       "occurred_at": row["occurred_at"]}
                      for row in connection.execute(
                          "SELECT * FROM postal_lineage WHERE container_id=? ORDER BY sequence",
                          (container_id,))]
            return {"container": dict(container), "items": items, "events": events}

    def leg_capacity_view(self, *, actor_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            self._require_reader(connection, actor_id)
            remaining = self._remaining_capacity(connection)
            legs = []
            for row in connection.execute("SELECT * FROM postal_legs ORDER BY leg_id"):
                used = connection.execute(
                    "SELECT COALESCE(SUM(units),0) AS used FROM postal_reservations "
                    "WHERE leg_id=? AND status IN ('held','consumed')",
                    (row["leg_id"],)).fetchone()["used"]
                legs.append({"leg_id": row["leg_id"], "status": row["status"],
                             "capacity": row["capacity"], "reserved": used,
                             "remaining": remaining.get(row["leg_id"], row["capacity"])})
            return {"legs": legs}
