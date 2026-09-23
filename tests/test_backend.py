from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from money_graph.clustering import adjusted_rand_index  # noqa: E402
from money_graph.features import _fifo_match  # noqa: E402
from money_graph.scoring import fit_continuation_model, signal_percentile  # noqa: E402
from money_graph.validation import synthetic_motif_checks  # noqa: E402


class CoreAlgorithmTests(unittest.TestCase):
    def test_fifo_does_not_double_spend_inflow(self) -> None:
        day = pd.Timestamp("2026-07-01")
        matched, median_lag = _fifo_match(
            [(day, 100.0)],
            [(day, 80.0), (day + pd.Timedelta(days=1), 80.0)],
            1,
        )
        self.assertEqual(matched, 100.0)
        self.assertIn(median_lag, {0.0, 1.0})

    def test_adjusted_rand_is_label_permutation_invariant(self) -> None:
        left = np.array([0, 0, 1, 1, 2])
        right = np.array([7, 7, 4, 4, 9])
        self.assertAlmostEqual(adjusted_rand_index(left, right), 1.0)

    def test_zero_signal_has_zero_percentile(self) -> None:
        ranked = signal_percentile(pd.Series([0.0, 0.0, 1.0, 2.0]))
        self.assertEqual(ranked.iloc[0], 0.0)
        self.assertEqual(ranked.iloc[1], 0.0)
        self.assertGreater(ranked.iloc[3], ranked.iloc[2])

    def test_continuation_model_is_bounded_and_transparent(self) -> None:
        n = 80
        strength = np.arange(n)
        frame = pd.DataFrame(
            {
                "depth": 1 + strength % 3,
                "is_seed": False,
                "in_deg": 1 + strength % 8,
                "in_tx": 1 + strength,
                "in_kzt": 5_000.0 * (1 + strength),
                "active_in_days": 1 + strength % 15,
                "in_source_hhi": np.linspace(1.0, 0.1, n),
                "direct_seed_in": strength % 3,
                "seed_affinity": 0.1 + strength / 20,
                "first_in_day": 1 + strength % 20,
                "last_in_day": 10 + strength % 20,
                "out_deg": (strength >= 35).astype(int),
            }
        )
        result = fit_continuation_model(frame)
        self.assertTrue(result.probability.between(0.0, 1.0).all())
        self.assertEqual(result.metadata["model"], "l2_logistic_irls_standardized")
        self.assertIn("standardized_coefficients", result.metadata)

    def test_synthetic_motifs(self) -> None:
        result = synthetic_motif_checks()
        self.assertTrue(result["passed"], result)


if __name__ == "__main__":
    unittest.main()
