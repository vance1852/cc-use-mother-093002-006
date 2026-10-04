import unittest

from postal_orchestration.acceptance import run


class PostalAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual(3, result["parcels_delivered"])
        self.assertEqual(1, result["commitment_met"])
        self.assertEqual(2, result["commitment_breached"])
        self.assertTrue(result["register_replayed"])
        self.assertTrue(result["seal_replayed"])
        self.assertEqual(2, result["restart_checks"])
        self.assertEqual(2, result["waitlist_promoted"])
        self.assertEqual(["P-001"], result["rule_change_affected"])
        self.assertEqual("已签收", result["support_status"])


if __name__ == "__main__":
    unittest.main()
