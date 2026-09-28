import unittest
import sys
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime" / "heavy"))
import counterpart_arrangement
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


def tetra_mesh():
    return {
        "vertices": [[0, 0, 0], [10, 0, 0], [0, 10, 0], [0, 0, 10]],
        "triangles": [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]],
    }


def mesh_candidate(translation, score):
    row = candidate(translation[0], translation[0] + 10, score)
    row["translation_mm"] = list(translation)
    row["pose_bbox_mm"] = {
        "min": list(translation),
        "max": [translation[axis] + 10 for axis in range(3)],
    }
    row["contact_bbox_mm"] = {"dimensions": [8, 8, 8]}
    return row


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

    def test_exact_mesh_clearance_accepts_disjoint_shapes_with_overlapping_aabbs(self):
        result = solve_instance_arrangement(
            [mesh_candidate([0, 0, 0], 100), mesh_candidate([6, 6, 0], 99)],
            2,
            source_dimensions_mm=[10, 10, 10],
            minimum_contact_count=14,
            minimum_gap_mm=0.5,
            source_mesh=tetra_mesh(),
        )
        self.assertEqual(result["status"], "ALIGNED")
        self.assertEqual(result["instance_solution_method"], "BOUNDED_MANIFOLD3D_MESH_GAP_ARRANGEMENT_V1")
        self.assertEqual(result["separation_proof"], "AABB_AND_MANIFOLD3D_EXACT_GAP")
        proof = result["clearance_pair_proofs"][0]
        self.assertEqual(proof["method"], "MANIFOLD3D_EXACT_MESH_GAP")
        self.assertGreaterEqual(proof["clearance_lower_bound_mm"], 0.5)
        self.assertLessEqual(proof["search_length_mm"], 0.51)
        self.assertEqual(proof["overlap_volume_mm3"], 0.0)

    def test_clearance_pair_indices_follow_selected_instance_order(self):
        candidates = [
            mesh_candidate([0, 0, 0], 100),
            mesh_candidate([4.5, 0, 0], 99),
            mesh_candidate([6, 6, 0], 50),
        ]
        result = solve_instance_arrangement(
            candidates,
            2,
            source_dimensions_mm=[10, 10, 10],
            minimum_contact_count=14,
            minimum_gap_mm=0.5,
            source_mesh=tetra_mesh(),
        )

        self.assertEqual(result["status"], "ALIGNED")
        self.assertEqual(result["matched_instances"], 2)
        self.assertEqual(
            result["clearance_pair_proofs"][0]["first_instance_index"], 0
        )
        self.assertEqual(
            result["clearance_pair_proofs"][0]["second_instance_index"], 1
        )

    def test_exact_mesh_intersection_rejects_overlapping_shapes_with_overlapping_aabbs(self):
        result = solve_instance_arrangement(
            [mesh_candidate([0, 0, 0], 100), mesh_candidate([4, 4, 0], 99)],
            2,
            source_dimensions_mm=[10, 10, 10],
            minimum_contact_count=14,
            minimum_gap_mm=0.0,
            source_mesh=tetra_mesh(),
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["matched_instances"], 0)
        self.assertEqual(result["reason"], "no_supported_collision_free_arrangement")

    def test_invalid_exact_mesh_cannot_fall_back_to_overlapping_aabbs(self):
        invalid_mesh = {"vertices": tetra_mesh()["vertices"], "triangles": [[0, 1, 2]]}
        result = solve_instance_arrangement(
            [mesh_candidate([0, 0, 0], 100), mesh_candidate([6, 6, 0], 99)],
            2,
            source_dimensions_mm=[10, 10, 10],
            minimum_contact_count=14,
            minimum_gap_mm=0.0,
            source_mesh=invalid_mesh,
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["matched_instances"], 0)

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

    def test_exact_mesh_pose_cache_is_bounded_for_large_candidate_pool(self):
        candidates = []
        for index, x in enumerate(range(0, 80, 8)):
            row = mesh_candidate([x, 0, 0], 100 - index)
            row["pose_bbox_mm"]["max"] = [x + 100, 10, 10]
            candidates.append(row)

        fake_state = {"base": object()}
        exact_proof = {
            "method": "MANIFOLD3D_EXACT_MESH_GAP",
            "clearance_lower_bound_mm": 0.25,
            "overlap_volume_mm3": 0.0,
            "required_gap_mm": 0.25,
            "status": "PASS",
        }
        with (
            patch.object(counterpart_arrangement, "_manifold_source", return_value=fake_state),
            patch.object(counterpart_arrangement, "_pose_manifold", side_effect=lambda _state, _pose: object()) as pose_builder,
            patch.object(counterpart_arrangement, "_exact_mesh_clearance", return_value=exact_proof),
        ):
            result = solve_instance_arrangement(
                candidates,
                2,
                source_dimensions_mm=[100, 20, 20],
                minimum_contact_count=14,
                minimum_gap_mm=0.25,
                source_mesh=tetra_mesh(),
                max_candidates=20,
                max_exact_pair_checks=100,
            )

        self.assertEqual(result["transformed_pose_cache_capacity"], 1)
        self.assertEqual(result["transformed_pose_cache_peak"], 1)
        self.assertGreater(pose_builder.call_count, result["transformed_pose_cache_capacity"])

    def test_compact_numpy_mesh_runs_exact_clearance_and_reports_stages(self):
        import numpy as np

        source_mesh = tetra_mesh()
        source_mesh["vertices"] = np.asarray(source_mesh["vertices"], dtype=np.float32)
        source_mesh["triangles"] = np.asarray(source_mesh["triangles"], dtype=np.uint32)
        stages = []
        result = solve_instance_arrangement(
            [mesh_candidate([0, 0, 0], 100), mesh_candidate([6, 6, 0], 99)],
            2,
            source_dimensions_mm=[10, 10, 10],
            minimum_contact_count=14,
            minimum_gap_mm=0.5,
            source_mesh=source_mesh,
            stage_callback=stages.append,
        )

        self.assertEqual(result["status"], "ALIGNED")
        self.assertLess(stages.index("manifold_source_start"), stages.index("manifold_source_ready"))
        self.assertIn("exact_pair_clearance_start", stages)
        self.assertIn("exact_pair_clearance_complete", stages)

    def test_relative_pose_matches_world_pose_pair_in_left_frame(self):
        left_rotation = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
        right_rotation = [[-1, 0, 0], [0, -1, 0], [0, 0, 1]]
        left = {"rotation_matrix": left_rotation, "translation_mm": [10, 20, 3]}
        right = {"rotation_matrix": right_rotation, "translation_mm": [8, 22, 7]}
        relative = counterpart_arrangement._relative_pose(left, right)
        point = [2, 3, 4]

        world_left = [
            sum(left_rotation[row][col] * point[col] for col in range(3)) + left["translation_mm"][row]
            for row in range(3)
        ]
        world_right = [
            sum(right_rotation[row][col] * point[col] for col in range(3)) + right["translation_mm"][row]
            for row in range(3)
        ]
        world_right_in_left_frame = [
            sum(left_rotation[row][axis] * (world_right[row] - left["translation_mm"][row]) for row in range(3))
            for axis in range(3)
        ]
        relative_point = [
            sum(relative["rotation_matrix"][row][col] * point[col] for col in range(3))
            + relative["translation_mm"][row]
            for row in range(3)
        ]

        self.assertEqual(world_left, [7, 22, 7])
        for actual, expected in zip(relative_point, world_right_in_left_frame):
            self.assertAlmostEqual(actual, expected)

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

