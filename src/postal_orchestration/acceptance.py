"""运行跨境邮政路由与责任编排平台的离线端到端验收。

场景：哈萨克斯坦邮政承接国际电商中转，三个包裹从阿斯塔纳发往柏林，
途中经历合包、封袋、三方交接、口岸查验、口岸关闭改道、候补、超时定责、
规则版本变更与申报更正，并两次重启进程验证状态一致。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from digital_trade_foundation.audit import verify_chain
from digital_trade_foundation.errors import ConflictError, PermissionDenied, ValidationError
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database

from .service import PostalService


class StepClock:
    """可推进的验收时钟。"""

    def __init__(self, start: datetime) -> None:
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, hours: int) -> None:
        self._current += timedelta(hours=hours)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"验收失败：{message}")


def _move_container(service: PostalService, container_id: str, node_out: str,
                    carrier: str, node_in: str, compliance: str = "op-compliance") -> None:
    """封袋（若尚未封）、三方确认发运、承运方到港、节点确认到达。"""

    lineage = service.container_lineage(actor_id="op-admin", container_id=container_id)
    if lineage["container"]["state"] == "open":
        sealed = service.seal_container(request_id=f"seal-{container_id}",
                                        actor_id=node_out, container_id=container_id)
        handover_id = sealed["handover_id"]
        service.confirm_handover(request_id=f"cfm-{handover_id}-n",
                                 actor_id=node_out, handover_id=handover_id)
        service.confirm_handover(request_id=f"cfm-{handover_id}-c",
                                 actor_id=carrier, handover_id=handover_id)
        done = service.confirm_handover(request_id=f"cfm-{handover_id}-g",
                                        actor_id=compliance, handover_id=handover_id)
        _check(done["completed"], f"{container_id} 发运交接应完成")
    arrived = service.arrive_container(request_id=f"arr-{container_id}",
                                       actor_id=carrier, container_id=container_id)
    service.confirm_handover(request_id=f"cfm-{arrived['handover_id']}-in",
                             actor_id=node_in, handover_id=arrived["handover_id"])


def run() -> dict[str, object]:
    """执行完整验收链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "postal.sqlite3"
        clock = StepClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        database = Database(path)
        foundation = DomainService(database, clock)
        service = PostalService(database, clock)

        # ---------- 主体与操作者 ----------
        foundation.register_organization(request_id="org", actor_id="bootstrap",
                                         organization_id="org-kzpost", name="哈萨克斯坦邮政")
        foundation.register_actor(request_id="fa", actor_id="bootstrap", new_actor_id="fa-admin",
                                  display_name="平台管理员", role="admin",
                                  organization_id="org-kzpost")
        for operator_id, role, node_id, carrier_id in [
            ("op-admin", "admin", None, None),
            ("op-compliance", "compliance", None, None),
            ("op-support", "support", None, None),
        ]:
            service.register_operator(request_id=f"op-{operator_id}", actor_id="fa-admin",
                                      operator_id=operator_id, display_name=operator_id,
                                      role=role, node_id=node_id, carrier_id=carrier_id)

        # ---------- 口岸、区段、承诺、规则 ----------
        for node_id, name, jurisdiction, kind in [
            ("N-AST", "阿斯塔纳枢纽", "KZ", "office"),
            ("N-ALA", "阿拉山口口岸", "KZ", "port"),
            ("N-URC", "乌鲁木齐口岸", "CN", "port"),
            ("N-BER", "柏林交换站", "DE", "office"),
            ("N-MSK", "莫斯科交换站", "RU", "office"),
        ]:
            service.register_node(request_id=f"node-{node_id}", actor_id="op-admin",
                                  node_id=node_id, name=name, jurisdiction=jurisdiction,
                                  kind=kind)
        for operator_id, node_id in [("op-ast", "N-AST"), ("op-ala", "N-ALA"),
                                     ("op-urc", "N-URC"), ("op-ber", "N-BER"),
                                     ("op-msk", "N-MSK")]:
            service.register_operator(request_id=f"op-{operator_id}", actor_id="fa-admin",
                                      operator_id=operator_id, display_name=operator_id,
                                      role="node", node_id=node_id)
        for operator_id, carrier_id in [("op-rail", "C-KZRAIL"), ("op-air", "C-KZAIR")]:
            service.register_operator(request_id=f"op-{operator_id}", actor_id="fa-admin",
                                      operator_id=operator_id, display_name=operator_id,
                                      role="carrier", carrier_id=carrier_id)
        for segment_id, from_node, to_node, carrier, mode, capacity, hours, priority in [
            ("S-AST-ALA", "N-AST", "N-ALA", "C-KZRAIL", "rail", 2, 20, 1),
            ("S-ALA-URC", "N-ALA", "N-URC", "C-KZRAIL", "rail", 1, 30, 1),
            ("S-URC-BER", "N-URC", "N-BER", "C-KZAIR", "air", 2, 90, 1),
            ("S-AST-MSK", "N-AST", "N-MSK", "C-KZRAIL", "rail", 1, 55, 5),
            ("S-MSK-BER", "N-MSK", "N-BER", "C-KZAIR", "air", 1, 100, 5),
        ]:
            service.register_segment(request_id=f"seg-{segment_id}", actor_id="op-admin",
                                     segment_id=segment_id, from_node=from_node,
                                     to_node=to_node, carrier_id=carrier, mode=mode,
                                     capacity=capacity, transit_hours=hours, priority=priority)
        service.register_commitment(request_id="cm-std", actor_id="op-admin",
                                    commitment_id="CM-STD", product="电商标准达",
                                    max_transit_hours=240)
        service.publish_rule(request_id="rule-prohibit", actor_id="op-compliance",
                             rule_id="R-DE-PROHIBIT", jurisdiction="DE", scope="destination",
                             rule_type="prohibited_category",
                             expression={"categories": ["weapons"]})
        service.publish_rule(request_id="rule-cap", actor_id="op-compliance",
                             rule_id="R-DE-CAP", jurisdiction="DE", scope="destination",
                             rule_type="value_cap",
                             expression={"max_value": 1000, "currency": "EUR"})
        service.publish_rule(request_id="rule-permit", actor_id="op-compliance",
                             rule_id="R-CN-PERMIT", jurisdiction="CN", scope="transit",
                             rule_type="requires_proof",
                             expression={"proof_kind": "transit_permit",
                                         "categories": ["cosmetics"]})

        # ---------- 收寄与规划 ----------
        service.register_parcel(request_id="p-001", actor_id="op-ast", parcel_id="P-001",
                                origin_node="N-AST", destination_node="N-BER",
                                category="clothes", declared_value=200, currency="EUR",
                                weight_grams=1200, commitment_id="CM-STD")
        replay = service.register_parcel(request_id="p-001", actor_id="op-ast",
                                         parcel_id="P-001", origin_node="N-AST",
                                         destination_node="N-BER", category="clothes",
                                         declared_value=200, currency="EUR",
                                         weight_grams=1200, commitment_id="CM-STD")
        _check(replay["replayed"], "重复收寄应命中幂等回执")
        service.register_parcel(request_id="p-002", actor_id="op-ast", parcel_id="P-002",
                                origin_node="N-AST", destination_node="N-BER",
                                category="cosmetics", declared_value=150, currency="EUR",
                                weight_grams=800, commitment_id="CM-STD",
                                proofs=[{"kind": "transit_permit", "ref": "TP-1", "version": 1}])
        service.register_parcel(request_id="p-003", actor_id="op-ast", parcel_id="P-003",
                                origin_node="N-AST", destination_node="N-BER",
                                category="cosmetics", declared_value=90, currency="EUR",
                                weight_grams=600, commitment_id="CM-STD")
        trace = service.parcel_trace(actor_id="op-admin", parcel_id="P-003")
        alt_segments = [leg["segment_id"] for plan in trace["plans"] for leg in plan["legs"]
                        if plan["plan"]["state"] == "active"]
        _check(alt_segments == ["S-AST-MSK", "S-MSK-BER"],
               "缺少过境许可的化妆品应改走莫斯科路径")

        # ---------- 合包：不能任意合袋 ----------
        service.create_container(request_id="c-1", actor_id="op-ast", container_id="C-1",
                                 segment_id="S-AST-ALA", capacity=5)
        service.create_container(request_id="c-2", actor_id="op-ast", container_id="C-2",
                                 segment_id="S-AST-MSK", capacity=1)
        try:
            service.load_parcel(request_id="bad-load", actor_id="op-ast",
                                container_id="C-1", parcel_id="P-003")
            raise RuntimeError("验收失败：路线不一致的包裹不应允许合袋")
        except ValidationError:
            pass
        service.load_parcel(request_id="load-1", actor_id="op-ast",
                            container_id="C-1", parcel_id="P-001")
        service.load_parcel(request_id="load-2", actor_id="op-ast",
                            container_id="C-1", parcel_id="P-002")
        service.load_parcel(request_id="load-3", actor_id="op-ast",
                            container_id="C-2", parcel_id="P-003")

        # ---------- 封袋与容量预留幂等 ----------
        sealed = service.seal_container(request_id="seal-C-1", actor_id="op-ast",
                                        container_id="C-1")
        seal_replay = service.seal_container(request_id="seal-C-1", actor_id="op-ast",
                                             container_id="C-1")
        _check(seal_replay["replayed"], "重复封袋应命中幂等回执")
        held = database.connection.execute(
            "SELECT COALESCE(SUM(units),0) AS units FROM reservations "
            "WHERE segment_id='S-AST-ALA' AND state='held'").fetchone()["units"]
        _check(held == 1, "重放封袋不能多占区段容量")
        handover_id = sealed["handover_id"]
        service.confirm_handover(request_id="cfm-h1-n", actor_id="op-ast",
                                 handover_id=handover_id)
        service.confirm_handover(request_id="cfm-h1-c", actor_id="op-rail",
                                 handover_id=handover_id)
        done = service.confirm_handover(request_id="cfm-h1-g", actor_id="op-compliance",
                                        handover_id=handover_id)
        _check(done["completed"], "三方确认后交接应完成")

        # ---------- 重启一：在途容器状态保持一致 ----------
        database.close()
        database = Database(path)
        service = PostalService(database, clock)
        lineage = service.container_lineage(actor_id="op-admin", container_id="C-1")
        _check(lineage["container"]["state"] == "in_transit",
               "重启后在途容器应保持 in_transit")

        # ---------- 到港、查验 ----------
        arrived = service.arrive_container(request_id="arr-C-1", actor_id="op-rail",
                                           container_id="C-1")
        service.confirm_handover(request_id="cfm-arr1", actor_id="op-ala",
                                 handover_id=arrived["handover_id"])
        inspection = service.open_inspection(request_id="insp-C-1", actor_id="op-compliance",
                                             container_id="C-1", reason="口岸抽检")
        _check(sorted(inspection["held_parcels"]) == ["P-001", "P-002"],
               "查验应挂起袋内全部包裹")
        closed = service.close_inspection(request_id="insp-C-1-close", actor_id="op-compliance",
                                          container_id="C-1",
                                          results=[{"parcel_id": "P-001", "outcome": "pass"},
                                                   {"parcel_id": "P-002", "outcome": "pass"}])
        _check(closed["state"] == "arrived", "查验放行后容器应恢复到港状态")
        service.unload_parcel(request_id="unload-1", actor_id="op-ala",
                              container_id="C-1", parcel_id="P-001")
        service.unload_parcel(request_id="unload-2", actor_id="op-ala",
                              container_id="C-1", parcel_id="P-002")

        # ---------- 口岸关闭：正向推导受影响承诺并候补 ----------
        impact = service.set_node_status(request_id="close-urc", actor_id="op-compliance",
                                         node_id="N-URC", status="closed",
                                         reason="口岸大风关闭")
        affected = {item.get("parcel_id"): item.get("action")
                    for item in impact["affected"] if item["type"] == "parcel"}
        _check(affected == {"P-001": "waitlisted", "P-002": "waitlisted"},
               "口岸关闭应使受影响包裹进入候补")

        # ---------- 重启二：候补状态保持一致 ----------
        database.close()
        database = Database(path)
        service = PostalService(database, clock)
        waiting = service.list_waitlist(actor_id="op-admin", node_id="N-ALA")
        _check(len(waiting["items"]) == 2, "重启后候补队列应保持")
        reopened = service.set_node_status(request_id="open-urc", actor_id="op-compliance",
                                           node_id="N-URC", status="open", reason="恢复通行")
        _check(sorted(reopened["promoted"]) == ["P-001", "P-002"],
               "口岸重开后候补应按稳定优先级转正")

        # ---------- 继续中转：ALA→URC→BER ----------
        service.create_container(request_id="c-3", actor_id="op-ala", container_id="C-3",
                                 segment_id="S-ALA-URC", capacity=2)
        service.load_parcel(request_id="load-4", actor_id="op-ala",
                            container_id="C-3", parcel_id="P-001")
        service.load_parcel(request_id="load-5", actor_id="op-ala",
                            container_id="C-3", parcel_id="P-002")
        _move_container(service, "C-3", "op-ala", "op-rail", "op-urc")
        service.unload_parcel(request_id="unload-3", actor_id="op-urc",
                              container_id="C-3", parcel_id="P-001")
        service.unload_parcel(request_id="unload-4", actor_id="op-urc",
                              container_id="C-3", parcel_id="P-002")
        service.create_container(request_id="c-4", actor_id="op-urc", container_id="C-4",
                                 segment_id="S-URC-BER", capacity=2)
        service.load_parcel(request_id="load-6", actor_id="op-urc",
                            container_id="C-4", parcel_id="P-001")
        service.load_parcel(request_id="load-7", actor_id="op-urc",
                            container_id="C-4", parcel_id="P-002")
        sealed4 = service.seal_container(request_id="seal-C-4", actor_id="op-urc",
                                         container_id="C-4")
        service.confirm_handover(request_id="cfm-h4-n", actor_id="op-urc",
                                 handover_id=sealed4["handover_id"])
        service.confirm_handover(request_id="cfm-h4-c", actor_id="op-air",
                                 handover_id=sealed4["handover_id"])
        service.confirm_handover(request_id="cfm-h4-g", actor_id="op-compliance",
                                 handover_id=sealed4["handover_id"])

        # ---------- P-003 经莫斯科路径按时签收 ----------
        _move_container(service, "C-2", "op-ast", "op-rail", "op-msk")
        service.unload_parcel(request_id="unload-5", actor_id="op-msk",
                              container_id="C-2", parcel_id="P-003")
        service.create_container(request_id="c-5", actor_id="op-msk", container_id="C-5",
                                 segment_id="S-MSK-BER", capacity=1)
        service.load_parcel(request_id="load-8", actor_id="op-msk",
                            container_id="C-5", parcel_id="P-003")
        _move_container(service, "C-5", "op-msk", "op-air", "op-ber")
        service.unload_parcel(request_id="unload-6", actor_id="op-ber",
                              container_id="C-5", parcel_id="P-003")
        delivered3 = service.deliver_parcel(request_id="deliver-3", actor_id="op-ber",
                                            parcel_id="P-003")
        _check(delivered3["outcome"] == "met", "P-003 应按时签收")

        # ---------- 超时责任 ----------
        clock.advance(300)
        timeouts = service.evaluate_timeouts(request_id="scan-1", actor_id="op-admin")
        _check(timeouts["records_opened"] == 2, "在途超时应为两个包裹各记一条责任")
        arrived4 = service.arrive_container(request_id="arr-C-4", actor_id="op-air",
                                            container_id="C-4")
        service.confirm_handover(request_id="cfm-arr4", actor_id="op-ber",
                                 handover_id=arrived4["handover_id"])
        service.unload_parcel(request_id="unload-7", actor_id="op-ber",
                              container_id="C-4", parcel_id="P-001")
        service.unload_parcel(request_id="unload-8", actor_id="op-ber",
                              container_id="C-4", parcel_id="P-002")

        # ---------- 规则版本变更只影响未完成且命中的包裹 ----------
        change = service.publish_rule(request_id="rule-cap-v2", actor_id="op-compliance",
                                      rule_id="R-DE-CAP", jurisdiction="DE",
                                      scope="destination", rule_type="value_cap",
                                      expression={"max_value": 150, "currency": "EUR"})
        _check(change["affected"] == ["P-001"], "只有申报 200 EUR 的 P-001 应命中新规则")
        trace3 = service.parcel_trace(actor_id="op-compliance", parcel_id="P-003")
        _check(not any(h["state"] == "open" for h in trace3["holds"]),
               "已签收的 P-003 不应受规则变更影响")
        delivered2 = service.deliver_parcel(request_id="deliver-2", actor_id="op-ber",
                                            parcel_id="P-002")
        _check(delivered2["outcome"] == "breached", "P-002 超时签收应记为 breached")
        try:
            service.deliver_parcel(request_id="deliver-1", actor_id="op-ber",
                                   parcel_id="P-001")
            raise RuntimeError("验收失败：存在合规扣留的包裹不应允许签收")
        except ConflictError:
            pass

        # ---------- 申报版本更正后放行签收 ----------
        declaration = service.submit_declaration(request_id="decl-1-v2", actor_id="op-ber",
                                                 parcel_id="P-001", declared_value=120,
                                                 currency="EUR", category="clothes")
        _check(declaration["open_holds"] == 0, "申报更正后扣留应解除")
        delivered1 = service.deliver_parcel(request_id="deliver-1-v2", actor_id="op-ber",
                                            parcel_id="P-001")
        _check(delivered1["outcome"] == "breached", "P-001 超时签收应记为 breached")

        # ---------- 客户支持披露与追溯 ----------
        view = service.support_view(actor_id="op-support", parcel_id="P-001")
        _check(view["status_code"] == "delivered" and view["status_text"] == "已签收",
               "客户支持应看到已签收")
        try:
            service.parcel_trace(actor_id="op-support", parcel_id="P-001")
            raise RuntimeError("验收失败：客户支持不应查看内部追溯")
        except PermissionDenied:
            pass
        trace1 = service.parcel_trace(actor_id="op-compliance", parcel_id="P-001")
        containers_used = sorted({row["container_id"] for row in trace1["containers"]
                                  if row["action"] == "loaded"})
        _check(containers_used == ["C-1", "C-3", "C-4"], "包裹谱系应覆盖全部容器")
        cap_evals = {(row["rule_version"], row["result"])
                     for row in trace1["rule_evaluations"] if row["rule_id"] == "R-DE-CAP"}
        _check((1, "pass") in cap_evals and (2, "violation") in cap_evals
               and (2, "pass") in cap_evals, "规则回看应包含两个版本的评估")
        parties = {row["responsible_party"] for row in trace1["responsibility"]}
        _check("carrier:C-KZAIR" in parties, "超时责任应归属承运方 C-KZAIR")
        node_impact = service.node_impact(actor_id="op-compliance", node_id="N-URC")
        _check(len(node_impact["reports"]) == 1, "口岸关闭应生成影响报告")
        impacted = {item.get("parcel_id") for item in node_impact["reports"][0]
                    ["detail"]["affected"] if item["type"] == "parcel"}
        _check(impacted == {"P-001", "P-002"}, "影响报告应列出受影响包裹及其承诺")

        valid, event_count = verify_chain(database.connection)
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "parcels_delivered": 3,
            "commitment_met": 1,
            "commitment_breached": 2,
            "register_replayed": replay["replayed"],
            "seal_replayed": seal_replay["replayed"],
            "restart_checks": 2,
            "waitlist_promoted": len(reopened["promoted"]),
            "timeout_records": timeouts["records_opened"],
            "rule_change_affected": change["affected"],
            "impact_reports": len(node_impact["reports"]),
            "support_status": view["status_text"],
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
