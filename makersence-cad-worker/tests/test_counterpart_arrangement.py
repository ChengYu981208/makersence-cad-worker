import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime" / "heavy"))
from counterpart_arrangement import solve_instance_arrangement


IDENTITY = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]


def candidate(x0, x1, score=100.0, rotation=None):
    return {
        "rotation_matrix": rotation or IDENTITY,
        "translation_mm": [x0, 0, 0],
        "score": score,
        "pose_bbox_mm": {"min": [x0, 0, 0], "max": [x1, 10, 10]},
        "contact_bbox_mm": {"dimensions": [8, 8, 0]},
        "contact_count": 30,
        "sample_count": 100,
        "intrusion_ratio": 0.02,
    }


class InstanceArrangementTests(unittest.TestCase):
    def solve(self, candidates, **kwargs):
        return solve_instance_arrangement(
            candidates, 2, source_dimensions_mm=[60, 20, 20], minimum_contact_count=14,
            minimum_gap_mm=0.25, **kwargs
        )

    def test_accepts_unique_nonoverlapping_supported_arrangement(self):
        result = self.solve([candidate(0, 10, 100), candidate(20, 30, 99)])
        self.assertEqual(result["status"], "ALIGNED")
        self.assertEqual(result["matched_instances"], 2)
        self.assertEqual(result["separation_proof"], "NONOVERLAPPING_AXIS_ALIGNED_BOUNDING_BOXES")

    def test_does_not_claim_arrangement_when_bounds_overlap(self):
        result = self.solve([candidate(0, 20, 100), candidate(15, 35, 99)])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["matched_instances"], 0)
        self.assertEqual(result["reason"], "no_supported_nonoverlapping_arrangement")

    def test_equal_competing_arrangements_remain_for_review(self):
        result = self.solve([candidate(0, 10), candidate(20, 30), candidate(40, 50)])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["reason"], "arrangement_not_unique")
        self.assertGreaterEqual(len(result["candidate_arrangements"]), 2)

    def test_nearby_optimizer_samples_for_one_seat_are_deduplicated(self):
        rows = [candidate(0, 10, 100), candidate(2, 12, 99), candidate(30, 40, 98)]
        result = self.solve(rows)
        self.assertEqual(result["status"], "ALIGNED")
        self.assertEqual(result["supported_pose_count"], 2)

    def test_invalid_rigid_transform_is_rejected(self):
        scale = [[2, 0, 0], [0, 1, 0], [0, 0, 1]]
        result = self.solve([candidate(0, 10, rotation=scale), candidate(20, 30)])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["matched_instances"], 0)

    def test_incomplete_search_cannot_be_accepted_as_unique(self):
        result = self.solve([candidate(0, 10), candidate(20, 30), candidate(40, 50)], max_search_nodes=1)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["reason"], "arrangement_search_budget_exhausted")


if __name__ == "__main__":
    unittest.main()

