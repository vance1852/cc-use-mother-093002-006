import unittest

from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database
from postal_orchestration.api import route
from postal_orchestration.service import PostalService


class PostalApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        foundation = DomainService(self.database)
        foundation.register_organization(request_id="org", actor_id="bootstrap",
                                         organization_id="org-1", name="哈萨克斯坦邮政")
        foundation.register_actor(request_id="fa", actor_id="bootstrap", new_actor_id="fa-admin",
                                  display_name="平台管理员", role="admin", organization_id="org-1")
        self.service = PostalService(self.database)
        self.service.register_operator(request_id="op-a", actor_id="fa-admin",
                                       operator_id="op-admin", display_name="管理员",
                                       role="admin")
        self.service.register_operator(request_id="op-s", actor_id="fa-admin",
                                       operator_id="op-support", display_name="客服",
                                       role="support")

    def tearDown(self):
        self.database.close()

    def _post(self, path, body, actor="op-admin"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def test_register_node_and_replay(self):
        body = {"request_id": "n-1", "node_id": "N-1", "name": "阿拉山口口岸",
                "jurisdiction": "KZ", "kind": "port"}
        status, payload = self._post("/postal/nodes", body)
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status, payload = self._post("/postal/nodes", body)
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/postal/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_permission_denied_maps_to_403(self):
        status, payload = self._post("/postal/nodes",
                                     {"request_id": "n-2", "node_id": "N-2", "name": "口岸",
                                      "jurisdiction": "KZ", "kind": "port"},
                                     actor="op-support")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_missing_field_maps_to_400(self):
        status, payload = self._post("/postal/nodes", {"request_id": "n-3"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_support_view_endpoint(self):
        self._post("/postal/nodes", {"request_id": "n-4", "node_id": "N-4", "name": "口岸甲",
                                     "jurisdiction": "KZ", "kind": "port"})
        self._post("/postal/nodes", {"request_id": "n-5", "node_id": "N-5", "name": "口岸乙",
                                     "jurisdiction": "DE", "kind": "office"})
        self._post("/postal/commitments", {"request_id": "cm", "commitment_id": "CM-1",
                                           "product": "标准达", "max_transit_hours": 240})
        self.service.register_operator(request_id="op-n4", actor_id="fa-admin",
                                       operator_id="op-n4", display_name="节点操作员",
                                       role="node", node_id="N-4")
        status, payload = self._post("/postal/parcels",
                                     {"request_id": "p-1", "parcel_id": "P-1",
                                      "origin_node": "N-4", "destination_node": "N-5",
                                      "category": "clothes", "declared_value": 10,
                                      "currency": "EUR", "weight_grams": 100,
                                      "commitment_id": "CM-1"}, actor="op-n4")
        self.assertEqual(201, status)
        status, view = route(self.service, "GET",
                             "/postal/parcels/support-view?parcel_id=P-1", None,
                             {"X-Actor-Id": "op-support"})
        self.assertEqual(200, status)
        self.assertEqual("P-1", view["parcel_id"])
        status, payload = route(self.service, "GET",
                                "/postal/parcels/trace?parcel_id=P-1", None,
                                {"X-Actor-Id": "op-support"})
        self.assertEqual(403, status)


if __name__ == "__main__":
    unittest.main()
