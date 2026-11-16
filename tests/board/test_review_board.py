import tempfile
import threading
import unittest
from pathlib import Path

from export_portfolio.intake.initiative import ContractError, DomainRecord
from export_portfolio.portfolio_office import Application

PAYLOAD = {"product_family": "robot", "target_country": "th", "scenario_proof": .8,
           "standard_gap": .2, "partner_strength": .8, "localization_effort": 10,
           "delivery_capacity": 4, "expected_revenue": 100, "currency_risk": .2,
           "policy_risk": .2, "support_cost": 5, "dependency_count": 2}


def scenarios(low, mid, high):
    return [{"name": "downside", "probability": .25, "revenue": low},
            {"name": "base", "probability": .5, "revenue": mid},
            {"name": "upside", "probability": .25, "revenue": high}]


class ReviewBoardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "board.sqlite3"
        self.app = Application(self.db)
        self.board = self.app.review_board
        self.book = self.app.resource_book
        for project_id in ("p1", "p2", "p3"):
            self.app.repository.save_record(DomainRecord(project_id, dict(PAYLOAD)))
        self.book.capacity("partner", 5)
        for project_id in ("p1", "p2", "p3"):
            self.book.require(project_id, "partner", 2)
        self.board.register_forecast("f-p1", "p1", scenarios(80, 100, 120))
        self.board.register_forecast("f-p2", "p2", scenarios(70, 90, 110))
        self.board.register_forecast("f-p3", "p3", scenarios(60, 80, 100))

    def test_forecast_must_express_uncertain_range(self):
        with self.assertRaises(ContractError):
            self.board.register_forecast("bad", "p1", [{"name": "sure", "probability": 1.0, "revenue": 10}])
        with self.assertRaises(ContractError):
            self.board.register_forecast("bad", "p1", [{"name": "a", "probability": .5, "revenue": 10},
                                                       {"name": "b", "probability": .5, "revenue": 10}])
        with self.assertRaises(ContractError):
            self.board.register_forecast("bad", "p1", [{"name": "a", "probability": .4, "revenue": 10},
                                                       {"name": "b", "probability": .5, "revenue": 20}])
        with self.assertRaises(ContractError):
            self.board.register_forecast("bad", "p1", [{"name": "a", "probability": .5, "revenue": -1},
                                                       {"name": "b", "probability": .5, "revenue": 20}])
        with self.assertRaises(ContractError):
            self.board.register_forecast("bad", "unknown", scenarios(1, 2, 3))
        with self.assertRaises(ContractError):
            self.board.register_forecast("other", "p1", scenarios(1, 2, 3))
        with self.assertRaises(ContractError):
            self.board.register_forecast("f-p1", "p2", scenarios(1, 2, 3))

    def test_forecast_revision_is_stable_and_idempotent(self):
        self.assertEqual(self.board.register_forecast("f-p1", "p1", scenarios(80, 100, 120)), 1)
        self.assertEqual(self.board.register_forecast("f-p1", "p1", scenarios(85, 100, 125)), 2)
        stored = self.board.inspect()["forecasts"]
        self.assertEqual({row["forecast_id"]: row["revision"] for row in stored},
                         {"f-p1": 2, "f-p2": 1, "f-p3": 1})

    def test_recommendation_explains_range_usage_and_opportunity(self):
        rec = self.board.recommend(["p1", "p2", "p3"])
        self.assertEqual(rec["recommended"], ["p1", "p2"])
        self.assertFalse(rec["all_together"]["feasible"])
        self.assertEqual(rec["all_together"]["usage"], {"partner": 6})
        self.assertEqual(rec["resources"]["partner"]["available"], 5)
        chosen = next(c for c in rec["combinations"] if c["projects"] == ["p1", "p2"])
        self.assertEqual((chosen["low"], chosen["expected"], chosen["high"]), (150.0, 190.0, 230.0))
        self.assertEqual(chosen["usage"], {"partner": 4})
        self.assertEqual(rec["opportunity_cost"], 180.0)
        for combo in rec["combinations"]:
            self.assertLess(combo["low"], combo["high"])

    def test_open_review_is_pinned_idempotent_and_needs_feasible_choice(self):
        first = self.board.open_review("r1", ["p1", "p2", "p3"])
        self.assertEqual(first["status"], "pending")
        self.assertEqual(self.board.open_review("r1", ["p1", "p2", "p3"])["basis_hash"], first["basis_hash"])
        with self.assertRaises(ContractError):
            self.board.open_review("r1", ["p1", "p2"])
        with self.assertRaises(ContractError):
            self.board.open_review("r2", ["p1", "missing"])
        with self.assertRaises(ContractError):
            self.board.open_review("r2", ["missing"])
        self.app.repository.save_record(DomainRecord("p4", dict(PAYLOAD)))
        self.book.require("p4", "partner", 6)
        self.board.register_forecast("f-p4", "p4", scenarios(1, 2, 3))
        with self.assertRaises(ContractError):
            self.board.open_review("r2", ["p4"])

    def test_approve_advances_selected_and_retains_snapshot(self):
        self.board.open_review("r1", ["p1", "p2", "p3"])
        approved = self.board.approve_review("r1")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(self.board.approve_review("r1")["basis_hash"], approved["basis_hash"])
        again = self.board.open_review("r1", ["p3", "p1", "p2"])
        self.assertEqual(again["status"], "approved")
        self.assertEqual(again["basis_hash"], approved["basis_hash"])
        with self.assertRaisesRegex(ContractError, "不可改写"):
            self.board.open_review("r1", ["p1", "p2"])
        state = Application(self.db)
        self.assertEqual(state.resource_book.inspect()["resources"][0]["available"], 1)
        self.assertEqual(state.repository.get_record("p1").state, "diligence")
        self.assertEqual(state.repository.get_record("p2").state, "diligence")
        self.assertEqual(state.repository.get_record("p3").state, "screening")
        snapshot = approved["review"]["recommendation"]
        self.assertEqual(snapshot["recommended"], ["p1", "p2"])
        self.assertEqual(snapshot["resources"]["partner"]["revision"], 1)
        self.assertEqual({c["project_id"]: c["forecast_revision"] for c in snapshot["candidates"]},
                         {"p1": 1, "p2": 1, "p3": 1})
        usage = {row["resource_id"]: row["units"]
                 for row in state.resource_book.inspect()["allocations"]}
        self.assertEqual(usage, {"partner": 4})

    def test_approve_rejects_changed_project_basis(self):
        self.board.open_review("r1", ["p1", "p2", "p3"])
        changed = dict(PAYLOAD, expected_revenue=999)
        self.app.ingest(DomainRecord("p3", changed))
        with self.assertRaisesRegex(ContractError, "项目依据已变化"):
            self.board.approve_review("r1")
        self.assertEqual(self.book.inspect()["resources"][0]["available"], 5)

    def test_approve_rejects_changed_requirements(self):
        self.board.open_review("r1", ["p1", "p2", "p3"])
        self.book.require("p3", "partner", 1)
        with self.assertRaisesRegex(ContractError, "资源需求已变化"):
            self.board.approve_review("r1")
        self.assertEqual(self.book.inspect()["resources"][0]["available"], 5)

    def test_approve_rejects_commitment_change(self):
        self.board.open_review("r1", ["p1", "p2", "p3"])
        self.book.capacity("partner", 4)
        with self.assertRaisesRegex(ContractError, "承诺已变化"):
            self.board.approve_review("r1")
        self.assertEqual(self.book.inspect()["resources"][0]["available"], 4)
        self.assertEqual(self.board.inspect()["reviews"][0]["status"], "pending")

    def test_approve_rejects_updated_forecast_then_new_review(self):
        self.board.open_review("r1", ["p1", "p2", "p3"])
        self.board.register_forecast("f-p1", "p1", scenarios(85, 105, 125))
        with self.assertRaisesRegex(ContractError, "预测已更新"):
            self.board.approve_review("r1")
        with self.assertRaisesRegex(ContractError, "不可改写"):
            self.board.open_review("r1", ["p1", "p2", "p3"])
        second = self.board.open_review("r2", ["p1", "p2", "p3"])
        pinned = {c["project_id"]: c["forecast_revision"]
                  for c in second["review"]["recommendation"]["candidates"]}
        self.assertEqual(pinned["p1"], 2)
        self.assertEqual(self.board.approve_review("r2")["status"], "approved")
        self.assertEqual(self.book.inspect()["resources"][0]["available"], 1)

    def test_later_forecast_cannot_rewrite_approved_review(self):
        first = self.board.open_review("r1", ["p1", "p2", "p3"])
        self.board.approve_review("r1")
        self.board.register_forecast("f-p3", "p3", scenarios(65, 85, 105))
        self.book.capacity("partner", 6)
        followup = self.board.open_review("r2", ["p3"])
        self.assertEqual(followup["review"]["recommendation"]["candidates"][0]["forecast_revision"], 2)
        self.assertEqual(self.board.approve_review("r2")["status"], "approved")
        self.assertEqual(self.book.inspect()["resources"][0]["available"], 0)
        stored = {row["review_id"]: row for row in self.board.inspect()["reviews"]}
        self.assertEqual(stored["r1"]["basis_hash"], first["basis_hash"])
        self.assertEqual(stored["r1"]["status"], "approved")
        snapshot = stored["r1"]["review"]["recommendation"]
        self.assertEqual(snapshot["recommended"], ["p1", "p2"])
        self.assertEqual(snapshot["resources"]["partner"]["revision"], 1)
        self.assertEqual({c["project_id"]: c["forecast_revision"] for c in snapshot["candidates"]},
                         {"p1": 1, "p2": 1, "p3": 1})

    def test_concurrent_reviews_never_over_approve(self):
        self.board.open_review("ra", ["p1", "p2"])
        self.board.open_review("rb", ["p3"])
        first, second = Application(self.db), Application(self.db)
        barrier = threading.Barrier(2)
        outcome = {}

        def attempt(app, review_id, key):
            barrier.wait()
            try:
                app.review_board.approve_review(review_id)
                outcome[key] = "approved"
            except ContractError:
                outcome[key] = "rejected"

        threads = [threading.Thread(target=attempt, args=(first, "ra", "ra")),
                   threading.Thread(target=attempt, args=(second, "rb", "rb"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcome.values()), ["approved", "rejected"])
        final = Application(self.db).resource_book.inspect()
        self.assertGreaterEqual(final["resources"][0]["available"], 1)
        self.assertEqual(len(final["approvals"]), 1)

    def test_approved_projects_cannot_be_reused(self):
        self.board.open_review("r1", ["p1", "p2", "p3"])
        self.board.approve_review("r1")
        with self.assertRaises(ContractError):
            self.book.require("p1", "partner", 1)
        with self.assertRaises(ContractError):
            self.book.approve("other", ["p1"], {"partner": 1})
        with self.assertRaisesRegex(ContractError, "不能重复评议"):
            self.board.open_review("r2", ["p1", "p3"])
        with self.assertRaises(ContractError):
            self.board.approve_review("unknown")


if __name__ == "__main__":
    unittest.main()
