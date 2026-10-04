"""离线端到端验收：在临时数据库上跑通跨境邮政路由全链路。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database

from .service import PostalService


class MutableClock:
    """可推进的验收时钟。"""

    def __init__(self, start: datetime) -> None:
        self._value = start

    def now(self) -> datetime:
        return self._value

    def advance(self, **kwargs) -> None:
        self._value = self._value + timedelta(**kwargs)


def _bootstrap(database: Database, clock: MutableClock) -> PostalService:
    foundation = DomainService(database, clock)
    postal = PostalService(database, foundation, clock)
    foundation.register_organization(request_id="req-org", actor_id="bootstrap",
                                     organization_id="org-kzpost", name="哈萨克斯坦邮政")
    foundation.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-1",
                              display_name="平台管理员", role="admin", organization_id="org-kzpost")
    foundation.register_actor(request_id="req-node", actor_id="admin-1", new_actor_id="node-1",
                              display_name="口岸操作员", role="node", organization_id="org-kzpost")
    foundation.register_actor(request_id="req-carrier", actor_id="admin-1", new_actor_id="carrier-1",
                              display_name="承运方代表", role="carrier", organization_id="org-kzpost")
    foundation.register_actor(request_id="req-compliance", actor_id="admin-1",
                              new_actor_id="compliance-1", display_name="合规专员",
                              role="compliance", organization_id="org-kzpost")
    foundation.register_actor(request_id="req-support", actor_id="admin-1",
                              new_actor_id="support-1", display_name="客服专员",
                              role="support", organization_id="org-kzpost")
    return postal


def _network(postal: PostalService) -> None:
    postal.register_commitment(request_id="req-cmt", actor_id="admin-1",
                               commitment_id="cmt-48h", description="四十八小时门到门",
                               promised_hours=48)
    for gateway_id, name, country, region in (
            ("gw-ala", "阿拉木图口岸", "KZ", "KZ-SE"),
            ("gw-urc", "乌鲁木齐口岸", "CN", "CN-XJ"),
            ("gw-svo", "莫斯科口岸", "RU", "RU-MOW"),
            ("gw-fra", "法兰克福口岸", "DE", "DE-HE")):
        postal.register_gateway(request_id=f"req-gw-{gateway_id}", actor_id="admin-1",
                                gateway_id=gateway_id, name=name, country=country, region=region)
    for leg_id, src, dst, carrier, capacity, priority in (
            ("leg-ala-fra", "gw-ala", "gw-fra", "kz-air", 2, 1),
            ("leg-ala-urc", "gw-ala", "gw-urc", "kz-air", 5, 1),
            ("leg-urc-fra", "gw-urc", "gw-fra", "cn-air", 5, 2),
            ("leg-ala-svo", "gw-ala", "gw-svo", "kz-air", 5, 3),
            ("leg-svo-fra", "gw-svo", "gw-fra", "ru-air", 5, 4)):
        postal.register_leg(request_id=f"req-leg-{leg_id}", actor_id="admin-1", leg_id=leg_id,
                            from_gateway=src, to_gateway=dst, carrier_id=carrier,
                            capacity=capacity, priority=priority)
    postal.publish_rule(request_id="req-rule-battery", actor_id="compliance-1",
                        rule_id="rule-de-battery", jurisdiction="DE",
                        rule_scope="destination", rule_type="prohibited_category",
                        selector={"item_category": "battery"}, constraint={})
    postal.publish_rule(request_id="req-rule-msds", actor_id="compliance-1",
                        rule_id="rule-cn-msds", jurisdiction="CN",
                        rule_scope="transit", rule_type="proof_required",
                        selector={"item_category": "cosmetics"},
                        constraint={"proof_type": "msds"})
    postal.publish_rule(request_id="req-rule-value", actor_id="compliance-1",
                        rule_id="rule-de-value", jurisdiction="DE",
                        rule_scope="destination", rule_type="value_threshold",
                        selector={"min_value_minor": 150000},
                        constraint={"requires_proof": "invoice"})


def run() -> dict[str, object]:
    """执行完整验收链并返回结构化结果。"""

    checks: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "postal.sqlite3"
        clock = MutableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        database = Database(path)
        postal = _bootstrap(database, clock)
        _network(postal)

        # 包裹事实与首版申报
        postal.register_parcel(request_id="req-p1", actor_id="node-1", parcel_id="p-001",
                               origin_region="KZ-SE", destination_country="DE",
                               weight_grams=1200, commitment_id="cmt-48h",
                               declared_value_minor=5000, currency="KZT",
                               item_category="general")
        postal.register_parcel(request_id="req-p2", actor_id="node-1", parcel_id="p-002",
                               origin_region="KZ-SE", destination_country="DE",
                               weight_grams=800, commitment_id="cmt-48h",
                               declared_value_minor=9000, currency="KZT",
                               item_category="cosmetics",
                               proofs=[{"type": "msds", "reference": "MSDS-77"}])
        postal.register_parcel(request_id="req-p3", actor_id="node-1", parcel_id="p-003",
                               origin_region="KZ-SE", destination_country="DE",
                               weight_grams=400, commitment_id="cmt-48h",
                               declared_value_minor=3000, currency="KZT",
                               item_category="battery")
        postal.register_parcel(request_id="req-p4", actor_id="node-1", parcel_id="p-004",
                               origin_region="KZ-SE", destination_country="DE",
                               weight_grams=600, commitment_id="cmt-48h",
                               declared_value_minor=2000, currency="KZT",
                               item_category="general")

        # 生成带理由的方案：直飞优先，备用路线按稳定优先级排序
        _, plan1 = postal.generate_plan(request_id="req-plan-1", actor_id="admin-1",
                                        parcel_id="p-001")
        _, plan2 = postal.generate_plan(request_id="req-plan-2", actor_id="admin-1",
                                        parcel_id="p-002")
        _, plan3 = postal.generate_plan(request_id="req-plan-3", actor_id="admin-1",
                                        parcel_id="p-003")
        checks["plan1_legs"] = [r for r in plan1["reasons"] if r["code"] == "capacity_reserved"]
        checks["plan1_has_backup"] = any(r["code"] == "backup_route" for r in plan1["reasons"])
        checks["battery_held"] = plan3["result"] == "held"

        # 重放同一请求不多占运力
        replay_receipt, _ = postal.generate_plan(request_id="req-plan-1", actor_id="admin-1",
                                                 parcel_id="p-001")
        capacity = postal.leg_capacity_view(actor_id="admin-1")
        direct = next(leg for leg in capacity["legs"] if leg["leg_id"] == "leg-ala-fra")
        checks["replay_no_double_reserve"] = replay_receipt.replayed and direct["reserved"] == 2

        # 直飞容量 2 已被 p-001/p-002 占满，p-004 落到备用路线
        _, plan4 = postal.generate_plan(request_id="req-plan-4", actor_id="admin-1",
                                        parcel_id="p-004")
        p4_legs = [r["leg_id"] for r in plan4["reasons"] if r["code"] == "capacity_reserved"]
        checks["overflow_to_backup"] = p4_legs == ["leg-ala-urc", "leg-urc-fra"]

        # 合包、封袋、发运：节点确认
        postal.register_container(request_id="req-bag", actor_id="node-1",
                                  container_id="bag-1", capacity=10, gateway_id="gw-ala")
        postal.consolidate(request_id="req-con", actor_id="node-1", container_id="bag-1",
                           parcel_ids=["p-001", "p-002"])
        postal.seal_container(request_id="req-seal", actor_id="node-1", container_id="bag-1")
        _, dispatched = postal.dispatch_container(request_id="req-disp", actor_id="node-1",
                                                  container_id="bag-1", leg_id="leg-ala-fra")
        checks["handover_id"] = dispatched["handover_id"]

        # 进程重启：在途容器、候补路线与责任记录保持一致
        database.close()
        database = Database(path)
        postal = _bootstrap_reopen(database, clock)
        view = postal.container_view(actor_id="admin-1", container_id="bag-1")
        trace = postal.parcel_trace(actor_id="admin-1", parcel_id="p-001")
        checks["restart_in_transit"] = view["container"]["state"] == "in_transit" \
            and len(view["items"]) == 2
        checks["restart_alternatives"] = len(trace["plans"][0]["alternatives"]) >= 1

        # 承运方确认到达交接
        postal.receive_container(request_id="req-recv", actor_id="carrier-1",
                                 container_id="bag-1")

        # 口岸查验：袋内包裹一起暂缓，责任从法兰克福节点开始
        _, inspection = postal.start_inspection(request_id="req-insp", actor_id="compliance-1",
                                                container_id="bag-1", reason="随机布控")
        checks["inspection_held_all"] = sorted(inspection["held"]) == ["p-001", "p-002"]
        postal.release_parcel(request_id="req-rel-1", actor_id="compliance-1",
                              container_id="bag-1", parcel_id="p-001", decision="released")
        postal.release_parcel(request_id="req-rel-2", actor_id="compliance-1",
                              container_id="bag-1", parcel_id="p-002", decision="released")
        trace = postal.parcel_trace(actor_id="admin-1", parcel_id="p-001")
        insp = [r for r in trace["responsibilities"] if r["responsibility_id"].startswith("insp-")]
        checks["inspection_responsibility"] = bool(insp) \
            and insp[0]["party_id"] == "gw-fra" and insp[0]["status"] == "resolved"

        # 拆包与签收：完成后的交接不可重写
        postal.deconsolidate(request_id="req-dec", actor_id="node-1", container_id="bag-1",
                             parcel_ids=["p-001", "p-002"])
        postal.deliver_parcel(request_id="req-dlv-1", actor_id="node-1", parcel_id="p-001")
        postal.deliver_parcel(request_id="req-dlv-2", actor_id="node-1", parcel_id="p-002")
        try:
            postal.add_declaration_version(request_id="req-late", actor_id="node-1",
                                           parcel_id="p-001", declared_value_minor=1,
                                           currency="KZT", item_category="general")
            checks["completed_immutable"] = False
        except Exception:
            checks["completed_immutable"] = True

        # 口岸关闭正向推导：p-004 的乌鲁木齐路线被命中并改道莫斯科
        _, closed = postal.set_gateway_status(request_id="req-close", actor_id="admin-1",
                                              gateway_id="gw-urc", status="closed")
        checks["closure_cascade"] = closed["affected"] == [
            {"parcel_id": "p-004", "result": "rerouted", "plan_id": "plan-p-004-v2"}]
        impact = postal.gateway_impact(actor_id="admin-1", gateway_id="gw-svo")
        checks["impact_forward"] = [p["parcel_id"] for p in impact["affected_parcels"]] == ["p-004"]

        # 超时责任扫描：可重放、可重启、不多开
        clock.advance(hours=49)
        _, scan1 = postal.scan_timeouts(request_id="req-scan-1", actor_id="admin-1")
        _, scan2 = postal.scan_timeouts(request_id="req-scan-2", actor_id="admin-1")
        checks["timeout_opened"] = sorted(o["parcel_id"] for o in scan1["opened"])
        checks["timeout_idempotent"] = scan2["opened"] == []

        # 客服只能看到可披露状态
        support = postal.support_view(actor_id="support-1", parcel_id="p-003")
        checks["support_view"] = support["status"]
        try:
            postal.parcel_trace(actor_id="support-1", parcel_id="p-003")
            checks["support_blocked"] = False
        except Exception:
            checks["support_blocked"] = True

        # 再次重启后责任与在途状态仍然一致
        database.close()
        database = Database(path)
        postal = _bootstrap_reopen(database, clock)
        trace3 = postal.parcel_trace(actor_id="admin-1", parcel_id="p-003")
        open_resp = [r for r in trace3["responsibilities"] if r["status"] == "open"]
        checks["restart_responsibility"] = len(open_resp) >= 1
        trace1 = postal.parcel_trace(actor_id="admin-1", parcel_id="p-001")
        checks["trace_lineage_complete"] = [e["action"] for e in trace1["lineage"]] == [
            "registered", "planned", "consolidated", "sealed", "departed", "arrived",
            "inspection_started", "inspection_released", "deconsolidated", "delivered"]
        valid, events = postal.foundation.verify_audit()
        checks["audit_valid"] = valid
        checks["audit_events"] = events
        database.close()

    ok = all([
        checks["plan1_has_backup"],
        checks["battery_held"],
        checks["replay_no_double_reserve"],
        checks["overflow_to_backup"],
        checks["restart_in_transit"],
        checks["restart_alternatives"],
        checks["inspection_held_all"],
        checks["inspection_responsibility"],
        checks["completed_immutable"],
        checks["closure_cascade"],
        checks["impact_forward"],
        checks["timeout_opened"] == ["p-003", "p-004"],
        checks["timeout_idempotent"],
        checks["support_view"] == "暂存待处理",
        checks["support_blocked"],
        checks["restart_responsibility"],
        checks["trace_lineage_complete"],
        checks["audit_valid"],
    ])
    return {"status": "ok" if ok else "failed", "checks": checks}


def _bootstrap_reopen(database: Database, clock: MutableClock) -> PostalService:
    """模拟进程重启：只重建服务对象，不触碰持久化状态。"""

    foundation = DomainService(database, clock)
    return PostalService(database, foundation, clock)


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
