import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from digital_trade_foundation.errors import ConflictError, PermissionDenied, ValidationError
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database
from postal_routing.service import PostalService


class MutableClock:
    def __init__(self, start):
        self._value = start

    def now(self):
        return self._value

    def advance(self, **kwargs):
        self._value = self._value + timedelta(**kwargs)


class PostalServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, self.clock)
        self.postal = PostalService(self.database, self.foundation, self.clock)
        self._bootstrap_identities()

    def _bootstrap_identities(self):
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="哈萨克斯坦邮政")
        for request_id, actor_id, role in (
                ("a-admin", "admin", "admin"), ("a-node", "node", "node"),
                ("a-carrier", "carrier", "carrier"), ("a-comp", "comp", "compliance"),
                ("a-support", "support", "support")):
            self.foundation.register_actor(request_id=request_id, actor_id="bootstrap"
                                           if actor_id == "admin" else "admin",
                                           new_actor_id=actor_id, display_name=actor_id,
                                           role=role, organization_id="o1")
        self.postal.register_commitment(request_id="cmt", actor_id="admin",
                                        commitment_id="c48", description="四十八小时",
                                        promised_hours=48)
        self._bootstrap_network()

    def _bootstrap_network(self):
        for gateway_id, country, region in (("gw-ala", "KZ", "KZ-SE"),
                                            ("gw-urc", "CN", "CN-XJ"),
                                            ("gw-svo", "RU", "RU-MOW"),
                                            ("gw-fra", "DE", "DE-HE")):
            self.postal.register_gateway(request_id=f"gw-{gateway_id}", actor_id="admin",
                                         gateway_id=gateway_id, name=gateway_id,
                                         country=country, region=region)
        for leg_id, src, dst, capacity, priority in (
                ("leg-direct", "gw-ala", "gw-fra", 2, 1),
                ("leg-ala-urc", "gw-ala", "gw-urc", 5, 1),
                ("leg-urc-fra", "gw-urc", "gw-fra", 5, 2),
                ("leg-ala-svo", "gw-ala", "gw-svo", 5, 3),
                ("leg-svo-fra", "gw-svo", "gw-fra", 5, 4)):
            self.postal.register_leg(request_id=f"leg-{leg_id}", actor_id="admin",
                                     leg_id=leg_id, from_gateway=src, to_gateway=dst,
                                     carrier_id="kz-air", capacity=capacity, priority=priority)

    def tearDown(self):
        self.database.close()

    def _parcel(self, parcel_id, category="general", value=1000, proofs=None, request_id=None):
        self.postal.register_parcel(
            request_id=request_id or f"rp-{parcel_id}", actor_id="node", parcel_id=parcel_id,
            origin_region="KZ-SE", destination_country="DE", weight_grams=500,
            commitment_id="c48", declared_value_minor=value, currency="KZT",
            item_category=category, proofs=proofs)

    def _plan(self, parcel_id, request_id=None):
        return self.postal.generate_plan(request_id=request_id or f"plan-{parcel_id}",
                                         actor_id="admin", parcel_id=parcel_id)

    def _ship_bag(self, bag_id, parcel_ids, leg_id="leg-direct"):
        self.postal.register_container(request_id=f"bag-{bag_id}", actor_id="node",
                                       container_id=bag_id, capacity=10, gateway_id="gw-ala")
        self.postal.consolidate(request_id=f"con-{bag_id}", actor_id="node",
                                container_id=bag_id, parcel_ids=parcel_ids)
        self.postal.seal_container(request_id=f"seal-{bag_id}", actor_id="node",
                                   container_id=bag_id)
        return self.postal.dispatch_container(request_id=f"disp-{bag_id}", actor_id="node",
                                              container_id=bag_id, leg_id=leg_id)

    # ------------------------------------------------------------------
    def test_plan_carries_reasons_and_stable_backups(self):
        self._parcel("p1")
        _, response = self._plan("p1")
        self.assertEqual("planned", response["result"])
        codes = [reason["code"] for reason in response["reasons"]]
        self.assertIn("capacity_reserved", codes)
        self.assertIn("backup_route", codes)
        backups = [r for r in response["reasons"] if r["code"] == "backup_route"]
        self.assertEqual([1, 2], [b["rank"] for b in backups])
        self.assertEqual(["leg-ala-urc", "leg-urc-fra"], backups[0]["legs"])
        self.assertEqual(["leg-ala-svo", "leg-svo-fra"], backups[1]["legs"])

    def test_prohibited_category_is_held_with_rule_reason(self):
        self.postal.publish_rule(request_id="rule1", actor_id="comp", rule_id="r-batt",
                                 jurisdiction="DE", rule_scope="destination",
                                 rule_type="prohibited_category",
                                 selector={"item_category": "battery"}, constraint={})
        self._parcel("p-batt", category="battery")
        _, response = self._plan("p-batt")
        self.assertEqual("held", response["result"])
        blocked = [r for r in response["reasons"] if r["code"] == "rule_blocked"]
        self.assertTrue(blocked)
        self.assertEqual("r-batt", blocked[0]["rule_id"])
        trace = self.postal.parcel_trace(actor_id="admin", parcel_id="p-batt")
        self.assertEqual("held", trace["parcel"]["status"])
        self.assertEqual("gw-ala", trace["responsibilities"][0]["party_id"])

    def test_missing_proof_holds_until_declaration_version_supplies_it(self):
        self.postal.publish_rule(request_id="rule2", actor_id="comp", rule_id="r-msds",
                                 jurisdiction="CN", rule_scope="transit",
                                 rule_type="proof_required",
                                 selector={"item_category": "cosmetics"},
                                 constraint={"proof_type": "msds"})
        # 直飞不经过 CN，可以成行
        self._parcel("p-cos", category="cosmetics")
        _, response = self._plan("p-cos")
        self.assertEqual("planned", response["result"])
        # 直飞停运后改道莫斯科（不经过 CN，仍无需证明）
        self.postal.set_leg_status(request_id="suspend-direct", actor_id="admin",
                                   leg_id="leg-direct", status="suspended")
        trace = self.postal.parcel_trace(actor_id="admin", parcel_id="p-cos")
        self.assertEqual("planned", trace["parcel"]["status"])
        # 莫斯科航线也停运后只剩经 CN 的路线，缺少 msds 证明被暂存
        self.postal.set_leg_status(request_id="suspend-svo", actor_id="admin",
                                   leg_id="leg-ala-svo", status="suspended")
        trace = self.postal.parcel_trace(actor_id="admin", parcel_id="p-cos")
        self.assertEqual("held", trace["parcel"]["status"])
        # 补充证明版本后重新生成方案即可成行
        self.postal.add_declaration_version(
            request_id="decl-2", actor_id="node", parcel_id="p-cos",
            declared_value_minor=1000, currency="KZT", item_category="cosmetics",
            proofs=[{"type": "msds", "reference": "M-1"}])
        _, response = self._plan("p-cos", request_id="plan-p-cos-2")
        self.assertEqual("planned", response["result"])
        reserved = [r["leg_id"] for r in response["reasons"] if r["code"] == "capacity_reserved"]
        self.assertEqual(["leg-ala-urc", "leg-urc-fra"], reserved)

    def test_replay_does_not_double_reserve_capacity(self):
        self._parcel("p1")
        first, _ = self._plan("p1")
        replay, _ = self._plan("p1")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        legs = self.postal.leg_capacity_view(actor_id="admin")["legs"]
        direct = next(leg for leg in legs if leg["leg_id"] == "leg-direct")
        self.assertEqual(1, direct["reserved"])

    def test_capacity_overflow_uses_backup_route(self):
        for pid in ("p1", "p2"):
            self._parcel(pid)
            self._plan(pid)
        self._parcel("p3")
        _, response = self._plan("p3")
        reserved = [r["leg_id"] for r in response["reasons"] if r["code"] == "capacity_reserved"]
        self.assertEqual(["leg-ala-urc", "leg-urc-fra"], reserved)

    def test_consolidation_rejects_incompatible_categories(self):
        self.postal.publish_rule(request_id="rule3", actor_id="comp", rule_id="r-cobag",
                                 jurisdiction="*", rule_scope="transit",
                                 rule_type="co_bag_restriction",
                                 selector={"item_category": "food"},
                                 constraint={"incompatible_categories": ["chemicals"]})
        self._parcel("p-food", category="food")
        self._parcel("p-chem", category="chemicals")
        self._plan("p-food")
        self._plan("p-chem")
        self.postal.register_container(request_id="bag-x", actor_id="node",
                                       container_id="bag-x", capacity=10, gateway_id="gw-ala")
        with self.assertRaises(ConflictError):
            self.postal.consolidate(request_id="con-x", actor_id="node",
                                    container_id="bag-x", parcel_ids=["p-food", "p-chem"])

    def test_consolidation_rejects_dedicated_container_rule(self):
        self.postal.publish_rule(request_id="rule4", actor_id="comp", rule_id="r-value",
                                 jurisdiction="DE", rule_scope="destination",
                                 rule_type="value_threshold",
                                 selector={"min_value_minor": 150000},
                                 constraint={"requires_proof": "invoice",
                                             "dedicated_container": True})
        self._parcel("p-rich", value=200000, proofs=[{"type": "invoice"}])
        self._parcel("p-plain")
        self._plan("p-rich")
        self._plan("p-plain")
        self.postal.register_container(request_id="bag-y", actor_id="node",
                                       container_id="bag-y", capacity=10, gateway_id="gw-ala")
        with self.assertRaises(ConflictError):
            self.postal.consolidate(request_id="con-y", actor_id="node",
                                    container_id="bag-y", parcel_ids=["p-rich", "p-plain"])

    def test_dispatch_and_receive_record_party_confirmations(self):
        self._parcel("p1")
        self._plan("p1")
        _, dispatched = self._ship_bag("bag-1", ["p1"])
        handover_id = dispatched["handover_id"]
        self.postal.receive_container(request_id="recv-1", actor_id="carrier",
                                      container_id="bag-1")
        trace = self.postal.parcel_trace(actor_id="admin", parcel_id="p1")
        confirmations = {(c["party"], c["resource_id"]) for c in trace["confirmations"]}
        self.assertIn(("node", handover_id), confirmations)
        self.assertIn(("carrier", handover_id), confirmations)

    def test_inspection_opens_node_responsibility_and_release_resolves(self):
        self._parcel("p1")
        self._plan("p1")
        self._ship_bag("bag-1", ["p1"])
        self.postal.receive_container(request_id="recv-1", actor_id="carrier",
                                      container_id="bag-1")
        self.postal.start_inspection(request_id="insp-1", actor_id="comp",
                                     container_id="bag-1", reason="布控")
        view = self.postal.container_view(actor_id="admin", container_id="bag-1")
        self.assertEqual("inspection", view["container"]["state"])
        self.assertEqual("inspection", view["items"][0]["status"])
        self.postal.release_parcel(request_id="rel-1", actor_id="comp",
                                   container_id="bag-1", parcel_id="p1", decision="released")
        trace = self.postal.parcel_trace(actor_id="admin", parcel_id="p1")
        responsibility = trace["responsibilities"][0]
        self.assertEqual("gw-fra", responsibility["party_id"])
        self.assertEqual("node", responsibility["party_type"])
        self.assertEqual("resolved", responsibility["status"])

    def test_seized_parcel_cancels_plan_and_releases_capacity(self):
        self._parcel("p1")
        self._plan("p1")
        self._ship_bag("bag-1", ["p1"])
        self.postal.receive_container(request_id="recv-1", actor_id="carrier",
                                      container_id="bag-1")
        self.postal.start_inspection(request_id="insp-1", actor_id="comp",
                                     container_id="bag-1", reason="布控")
        self.postal.release_parcel(request_id="rel-1", actor_id="comp",
                                   container_id="bag-1", parcel_id="p1", decision="seized")
        trace = self.postal.parcel_trace(actor_id="admin", parcel_id="p1")
        self.assertEqual("held", trace["parcel"]["status"])
        self.assertEqual("cancelled", trace["plans"][0]["status"])

    def test_gateway_closure_only_reroutes_parcels_whose_remaining_path_hits_it(self):
        self._parcel("p-direct")
        self._parcel("p-via-urc")
        self._plan("p-direct")
        self._plan("p-via-urc")
        # p-via-urc 占满直飞后的第二个包裹？直飞容量 2，这里两个都在直飞
        self._parcel("p-third")
        self._plan("p-third")  # 直飞已满，经乌鲁木齐
        _, response = self.postal.set_gateway_status(request_id="close-urc", actor_id="admin",
                                                     gateway_id="gw-urc", status="closed")
        self.assertEqual([{"parcel_id": "p-third", "result": "rerouted",
                           "plan_id": "plan-p-third-v2"}], response["affected"])
        trace = self.postal.parcel_trace(actor_id="admin", parcel_id="p-third")
        active = [p for p in trace["plans"] if p["status"] == "active"][0]
        self.assertEqual(["leg-ala-svo", "leg-svo-fra"], active["legs"])
        # 未命中乌鲁木齐的包裹方案保持不变
        trace_direct = self.postal.parcel_trace(actor_id="admin", parcel_id="p-direct")
        self.assertEqual(1, len(trace_direct["plans"]))

    def test_completed_parcels_are_untouched_by_rule_and_gateway_changes(self):
        self._parcel("p1")
        self._plan("p1")
        self._ship_bag("bag-1", ["p1"])
        self.postal.receive_container(request_id="recv-1", actor_id="carrier",
                                      container_id="bag-1")
        self.postal.deconsolidate(request_id="dec-1", actor_id="node",
                                  container_id="bag-1", parcel_ids=["p1"])
        self.postal.deliver_parcel(request_id="dlv-1", actor_id="node", parcel_id="p1")
        _, response = self.postal.set_gateway_status(request_id="close-ala", actor_id="admin",
                                                     gateway_id="gw-ala", status="closed")
        self.assertEqual([], response["affected"])
        with self.assertRaises(ConflictError):
            self.postal.add_declaration_version(request_id="late", actor_id="node",
                                                parcel_id="p1", declared_value_minor=1,
                                                currency="KZT", item_category="general")
        trace = self.postal.parcel_trace(actor_id="admin", parcel_id="p1")
        self.assertEqual("delivered", trace["parcel"]["status"])
        self.assertEqual("completed", trace["plans"][0]["status"])

    def test_leg_capacity_reduction_bumps_newest_reservation(self):
        for pid in ("p1", "p2"):
            self._parcel(pid)
            self._plan(pid)
        _, response = self.postal.set_leg_status(request_id="reduce", actor_id="admin",
                                                 leg_id="leg-direct", status="reduced",
                                                 capacity=1)
        self.assertEqual([{"parcel_id": "p2", "result": "rerouted",
                           "plan_id": "plan-p2-v2"}], response["affected"])
        legs = self.postal.leg_capacity_view(actor_id="admin")["legs"]
        direct = next(leg for leg in legs if leg["leg_id"] == "leg-direct")
        self.assertEqual(1, direct["reserved"])

    def test_rule_change_only_hits_matching_unfinished_parcels(self):
        self._parcel("p-general")
        self._parcel("p-device", category="electronics")
        self._plan("p-general")
        self._plan("p-device")
        _, response = self.postal.publish_rule(
            request_id="rule5", actor_id="comp", rule_id="r-elec", jurisdiction="DE",
            rule_scope="destination", rule_type="prohibited_category",
            selector={"item_category": "electronics"}, constraint={})
        self.assertEqual([{"parcel_id": "p-device", "result": "held"}], response["affected"])
        trace = self.postal.parcel_trace(actor_id="admin", parcel_id="p-general")
        self.assertEqual(1, len(trace["plans"]))
        self.assertEqual("planned", trace["parcel"]["status"])

    def test_completed_handover_is_not_rewritten_by_later_rule(self):
        self._parcel("p1", category="electronics")
        self._plan("p1")
        self._ship_bag("bag-1", ["p1"])
        self.postal.receive_container(request_id="recv-1", actor_id="carrier",
                                      container_id="bag-1")
        self.postal.publish_rule(request_id="rule6", actor_id="comp", rule_id="r-elec",
                                 jurisdiction="DE", rule_scope="destination",
                                 rule_type="prohibited_category",
                                 selector={"item_category": "electronics"}, constraint={})
        trace = self.postal.parcel_trace(actor_id="admin", parcel_id="p1")
        actions = [event["action"] for event in trace["lineage"]]
        self.assertIn("departed", actions)
        self.assertIn("arrived", actions)
        self.assertEqual("held", trace["parcel"]["status"])
        consumed = self.postal.leg_capacity_view(actor_id="admin")["legs"]
        direct = next(leg for leg in consumed if leg["leg_id"] == "leg-direct")
        self.assertEqual(1, direct["reserved"])  # 已消耗的交接不回滚

    def test_timeout_scan_assigns_carrier_when_in_transit(self):
        self._parcel("p1")
        self._plan("p1")
        self._ship_bag("bag-1", ["p1"])
        self.clock.advance(hours=49)
        _, scan = self.postal.scan_timeouts(request_id="scan-1", actor_id="admin")
        self.assertEqual([{"parcel_id": "p1", "party_type": "carrier",
                           "party_id": "kz-air"}], scan["opened"])
        _, again = self.postal.scan_timeouts(request_id="scan-2", actor_id="admin")
        self.assertEqual([], again["opened"])

    def test_support_only_sees_disclosable_view(self):
        self._parcel("p1")
        self._plan("p1")
        view = self.postal.support_view(actor_id="support", parcel_id="p1")
        self.assertEqual("已收寄，等待发运", view["status"])
        self.assertNotIn("plans", view)
        with self.assertRaises(PermissionDenied):
            self.postal.parcel_trace(actor_id="support", parcel_id="p1")
        with self.assertRaises(PermissionDenied):
            self.postal.gateway_impact(actor_id="support", gateway_id="gw-ala")

    def test_confirmation_is_unique_per_party(self):
        self.postal.confirm(request_id="cf-1", actor_id="node", resource_type="handover",
                            resource_id="hh-1", decision="confirmed")
        self.postal.confirm(request_id="cf-2", actor_id="carrier", resource_type="handover",
                            resource_id="hh-1", decision="confirmed")
        with self.assertRaises(ConflictError):
            self.postal.confirm(request_id="cf-3", actor_id="node", resource_type="handover",
                                resource_id="hh-1", decision="rejected")
        with self.assertRaises(PermissionDenied):
            self.postal.confirm(request_id="cf-4", actor_id="support", resource_type="handover",
                                resource_id="hh-1", decision="confirmed")

    def test_state_survives_restart(self):
        self.database.close()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "restart.sqlite3"
        self.database = Database(path)
        self.foundation = DomainService(self.database, self.clock)
        self.postal = PostalService(self.database, self.foundation, self.clock)
        self._bootstrap_identities()
        self._parcel("p1")
        self._plan("p1")
        self._ship_bag("bag-1", ["p1"])
        self.database.close()
        self.database = Database(path)
        self.foundation = DomainService(self.database, self.clock)
        self.postal = PostalService(self.database, self.foundation, self.clock)
        view = self.postal.container_view(actor_id="admin", container_id="bag-1")
        self.assertEqual("in_transit", view["container"]["state"])
        self.assertEqual(["p1"], [item["parcel_id"] for item in view["items"]])
        trace = self.postal.parcel_trace(actor_id="admin", parcel_id="p1")
        self.assertEqual(2, len(trace["plans"][0]["alternatives"]))
        self.postal.receive_container(request_id="recv-1", actor_id="carrier",
                                      container_id="bag-1")
        self.assertEqual("arrived", self.postal.container_view(
            actor_id="admin", container_id="bag-1")["container"]["state"])

    def test_gateway_impact_lists_commitments_and_reroute_results(self):
        self._parcel("p1")
        self._parcel("p2")
        self._plan("p1")
        self._plan("p2")
        self._parcel("p3")
        self._plan("p3")
        self.postal.set_gateway_status(request_id="close-urc", actor_id="admin",
                                       gateway_id="gw-urc", status="closed")
        impact = self.postal.gateway_impact(actor_id="admin", gateway_id="gw-svo")
        self.assertEqual(["p3"], [p["parcel_id"] for p in impact["affected_parcels"]])
        self.assertEqual("rerouted", impact["affected_parcels"][0]["last_outcome"]["action"])
        self.assertEqual("c48", impact["affected_commitments"][0]["commitment_id"])
        self.assertEqual(1, impact["affected_commitments"][0]["at_risk_parcels"])

    def test_validation_errors(self):
        with self.assertRaises(ValidationError):
            self.postal.register_gateway(request_id="bad", actor_id="admin", gateway_id="gw x",
                                         name="x", country="KZ", region="KZ")
        with self.assertRaises(ValidationError):
            self.postal.set_gateway_status(request_id="bad2", actor_id="admin",
                                           gateway_id="gw-ala", status="unknown")


if __name__ == "__main__":
    unittest.main()
