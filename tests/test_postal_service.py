import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from digital_trade_foundation.errors import ConflictError, PermissionDenied, ValidationError
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database
from postal_orchestration.service import PostalService


class MutableClock:
    def __init__(self, start):
        self._current = start

    def now(self):
        return self._current

    def advance(self, hours):
        self._current += timedelta(hours=hours)


class PostalServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        foundation = DomainService(self.database, self.clock)
        foundation.register_organization(request_id="org", actor_id="bootstrap",
                                         organization_id="org-1", name="哈萨克斯坦邮政")
        foundation.register_actor(request_id="fa", actor_id="bootstrap", new_actor_id="fa-admin",
                                  display_name="平台管理员", role="admin", organization_id="org-1")
        self.service = PostalService(self.database, self.clock)
        self._build_world()

    def tearDown(self):
        self.database.close()

    def _build_world(self):
        s = self.service
        for operator_id, role in [("op-admin", "admin"), ("op-compliance", "compliance"),
                                  ("op-support", "support")]:
            s.register_operator(request_id=f"op-{operator_id}", actor_id="fa-admin",
                                operator_id=operator_id, display_name=operator_id, role=role)
        for node_id, jurisdiction in [("N-A", "KZ"), ("N-B", "KZ"), ("N-C", "CN"),
                                      ("N-D", "DE"), ("N-E", "RU")]:
            s.register_node(request_id=f"node-{node_id}", actor_id="op-admin", node_id=node_id,
                            name=f"节点{node_id}", jurisdiction=jurisdiction, kind="port")
        for operator_id, node_id in [("op-na", "N-A"), ("op-nb", "N-B"), ("op-nc", "N-C"),
                                     ("op-nd", "N-D"), ("op-ne", "N-E")]:
            s.register_operator(request_id=f"op-{operator_id}", actor_id="fa-admin",
                                operator_id=operator_id, display_name=operator_id,
                                role="node", node_id=node_id)
        for operator_id, carrier_id in [("op-c1", "C-1"), ("op-c2", "C-2")]:
            s.register_operator(request_id=f"op-{operator_id}", actor_id="fa-admin",
                                operator_id=operator_id, display_name=operator_id,
                                role="carrier", carrier_id=carrier_id)
        for segment_id, a, b, carrier, capacity, hours, priority in [
            ("S-AB", "N-A", "N-B", "C-1", 2, 24, 1),
            ("S-BC", "N-B", "N-C", "C-1", 1, 48, 1),
            ("S-CD", "N-C", "N-D", "C-2", 2, 72, 1),
            ("S-AE", "N-A", "N-E", "C-1", 1, 60, 2),
            ("S-ED", "N-E", "N-D", "C-2", 1, 100, 2),
        ]:
            s.register_segment(request_id=f"seg-{segment_id}", actor_id="op-admin",
                               segment_id=segment_id, from_node=a, to_node=b,
                               carrier_id=carrier, mode="rail", capacity=capacity,
                               transit_hours=hours, priority=priority)
        s.register_commitment(request_id="cm-1", actor_id="op-admin", commitment_id="CM-1",
                              product="电商标准达", max_transit_hours=240)
        s.publish_rule(request_id="r-ban", actor_id="op-compliance", rule_id="R-BAN",
                       jurisdiction="DE", scope="destination",
                       rule_type="prohibited_category", expression={"categories": ["weapons"]})
        s.publish_rule(request_id="r-cap", actor_id="op-compliance", rule_id="R-CAP",
                       jurisdiction="DE", scope="destination", rule_type="value_cap",
                       expression={"max_value": 1000, "currency": "EUR"})
        s.publish_rule(request_id="r-permit", actor_id="op-compliance", rule_id="R-PERMIT",
                       jurisdiction="CN", scope="transit", rule_type="requires_proof",
                       expression={"proof_kind": "transit_permit", "categories": ["cosmetics"]})

    # ------------------------------------------------------------------
    # 测试辅助
    # ------------------------------------------------------------------

    def _parcel(self, parcel_id, category="clothes", value=100, proofs=None, request_id=None):
        return self.service.register_parcel(
            request_id=request_id or f"parcel-{parcel_id}", actor_id="op-na",
            parcel_id=parcel_id, origin_node="N-A", destination_node="N-D",
            category=category, declared_value=value, currency="EUR",
            weight_grams=500, commitment_id="CM-1", proofs=proofs or [])

    def _container(self, container_id, segment_id, capacity, node_op):
        return self.service.create_container(request_id=f"ct-{container_id}", actor_id=node_op,
                                             container_id=container_id, segment_id=segment_id,
                                             capacity=capacity)

    def _load(self, container_id, parcel_id, node_op):
        return self.service.load_parcel(request_id=f"load-{container_id}-{parcel_id}",
                                        actor_id=node_op, container_id=container_id,
                                        parcel_id=parcel_id)

    def _seal(self, container_id, node_op, request_id=None):
        return self.service.seal_container(request_id=request_id or f"seal-{container_id}",
                                           actor_id=node_op, container_id=container_id)

    def _dispatch(self, container_id, node_op, carrier_op, request_id=None):
        sealed = self._seal(container_id, node_op, request_id=request_id)
        handover_id = sealed["handover_id"]
        self.service.confirm_handover(request_id=f"{handover_id}-n", actor_id=node_op,
                                      handover_id=handover_id)
        self.service.confirm_handover(request_id=f"{handover_id}-c", actor_id=carrier_op,
                                      handover_id=handover_id)
        return self.service.confirm_handover(request_id=f"{handover_id}-g",
                                             actor_id="op-compliance", handover_id=handover_id)

    def _arrive(self, container_id, carrier_op, node_op):
        arrived = self.service.arrive_container(request_id=f"arr-{container_id}",
                                                actor_id=carrier_op, container_id=container_id)
        return self.service.confirm_handover(request_id=f"{arrived['handover_id']}-in",
                                             actor_id=node_op,
                                             handover_id=arrived["handover_id"])

    def _active_legs(self, parcel_id):
        trace = self.service.parcel_trace(actor_id="op-admin", parcel_id=parcel_id)
        for plan in trace["plans"]:
            if plan["plan"]["state"] == "active":
                return [leg["segment_id"] for leg in plan["legs"]]
        return None

    # ------------------------------------------------------------------
    # 规划与规则
    # ------------------------------------------------------------------

    def test_plan_has_reasons_and_standby(self):
        result = self._parcel("P-1")
        self.assertEqual("planned", result["plan"]["outcome"])
        trace = self.service.parcel_trace(actor_id="op-admin", parcel_id="P-1")
        active = [p for p in trace["plans"] if p["plan"]["state"] == "active"][0]
        self.assertEqual(["S-AB", "S-BC", "S-CD"], [l["segment_id"] for l in active["legs"]])
        self.assertTrue(all(leg["reason"] for leg in active["legs"]))
        self.assertIn("总耗时最低", active["plan"]["reason"]["summary"])
        self.assertEqual(1, len(trace["standbys"]))
        self.assertEqual(1, trace["standbys"][0]["rank"])
        self.assertEqual(["S-AE", "S-ED"], json.loads(trace["standbys"][0]["legs_json"]))
        evaluations = {(e["rule_id"], e["result"]) for e in trace["rule_evaluations"]}
        self.assertIn(("R-BAN", "pass"), evaluations)
        self.assertIn(("R-CAP", "pass"), evaluations)

    def test_rule_violation_forces_alternative_route(self):
        self._parcel("P-2", category="cosmetics")
        self.assertEqual(["S-AE", "S-ED"], self._active_legs("P-2"))
        self._parcel("P-3", category="cosmetics",
                     proofs=[{"kind": "transit_permit", "ref": "TP", "version": 1}])
        self.assertEqual(["S-AB", "S-BC", "S-CD"], self._active_legs("P-3"))

    def test_no_route_goes_waitlist_with_responsibility(self):
        self.service.set_node_status(request_id="close-c", actor_id="op-compliance",
                                     node_id="N-C", status="closed", reason="关闭")
        self.service.set_node_status(request_id="close-e", actor_id="op-compliance",
                                     node_id="N-E", status="closed", reason="关闭")
        result = self._parcel("P-4")
        self.assertEqual("waitlisted", result["plan"]["outcome"])
        waiting = self.service.list_waitlist(actor_id="op-admin", node_id="N-A")
        self.assertEqual(["P-4"], [row["parcel_id"] for row in waiting["items"]])
        trace = self.service.parcel_trace(actor_id="op-admin", parcel_id="P-4")
        self.assertEqual("waitlist", trace["responsibility"][0]["stage"])
        self.assertEqual("node:N-A", trace["responsibility"][0]["responsible_party"])

    # ------------------------------------------------------------------
    # 合袋与容量
    # ------------------------------------------------------------------

    def test_cannot_load_into_mismatched_container(self):
        self._parcel("P-5")
        self._container("K-1", "S-AE", 2, "op-na")
        with self.assertRaises(ValidationError):
            self._load("K-1", "P-5", "op-na")

    def test_container_capacity_atomic_and_replay_safe(self):
        self._parcel("P-6")
        self._parcel("P-7")
        self._container("K-2", "S-AB", 1, "op-na")
        first = self.service.load_parcel(request_id="load-once", actor_id="op-na",
                                         container_id="K-2", parcel_id="P-6")
        self.assertFalse(first["replayed"])
        with self.assertRaises(ConflictError):
            self._load("K-2", "P-7", "op-na")
        replay = self.service.load_parcel(request_id="load-once", actor_id="op-na",
                                          container_id="K-2", parcel_id="P-6")
        self.assertTrue(replay["replayed"])
        lineage = self.service.container_lineage(actor_id="op-admin", container_id="K-2")
        self.assertEqual(1, lineage["container"]["loaded_count"])
        self.assertEqual(["P-6"], lineage["current_members"])

    def test_segment_reservation_atomic_and_replay_safe(self):
        self.service.update_segment_capacity(request_id="cap-ab", actor_id="op-admin",
                                             segment_id="S-AB", capacity=1)
        self._parcel("P-8")
        self._parcel("P-9")
        self._container("K-3", "S-AB", 2, "op-na")
        self._container("K-4", "S-AB", 2, "op-na")
        self._load("K-3", "P-8", "op-na")
        self._load("K-4", "P-9", "op-na")
        self._seal("K-3", "op-na", request_id="seal-once")
        with self.assertRaises(ConflictError):
            self._seal("K-4", "op-na")
        replay = self._seal("K-3", "op-na", request_id="seal-once")
        self.assertTrue(replay["replayed"])
        held = self.database.connection.execute(
            "SELECT COALESCE(SUM(units),0) AS units FROM reservations WHERE state='held'"
        ).fetchone()["units"]
        self.assertEqual(1, held)

    def test_unseal_releases_reservation_and_cancels_handover(self):
        self._parcel("P-10")
        self._container("K-5", "S-AB", 2, "op-na")
        self._load("K-5", "P-10", "op-na")
        self._seal("K-5", "op-na")
        self.service.unload_parcel(request_id="unload-10", actor_id="op-na",
                                   container_id="K-5", parcel_id="P-10", reason="复查拆袋")
        lineage = self.service.container_lineage(actor_id="op-admin", container_id="K-5")
        self.assertEqual("open", lineage["container"]["state"])
        self.assertEqual("released", lineage["reservations"][0]["state"])
        self.assertEqual("cancelled", lineage["handovers"][0]["state"])

    # ------------------------------------------------------------------
    # 交接与不可重写
    # ------------------------------------------------------------------

    def test_handover_needs_three_parties_and_is_immutable(self):
        self._parcel("P-11")
        self._container("K-6", "S-AB", 2, "op-na")
        self._load("K-6", "P-11", "op-na")
        handover_id = self._seal("K-6", "op-na")["handover_id"]
        with self.assertRaises(PermissionDenied):
            self.service.confirm_handover(request_id="bad-1", actor_id="op-support",
                                          handover_id=handover_id)
        with self.assertRaises(PermissionDenied):
            self.service.confirm_handover(request_id="bad-2", actor_id="op-nb",
                                          handover_id=handover_id)
        pending = self.service.confirm_handover(request_id="ok-1", actor_id="op-na",
                                                handover_id=handover_id)
        self.assertFalse(pending["completed"])
        self.service.confirm_handover(request_id="ok-2", actor_id="op-c1",
                                      handover_id=handover_id)
        done = self.service.confirm_handover(request_id="ok-3", actor_id="op-compliance",
                                             handover_id=handover_id)
        self.assertTrue(done["completed"])
        with self.assertRaises(ConflictError):
            self.service.confirm_handover(request_id="ok-4", actor_id="op-compliance",
                                          handover_id=handover_id)

    def test_full_lifecycle_and_commitment_met(self):
        self._parcel("P-12")
        for container_id, segment, out_op, carrier_op, in_op in [
            ("K-7", "S-AB", "op-na", "op-c1", "op-nb"),
            ("K-8", "S-BC", "op-nb", "op-c1", "op-nc"),
            ("K-9", "S-CD", "op-nc", "op-c2", "op-nd"),
        ]:
            self._container(container_id, segment, 2, out_op)
            self._load(container_id, "P-12", out_op)
            done = self._dispatch(container_id, out_op, carrier_op)
            self.assertTrue(done["completed"])
            self._arrive(container_id, carrier_op, in_op)
            self.service.unload_parcel(request_id=f"unload-{container_id}", actor_id=in_op,
                                       container_id=container_id, parcel_id="P-12")
        delivered = self.service.deliver_parcel(request_id="deliver-12", actor_id="op-nd",
                                                parcel_id="P-12")
        self.assertEqual("met", delivered["outcome"])
        trace = self.service.parcel_trace(actor_id="op-admin", parcel_id="P-12")
        self.assertEqual("delivered", trace["parcel"]["state"])
        self.assertEqual("completed", trace["plans"][-1]["plan"]["state"])
        containers = sorted({row["container_id"] for row in trace["containers"]
                             if row["action"] == "loaded"})
        self.assertEqual(["K-7", "K-8", "K-9"], containers)

    def test_completed_parcel_cannot_be_rewritten(self):
        self._parcel("P-13")
        self._container("K-10", "S-AB", 2, "op-na")
        self._load("K-10", "P-13", "op-na")
        self._dispatch("K-10", "op-na", "op-c1")
        self._arrive("K-10", "op-c1", "op-nb")
        self.service.unload_parcel(request_id="unload-13", actor_id="op-nb",
                                   container_id="K-10", parcel_id="P-13")
        # 直接改道到目的节点再签收，缩短链路
        self.service.set_node_status(request_id="close-c-2", actor_id="op-compliance",
                                     node_id="N-C", status="closed", reason="关闭")
        self.service.set_node_status(request_id="close-e-2", actor_id="op-compliance",
                                     node_id="N-E", status="closed", reason="关闭")
        with self.assertRaises(ValidationError):
            # 包裹在 N-B，无路可走只能候补，不能签收
            self.service.deliver_parcel(request_id="deliver-13", actor_id="op-nd",
                                        parcel_id="P-13")
        # 恢复口岸并完成旅程
        self.service.set_node_status(request_id="open-c-2", actor_id="op-compliance",
                                     node_id="N-C", status="open", reason="恢复")
        self.service.set_node_status(request_id="open-e-2", actor_id="op-compliance",
                                     node_id="N-E", status="open", reason="恢复")
        for container_id, segment, out_op, carrier_op, in_op in [
            ("K-11", "S-BC", "op-nb", "op-c1", "op-nc"),
            ("K-12", "S-CD", "op-nc", "op-c2", "op-nd"),
        ]:
            self._container(container_id, segment, 2, out_op)
            self._load(container_id, "P-13", out_op)
            self._dispatch(container_id, out_op, carrier_op)
            self._arrive(container_id, carrier_op, in_op)
            self.service.unload_parcel(request_id=f"unload-{container_id}", actor_id=in_op,
                                       container_id=container_id, parcel_id="P-13")
        self.service.deliver_parcel(request_id="deliver-13-ok", actor_id="op-nd",
                                    parcel_id="P-13")
        with self.assertRaises(ValidationError):
            self.service.reroute_parcel(request_id="reroute-13", actor_id="op-nd",
                                        parcel_id="P-13", reason="已完成")
        with self.assertRaises(ConflictError):
            self.service.submit_declaration(request_id="decl-13", actor_id="op-nd",
                                            parcel_id="P-13", declared_value=50,
                                            currency="EUR", category="clothes")
        change = self.service.publish_rule(request_id="r-ban-v2", actor_id="op-compliance",
                                           rule_id="R-BAN", jurisdiction="DE",
                                           scope="destination", rule_type="prohibited_category",
                                           expression={"categories": ["weapons", "clothes"]})
        self.assertNotIn("P-13", change["affected"])
        trace = self.service.parcel_trace(actor_id="op-admin", parcel_id="P-13")
        self.assertFalse(any(h["state"] == "open" for h in trace["holds"]))

    # ------------------------------------------------------------------
    # 规则与申报版本的定向影响
    # ------------------------------------------------------------------

    def test_rule_change_hits_only_matching_incomplete_parcels(self):
        self._parcel("P-14", category="toys")
        self._parcel("P-15", category="clothes")
        change = self.service.publish_rule(request_id="r-ban-v3", actor_id="op-compliance",
                                           rule_id="R-BAN", jurisdiction="DE",
                                           scope="destination", rule_type="prohibited_category",
                                           expression={"categories": ["weapons", "toys"]})
        self.assertEqual(["P-14"], change["affected"])
        trace = self.service.parcel_trace(actor_id="op-admin", parcel_id="P-14")
        self.assertEqual(1, len([h for h in trace["holds"] if h["state"] == "open"]))
        self.assertEqual("rule:R-BAN", trace["holds"][0]["source"])
        trace15 = self.service.parcel_trace(actor_id="op-admin", parcel_id="P-15")
        self.assertEqual([], trace15["holds"])

    def test_declaration_update_releases_hold(self):
        self._parcel("P-16", category="toys")
        self.service.publish_rule(request_id="r-ban-v4", actor_id="op-compliance",
                                  rule_id="R-BAN", jurisdiction="DE", scope="destination",
                                  rule_type="prohibited_category",
                                  expression={"categories": ["weapons", "toys"]})
        result = self.service.submit_declaration(request_id="decl-16", actor_id="op-na",
                                                 parcel_id="P-16", declared_value=100,
                                                 currency="EUR", category="clothes")
        self.assertEqual(0, result["open_holds"])
        self.assertEqual(2, result["declaration_version"])
        trace = self.service.parcel_trace(actor_id="op-admin", parcel_id="P-16")
        self.assertEqual("released", trace["holds"][0]["state"])
        self.assertIsNotNone(self._active_legs("P-16"))

    # ------------------------------------------------------------------
    # 口岸关闭、候补与改道
    # ------------------------------------------------------------------

    def _drive_to_b(self, parcel_id, container_id="K-20"):
        self._parcel(parcel_id)
        self._container(container_id, "S-AB", 2, "op-na")
        self._load(container_id, parcel_id, "op-na")
        self._dispatch(container_id, "op-na", "op-c1")
        self._arrive(container_id, "op-c1", "op-nb")
        self.service.unload_parcel(request_id=f"unload-{parcel_id}", actor_id="op-nb",
                                   container_id=container_id, parcel_id=parcel_id)

    def test_port_close_impact_waitlist_and_reopen(self):
        self._drive_to_b("P-17")
        impact = self.service.set_node_status(request_id="close-c-3", actor_id="op-compliance",
                                              node_id="N-C", status="closed", reason="大风")
        parcels = {item["parcel_id"]: item for item in impact["affected"]
                   if item["type"] == "parcel"}
        self.assertEqual("waitlisted", parcels["P-17"]["action"])
        self.assertEqual("CM-1", parcels["P-17"]["commitment_id"])
        self.assertIsNotNone(impact["report_id"])
        reopened = self.service.set_node_status(request_id="open-c-3", actor_id="op-compliance",
                                                node_id="N-C", status="open", reason="恢复")
        self.assertEqual(["P-17"], reopened["promoted"])
        self.assertEqual(["S-BC", "S-CD"], self._active_legs("P-17"))
        node = self.service.node_impact(actor_id="op-admin", node_id="N-C")
        self.assertEqual(1, len(node["reports"]))
        self.assertEqual("node_closed", node["reports"][0]["trigger"])

    def test_segment_suspend_promotes_standby_route(self):
        self._parcel("P-18")
        result = self.service.set_segment_status(request_id="suspend-bc", actor_id="op-admin",
                                                 segment_id="S-BC", status="suspended")
        parcels = {item["parcel_id"]: item for item in result["affected"]
                   if item["type"] == "parcel"}
        self.assertEqual("planned", parcels["P-18"]["action"])
        self.assertEqual(["S-AE", "S-ED"], self._active_legs("P-18"))
        trace = self.service.parcel_trace(actor_id="op-admin", parcel_id="P-18")
        self.assertEqual("promoted", trace["standbys"][0]["state"])

    # ------------------------------------------------------------------
    # 查验
    # ------------------------------------------------------------------

    def test_inspection_holds_then_flag_or_release(self):
        self._parcel("P-19")
        self._parcel("P-20")
        self._container("K-21", "S-AB", 2, "op-na")
        self._load("K-21", "P-19", "op-na")
        self._load("K-21", "P-20", "op-na")
        self._dispatch("K-21", "op-na", "op-c1")
        self._arrive("K-21", "op-c1", "op-nb")
        opened = self.service.open_inspection(request_id="insp-1", actor_id="op-compliance",
                                              container_id="K-21", reason="抽检")
        self.assertEqual(["P-19", "P-20"], sorted(opened["held_parcels"]))
        with self.assertRaises(ConflictError):
            self.service.unload_parcel(request_id="bad-unload", actor_id="op-nb",
                                       container_id="K-21", parcel_id="P-19")
        closed = self.service.close_inspection(
            request_id="insp-1-close", actor_id="op-compliance", container_id="K-21",
            results=[{"parcel_id": "P-19", "outcome": "flag", "note": "申报不符"},
                     {"parcel_id": "P-20", "outcome": "pass"}])
        self.assertEqual(["P-19"], closed["flagged"])
        trace = self.service.parcel_trace(actor_id="op-admin", parcel_id="P-19")
        self.assertEqual("exception", trace["parcel"]["state"])
        parties = {(r["stage"], r["responsible_party"]) for r in trace["responsibility"]}
        self.assertIn(("inspection", "compliance@N-B"), parties)
        trace20 = self.service.parcel_trace(actor_id="op-admin", parcel_id="P-20")
        self.assertFalse(any(h["state"] == "open" for h in trace20["holds"]))

    # ------------------------------------------------------------------
    # 超时责任与重启一致性
    # ------------------------------------------------------------------

    def test_timeout_responsibility_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "postal.sqlite3"
            clock = MutableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
            database = Database(path)
            foundation = DomainService(database, clock)
            foundation.register_organization(request_id="org", actor_id="bootstrap",
                                             organization_id="org-1", name="哈萨克斯坦邮政")
            foundation.register_actor(request_id="fa", actor_id="bootstrap",
                                      new_actor_id="fa-admin", display_name="平台管理员",
                                      role="admin", organization_id="org-1")
            service = PostalService(database, clock)
            self._build_world_on(service)
            service.register_parcel(request_id="p-30", actor_id="op-na", parcel_id="P-30",
                                    origin_node="N-A", destination_node="N-D",
                                    category="clothes", declared_value=100, currency="EUR",
                                    weight_grams=500, commitment_id="CM-1")
            service.create_container(request_id="k-30", actor_id="op-na", container_id="K-30",
                                     segment_id="S-AB", capacity=2)
            service.load_parcel(request_id="load-30", actor_id="op-na",
                                container_id="K-30", parcel_id="P-30")
            sealed = service.seal_container(request_id="seal-30", actor_id="op-na",
                                            container_id="K-30")
            handover_id = sealed["handover_id"]
            for suffix, actor in [("n", "op-na"), ("c", "op-c1"), ("g", "op-compliance")]:
                service.confirm_handover(request_id=f"{handover_id}-{suffix}",
                                         actor_id=actor, handover_id=handover_id)
            clock.advance(300)
            scan = service.evaluate_timeouts(request_id="scan-30", actor_id="op-admin")
            self.assertEqual(1, scan["records_opened"])
            database.close()

            database = Database(path)
            service = PostalService(database, clock)
            lineage = service.container_lineage(actor_id="op-admin", container_id="K-30")
            self.assertEqual("in_transit", lineage["container"]["state"])
            trace = service.parcel_trace(actor_id="op-admin", parcel_id="P-30")
            open_records = [r for r in trace["responsibility"] if r["ended_at"] is None]
            self.assertEqual(1, len(open_records))
            self.assertEqual("carrier_transit", open_records[0]["stage"])
            self.assertEqual("carrier:C-1", open_records[0]["responsible_party"])
            replay = service.seal_container(request_id="seal-30", actor_id="op-na",
                                            container_id="K-30")
            self.assertTrue(replay["replayed"])
            again = service.evaluate_timeouts(request_id="scan-31", actor_id="op-admin")
            self.assertEqual(0, again["records_opened"])
            arrived = service.arrive_container(request_id="arr-30", actor_id="op-c1",
                                               container_id="K-30")
            service.confirm_handover(request_id="arr-30-in", actor_id="op-nb",
                                     handover_id=arrived["handover_id"])
            service.unload_parcel(request_id="unload-30", actor_id="op-nb",
                                  container_id="K-30", parcel_id="P-30")
            # 直接完成剩余旅程
            for container_id, segment, out_op, carrier_op, in_op in [
                ("K-31", "S-BC", "op-nb", "op-c1", "op-nc"),
                ("K-32", "S-CD", "op-nc", "op-c2", "op-nd"),
            ]:
                service.create_container(request_id=f"ct-{container_id}", actor_id=out_op,
                                         container_id=container_id, segment_id=segment,
                                         capacity=2)
                service.load_parcel(request_id=f"load-{container_id}", actor_id=out_op,
                                    container_id=container_id, parcel_id="P-30")
                seal = service.seal_container(request_id=f"seal-{container_id}",
                                              actor_id=out_op, container_id=container_id)
                hid = seal["handover_id"]
                for suffix, actor in [("n", out_op), ("c", carrier_op), ("g", "op-compliance")]:
                    service.confirm_handover(request_id=f"{hid}-{suffix}", actor_id=actor,
                                             handover_id=hid)
                arr = service.arrive_container(request_id=f"arr-{container_id}",
                                               actor_id=carrier_op, container_id=container_id)
                service.confirm_handover(request_id=f"{arr['handover_id']}-in", actor_id=in_op,
                                         handover_id=arr["handover_id"])
                service.unload_parcel(request_id=f"unload-{container_id}", actor_id=in_op,
                                      container_id=container_id, parcel_id="P-30")
            delivered = service.deliver_parcel(request_id="deliver-30", actor_id="op-nd",
                                               parcel_id="P-30")
            self.assertEqual("breached", delivered["outcome"])
            trace = service.parcel_trace(actor_id="op-admin", parcel_id="P-30")
            self.assertTrue(all(r["ended_at"] is not None for r in trace["responsibility"]))
            database.close()

    def _build_world_on(self, service):
        saved = self.service
        self.service = service
        try:
            self._build_world()
        finally:
            self.service = saved

    # ------------------------------------------------------------------
    # 客户支持披露
    # ------------------------------------------------------------------

    def test_support_view_is_disclosable_only(self):
        self._parcel("P-40")
        view = self.service.support_view(actor_id="op-support", parcel_id="P-40")
        self.assertEqual({"parcel_id", "status_code", "status_text", "location",
                          "updated_at", "commitment"}, set(view))
        self.assertEqual("processing", view["status_code"])
        with self.assertRaises(PermissionDenied):
            self.service.parcel_trace(actor_id="op-support", parcel_id="P-40")
        with self.assertRaises(PermissionDenied):
            self.service.container_lineage(actor_id="op-support", container_id="K-x")
        with self.assertRaises(PermissionDenied):
            self.service.node_impact(actor_id="op-support", node_id="N-A")

    def test_support_view_reflects_inspection_and_waitlist(self):
        self._parcel("P-41")
        self._container("K-41", "S-AB", 2, "op-na")
        self._load("K-41", "P-41", "op-na")
        self._seal("K-41", "op-na")
        self.service.open_inspection(request_id="insp-41", actor_id="op-compliance",
                                     container_id="K-41", reason="抽检")
        view = self.service.support_view(actor_id="op-support", parcel_id="P-41")
        self.assertEqual("customs_inspection", view["status_code"])
        self.assertEqual("口岸查验中", view["status_text"])

    def test_request_id_rejects_changed_payload(self):
        self._parcel("P-42", request_id="same-request")
        with self.assertRaises(ConflictError):
            self.service.register_parcel(request_id="same-request", actor_id="op-na",
                                         parcel_id="P-43", origin_node="N-A",
                                         destination_node="N-D", category="clothes",
                                         declared_value=100, currency="EUR",
                                         weight_grams=500, commitment_id="CM-1")


if __name__ == "__main__":
    unittest.main()
