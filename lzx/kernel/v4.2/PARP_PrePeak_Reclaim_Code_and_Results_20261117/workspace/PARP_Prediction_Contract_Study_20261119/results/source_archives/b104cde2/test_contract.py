"""Contract, safety and regression checks before any privileged setup."""
import unittest
from oracle_scenarios import Scenario, scenarios
from prediction_contract import PredictionRequest, validate_output
from run_simulation import simulate
from run_simulation import Clock, Plant
from study_pipeline import Pipeline
from prepeak_timing import ReleaseProfile
from oracle_scenarios import MIB


class ContractTests(unittest.TestCase):
    def test_already_realized_growth_is_not_counted_again_before_grid_point(self):
        s, clock = Scenario(), Clock()
        plants = {"cold":Plant(528*MIB,clock),"hot":Plant(80*MIB,clock),"incoming":Plant(32*MIB,clock)}
        initial = {a:{"rss":p.rss(),"usage":p.usage()} for a,p in plants.items()}
        root = lambda:sum(p.usage() for p in plants.values())
        quota = root()+128*MIB
        p = Pipeline(s,"occupancy_supply",plants,root,quota,initial,
                     {"approved_bytes":384*MIB,"safe_rss":144*MIB,"safe_usage":144*MIB,
                      "idle_faults":0,"measurement_id":"SIM_ONLY"},
                     ReleaseProfile(16*MIB,.6,384*MIB),"grid-regression",clock,timing="epoch")
        for i in range(190):
            clock.value = i*.1
            p.tick(clock.value)
        clock.value = 19.55
        p.update(clock.value)
        clock.value = 20.01
        plants["incoming"].value += 256*MIB
        self.assertEqual(p.need(),0)
        p.need_mode = "time"
        self.assertGreater(p.need(),200*MIB)
        p.close(clock.value)

    def test_rolling_range_is_exact_and_no_past_is_returned(self):
        result, report = simulate(Scenario(), "occupancy_supply", 20., timing="epoch")
        self.assertTrue(result["passed"])
        frame = next(f for f in report["frames"] if f["time_s"] == 10.)
        for app, output in frame["intra_app"].items():
            self.assertEqual(len(output["windows"]), 40)
            self.assertEqual(output["windows"][0]["start_s"], 10.)
            self.assertEqual(output["windows"][-1]["end_s"], 30.)
            validate_output(output, PredictionRequest(**{**frame["requests"][app],
                                                         "observations": tuple(frame["requests"][app]["observations"])}))
        self.assertTrue(all(r["time_s"] <= f["time_s"] for f in report["frames"]
                            for request in f["requests"].values() for r in request["observations"]))

    def test_regression_new_peak_after_old_protection_expires(self):
        s = next(s for s in scenarios() if s.name == "oscillating")
        old, _ = simulate(s, "occupancy_supply", 12., timing="baseline")
        new, _ = simulate(s, "occupancy_supply", 12., timing="epoch")
        self.assertTrue(old["errors"])
        self.assertTrue(new["passed"])

    def test_occupancy_growth_is_not_inferred_from_wss(self):
        s = next(s for s in scenarios() if s.name == "flat_WSS_growth")
        old, _ = simulate(s, "wss_only", 20., timing="epoch")
        new, _ = simulate(s, "occupancy_supply", 20., timing="epoch")
        self.assertFalse(old["passed"])
        self.assertTrue(new["passed"])

    def test_hot_revisit_is_blocked_not_claimed_as_success(self):
        s = next(s for s in scenarios() if s.name == "cold_revisit")
        result, report = simulate(s, "occupancy_supply", 20., timing="epoch")
        self.assertTrue(result["correctly_blocked"])
        self.assertFalse(result["passed"])
        self.assertTrue(any(f["timing"]["unallocated_bytes"] > 0 for f in report["frames"]))


if __name__ == "__main__":
    unittest.main()
