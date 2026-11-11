"""Contract tests for page-to-TensorCast workload and native result units."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from msmodeling_bridge.config import normalize, workload


ROOT = Path(__file__).resolve().parents[1]


def request():
    return {"phase": "d", "batch": 2, "acceptedTokensPerCycle": 3, "config": {
        "input": 16384, "output": 1024, "decode": "dspark", "ttft": 10, "itl": .06, "tpot": .02,
        "util": 85, "reserve": 4, "usable": 0, "vision": False,
        "p": {"hw": "a5-96", "nodes": 1, "tp": 8, "ep": 8},
        "d": {"hw": "a5-96", "nodes": 1, "tp": 1, "ep": 8},
    }}


class WorkloadContract(unittest.TestCase):
    def plan(self, payload):
        return workload(payload, ROOT / "data/msmodeling/deepseek-v4.1-flash", Path(tempfile.gettempdir()) / "trace.json")

    def test_dp_batch_and_native_verify_window(self):
        p = request()
        effective = self.plan(p)
        self.assertEqual((effective["dp"], effective["globalQueries"], effective["queryLength"]), (8, 16, 6))
        argv = effective["argv"]
        self.assertEqual(argv[argv.index("--num-speculative-tokens") + 1], "5")
        p["phase"] = "p"
        e = self.plan(p)
        self.assertEqual((e["dp"], e["globalQueries"], e["queryLength"], e["context"]), (1, 2, 16384, 0))
        self.assertNotIn("--speculative-method", e["argv"])
        self.assertEqual(e["executionPath"], "full_40_layer_reference")

    def test_slo_never_changes_acceptance_or_native_work(self):
        p = request()
        a = self.plan(p)
        p["config"]["tpot"] = .001
        p["config"]["itl"] = .002
        b = self.plan(p)
        self.assertEqual(a["argv"], b["argv"])
        self.assertEqual(a["acceptedTokensPerCycle"], b["acceptedTokensPerCycle"])

    def test_ar_and_short_output_do_not_charge_five_drafts(self):
        p = request()
        p["config"]["decode"] = "ar"
        a = self.plan(p)
        self.assertEqual((a["queryLength"], a["acceptedTokensPerCycle"]), (1, 1))
        self.assertNotIn("--speculative-method", a["argv"])
        p["config"].update(decode="dspark", output=2)
        p["acceptedTokensPerCycle"] = 1
        self.assertNotIn("--speculative-method", self.plan(p)["argv"])
        p["config"]["d"] = {"hw": "a3-560", "nodes": 3, "tp": 8, "ep": 48}
        self.assertEqual(self.plan(p)["ep"], 48)
        p["phase"] = "p"
        p["config"]["p"] = p["config"]["d"].copy()
        self.assertEqual(self.plan(p)["ep"], 48)

    def test_observed_native_metrics_keep_cycle_wall_time_and_gib_separate(self):
        fixture = json.loads((ROOT / "tests/fixtures/msmodeling-ar-native.json").read_text(encoding="utf-8"))
        p = request()
        p.update(batch=1, acceptedTokensPerCycle=1)
        p["config"].update(input=128, output=8, decode="ar")
        e = self.plan(p)
        result = normalize(fixture["native"], e, {"revision": fixture["engineRevision"]}, "test")
        cycle = fixture["native"]["execution_time_s"]["analytic"]
        self.assertAlmostEqual(result["cycleSeconds"], cycle)
        self.assertAlmostEqual(result["requestQps"], 8 / (7 * cycle))
        self.assertAlmostEqual(result["tokensPerSecond"], 8 / cycle)
        self.assertEqual(result["peakMemoryGiB"], fixture["native"]["memory_gb"]["peak_usage"])
        self.assertNotEqual(result["simulatorWallSeconds"], result["cycleSeconds"])
        self.assertGreater(result["hostMemoryGiB"], 0)
        self.assertTrue(result["operators"])
        native = copy.deepcopy(fixture["native"])
        native["execution_time_s"] = {"analytic": None}
        with self.assertRaisesRegex(ValueError, "execution_time_s"):
            normalize(native, e, {}, "test")


if __name__ == "__main__":
    unittest.main()
