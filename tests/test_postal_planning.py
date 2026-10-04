import unittest

from postal_orchestration.planning import (
    ParcelFacts,
    RuleInfo,
    SegmentInfo,
    assess_path,
    enumerate_paths,
    evaluate_rule,
    plan_routes,
)


def _facts(**overrides):
    values = dict(parcel_id="P", category="clothes", declared_value=100.0, currency="EUR",
                  proof_kinds=frozenset(), origin_node="A", destination_node="D")
    values.update(overrides)
    return ParcelFacts(**values)


class EvaluateRuleTest(unittest.TestCase):
    def test_prohibited_category(self):
        rule = RuleInfo("R1", 1, "DE", "destination", "prohibited_category",
                        {"categories": ["weapons"]})
        self.assertEqual("violation", evaluate_rule(rule, _facts(category="weapons"),
                                                    "destination")[0])
        self.assertEqual("pass", evaluate_rule(rule, _facts(), "destination")[0])
        self.assertEqual("not_applicable", evaluate_rule(rule, _facts(category="weapons"),
                                                         "transit")[0])

    def test_value_cap_checks_currency_and_amount(self):
        rule = RuleInfo("R2", 1, "DE", "destination", "value_cap",
                        {"max_value": 150, "currency": "EUR"})
        self.assertEqual("violation", evaluate_rule(rule, _facts(declared_value=200.0),
                                                    "destination")[0])
        self.assertEqual("pass", evaluate_rule(rule, _facts(declared_value=150.0),
                                               "destination")[0])
        self.assertEqual("not_applicable",
                         evaluate_rule(rule, _facts(currency="USD"), "destination")[0])

    def test_requires_proof_with_category_filter(self):
        rule = RuleInfo("R3", 1, "CN", "transit", "requires_proof",
                        {"proof_kind": "transit_permit", "categories": ["cosmetics"]})
        self.assertEqual("not_applicable", evaluate_rule(rule, _facts(), "transit")[0])
        self.assertEqual("violation",
                         evaluate_rule(rule, _facts(category="cosmetics"), "transit")[0])
        self.assertEqual("pass",
                         evaluate_rule(rule, _facts(category="cosmetics",
                                                    proof_kinds=frozenset({"transit_permit"})),
                                       "transit")[0])


class PathPlanningTest(unittest.TestCase):
    def setUp(self):
        self.segments = [
            SegmentInfo("S-AB", "A", "B", "C-1", "rail", 10, 1, "active"),
            SegmentInfo("S-BD", "B", "D", "C-1", "rail", 10, 1, "active"),
            SegmentInfo("S-AD", "A", "D", "C-2", "air", 25, 2, "active"),
            SegmentInfo("S-AC", "A", "C", "C-1", "rail", 5, 1, "suspended"),
            SegmentInfo("S-CD", "C", "D", "C-1", "rail", 5, 1, "active"),
        ]

    def test_enumerate_paths_is_stable_and_skips_suspended(self):
        paths = enumerate_paths(self.segments, "A", "D")
        self.assertEqual([["S-AB", "S-BD"], ["S-AD"]],
                         [[s.segment_id for s in path] for path in paths])

    def test_plan_routes_prefers_cheapest_and_offers_standby(self):
        result = plan_routes(_facts(), "A", "D", self.segments, {}, {}, [])
        self.assertEqual(["S-AB", "S-BD"],
                         [s.segment_id for s in result["primary"]["legs"]])
        self.assertEqual([["S-AD"]],
                         [[s.segment_id for s in s_["legs"]] for s_ in result["standbys"]])
        self.assertIn("总耗时最低", result["summary"])

    def test_closed_port_and_rule_violation_reject_path(self):
        jurisdictions = {"A": "KZ", "B": "CN", "D": "DE"}
        statuses = {"A": "open", "B": "closed", "D": "open"}
        result = plan_routes(_facts(), "A", "D", self.segments, jurisdictions, statuses, [])
        self.assertEqual(["S-AD"], [s.segment_id for s in result["primary"]["legs"]])
        rules = [RuleInfo("R", 1, "DE", "destination", "value_cap",
                          {"max_value": 50, "currency": "EUR"})]
        result = plan_routes(_facts(), "A", "D", self.segments, jurisdictions,
                             {"A": "open", "B": "open", "D": "open"}, rules)
        self.assertIsNone(result["primary"])
        self.assertEqual(2, len(result["rejections"]))

    def test_assess_path_records_evaluations_with_reasons(self):
        jurisdictions = {"A": "KZ", "B": "CN", "D": "DE"}
        statuses = {"A": "open", "B": "open", "D": "open"}
        rules = [RuleInfo("R", 2, "DE", "destination", "value_cap",
                          {"max_value": 500, "currency": "EUR"})]
        feasible, evaluations, notes = assess_path(
            [self.segments[0], self.segments[1]], _facts(), jurisdictions, statuses, rules)
        self.assertTrue(feasible)
        self.assertEqual(1, len(evaluations))
        self.assertEqual("pass", evaluations[0]["result"])
        self.assertEqual(2, evaluations[0]["version"])
        self.assertEqual([], notes)


if __name__ == "__main__":
    unittest.main()
