import unittest

from postal_routing.acceptance import run


class PostalAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"], result["checks"])
        self.assertTrue(result["checks"]["audit_valid"])
        self.assertTrue(result["checks"]["restart_in_transit"])
        self.assertTrue(result["checks"]["replay_no_double_reserve"])


if __name__ == "__main__":
    unittest.main()
