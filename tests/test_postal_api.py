import unittest
from datetime import datetime, timezone

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database
from postal_routing.api import route
from postal_routing.service import PostalService


class PostalApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, clock)
        self.service = PostalService(self.database, self.foundation, clock)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="哈萨克斯坦邮政")
        self.foundation.register_actor(request_id="a1", actor_id="bootstrap",
                                       new_actor_id="admin", display_name="管理员",
                                       role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="a2", actor_id="admin",
                                       new_actor_id="support", display_name="客服",
                                       role="support", organization_id="o1")
        self.service.register_commitment(request_id="cmt", actor_id="admin",
                                         commitment_id="c48", description="四十八小时",
                                         promised_hours=48)
        self.service.register_gateway(request_id="gw1", actor_id="admin", gateway_id="gw-ala",
                                      name="阿拉木图", country="KZ", region="KZ-SE")
        self.service.register_gateway(request_id="gw2", actor_id="admin", gateway_id="gw-fra",
                                      name="法兰克福", country="DE", region="DE-HE")
        self.service.register_leg(request_id="leg1", actor_id="admin", leg_id="leg-direct",
                                  from_gateway="gw-ala", to_gateway="gw-fra",
                                  carrier_id="kz-air", capacity=3, priority=1)
        self.service.register_parcel(request_id="p1", actor_id="admin", parcel_id="p-001",
                                     origin_region="KZ-SE", destination_country="DE",
                                     weight_grams=500, commitment_id="c48",
                                     declared_value_minor=1000, currency="KZT",
                                     item_category="general")

    def tearDown(self):
        self.database.close()

    def _post(self, path, body, actor="admin"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def test_health(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_generate_plan_and_replay_over_http(self):
        status, payload = self._post("/postal/plans", {"request_id": "plan-1",
                                                       "parcel_id": "p-001"})
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        self.assertEqual("planned", payload["result"]["result"])
        status, payload = self._post("/postal/plans", {"request_id": "plan-1",
                                                       "parcel_id": "p-001"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

    def test_trace_and_support_view(self):
        self._post("/postal/plans", {"request_id": "plan-1", "parcel_id": "p-001"})
        status, payload = route(self.service, "GET", "/postal/parcels/p-001/trace", None,
                                {"X-Actor-Id": "admin"})
        self.assertEqual(200, status)
        self.assertEqual("p-001", payload["parcel"]["parcel_id"])
        self.assertEqual(1, len(payload["plans"]))
        status, payload = route(self.service, "GET", "/postal/parcels/p-001/support-view",
                                None, {"X-Actor-Id": "support"})
        self.assertEqual(200, status)
        self.assertEqual("已收寄，等待发运", payload["status"])
        status, payload = route(self.service, "GET", "/postal/parcels/p-001/trace", None,
                                {"X-Actor-Id": "support"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_gateway_impact_route(self):
        status, payload = route(self.service, "GET", "/postal/gateways/gw-ala/impact", None,
                                {"X-Actor-Id": "admin"})
        self.assertEqual(200, status)
        self.assertEqual("gw-ala", payload["gateway"]["gateway_id"])

    def test_unknown_and_invalid_requests(self):
        status, payload = route(self.service, "GET", "/postal/unknown", None)
        self.assertEqual(404, status)
        status, payload = self._post("/postal/plans", {"request_id": "x"})
        self.assertEqual(400, status)
        status, payload = self._post("/postal/plans", {"request_id": "y", "parcel_id": "p-001"},
                                     actor="ghost")
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_leg_capacity_route(self):
        self._post("/postal/plans", {"request_id": "plan-1", "parcel_id": "p-001"})
        status, payload = route(self.service, "GET", "/postal/legs", None,
                                {"X-Actor-Id": "admin"})
        self.assertEqual(200, status)
        direct = next(leg for leg in payload["legs"] if leg["leg_id"] == "leg-direct")
        self.assertEqual(1, direct["reserved"])
        self.assertEqual(2, direct["remaining"])


if __name__ == "__main__":
    unittest.main()
