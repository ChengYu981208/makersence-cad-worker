import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime" / "heavy"))
from product_geometry_contract import (
    interface_preserving_contract_issues,
    is_interface_preserving_scope,
    protected_rebuild_blockers,
)


def ready_contract():
    signature = {"id": "sig_cradle", "priority": "REQUIRED", "mapped_to_executor": True,
                 "executor_binding": {"operation": "PRESERVE_SOURCE_GEOMETRY", "geometry_node_id": "CORE"}}
    zone = {"id": "cradle", "required": True}
    return {
        "design_scope_contract": {
            "protect_mating_interface": True,
            "execution": {"required_capability": "interface_preserving_exterior_rebuild_v1", "available": True, "ready": True},
        },
        "design_model": {"concept_approved": True},
        "product_geometry_contract": {
            "version": "makersence-worker-product-geometry-v1", "status": "READY", "executor_ready": True,
            "required_capabilities": ["interface_preserving_exterior_rebuild_v1"], "hard_blockers": [],
            "feature_graph": {"nodes": [
                {"id": "CORE", "type": "source_part", "geometry_evidence": {"mesh_brep": {
                    "status": "ready", "strategy": "FACETED_MESH_BREP", "encoding": "zlib_base64_json_v1",
                    "payload": "synthetic-payload", "triangle_count": 12,
                }}},
                {"id": "PROTECTED_INTERFACE", "type": "protected_interface", "operation": "PRESERVE", "source_part_id": "CORE"},
            ]},
            "product_understanding_contract": {"functional_zones": [zone]},
            "design_fidelity_gate": {"required_signatures": [signature]},
            "geometry_bindings": [
                {"zone_id": "cradle", "execution_state": "CAD_EXECUTOR_BOUND", "geometry_node_id": "CORE", "operation": "PRESERVE_SOURCE_GEOMETRY"},
                {"signature_id": "sig_cradle", "execution_state": "CAD_EXECUTOR_BOUND", "geometry_node_id": "CORE", "operation": "PRESERVE_SOURCE_GEOMETRY"},
            ],
            "interface_execution": {
                "protected_core": {"part_id": "CORE", "preserve_source_geometry": True},
                "exterior_shell_registration": {
                    "status": "PASS", "matched_landmark_count": 3, "rms_residual_mm": 0.2,
                    "source_to_design_transform_4x4": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
                },
                "fit_clearance_proofs": [{"id": "cradle_clearance", "status": "PASS", "actual_mm": 0.25,
                                           "required_mm": 0.25, "evidence_verified": True}],
            },
        },
    }



def ready_device_envelope_contract():
    contract = ready_contract()
    geometry = contract["product_geometry_contract"]
    geometry["geometry_evidence"] = {"interface_mode": "DEVICE_ENVELOPE"}
    source = geometry["feature_graph"]["nodes"][0]
    source["role"] = "interface"
    geometry["feature_graph"]["nodes"].insert(1, {
        "id": "DEVICE_REFERENCE", "type": "counterpart_reference",
        "assembly_role": "NON_PRINTABLE_COUNTERPART_REFERENCE",
        "geometry_evidence": {"mesh_brep": {
            "status": "ready", "strategy": "FACETED_MESH_BREP",
            "encoding": "zlib_base64_json_v1", "payload": "counterpart-payload",
            "triangle_count": 16,
        }},
    })
    geometry["feature_graph"]["nodes"].append({"id": "CRADLE", "type": "generated_geometry"})
    geometry["feature_graph"]["nodes"][2].pop("source_part_id", None)
    geometry["feature_graph"]["nodes"][2]["mode"] = "DEVICE_ENVELOPE"
    geometry.pop("interface_execution", None)
    geometry["interface_execution"] = {
        "exterior_shell_registration": {
            "status": "PASS", "matched_landmark_count": 3, "rms_residual_mm": 0.2,
            "source_to_design_transform_4x4": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
        },
        "fit_clearance_proofs": [{
            "id": "cradle_clearance", "status": "PASS", "actual_mm": 0.25,
            "required_mm": 0.25, "evidence_verified": True,
        }],
    }
    geometry["interface_reference"] = {
        "status": "IDENTIFIED", "mode": "DEVICE_ENVELOPE",
        "assembly_role": "NON_PRINTABLE_COUNTERPART_REFERENCE",
        "export_policy": "EXCLUDE_FROM_PRINTABLE_OUTPUT",
        "part_ids": ["DEVICE_REFERENCE"],
        "alignment": {
            "status": "ALIGNED", "confidence": "HIGH", "pose_unique": True, "solution_count": 1,
            "expected_instances": 1, "matched_instances": 1,
            "selected_transform_4x4": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
            "candidate_poses": [],
        },
        "fit": {"xy_clearance_mm": 0.25, "calibration_status": "UNVERIFIED", "physical_validation_required": True},
    }
    geometry["geometry_bindings"] = [
        {"zone_id": "cradle", "execution_state": "CAD_EXECUTOR_BOUND", "geometry_node_id": "CRADLE", "operation": "BUILD_CLEARANCE_CRADLE"},
        {"signature_id": "sig_cradle", "execution_state": "CAD_EXECUTOR_BOUND", "geometry_node_id": "CRADLE", "operation": "BUILD_CLEARANCE_CRADLE"},
    ]
    signature = geometry["design_fidelity_gate"]["required_signatures"][0]
    signature["executor_binding"] = {"operation": "BUILD_CLEARANCE_CRADLE", "geometry_node_id": "CRADLE"}
    contract["design_scope_contract"]["interface_mode"] = "DEVICE_ENVELOPE"
    return contract

def ready_multi_device_envelope_contract():
    contract = ready_device_envelope_contract()
    alignment = contract["product_geometry_contract"]["interface_reference"]["alignment"]
    identity = [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
    second = [[1,0,0,20],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
    alignment.update({
        "expected_instances": 2, "matched_instances": 2,
        "selected_transform_4x4": None,
        "instance_transforms_4x4": [identity, second],
        "instance_solution_method": "BOUNDED_NONOVERLAPPING_AABB_ARRANGEMENT_V1",
        "instance_solution_complete": True,
        "instance_interface_selections": [
            {"instance_index": 0, "axis": "Y", "plane_mm": 3, "raw_source_plane_mm": 7,
             "band_mm": 8, "span_mm": 20, "confidence": "HIGH",
             "coordinate_frame": "source_largest_part_min_normalized", "transform_4x4": identity},
            {"instance_index": 1, "axis": "Y", "plane_mm": 5, "raw_source_plane_mm": 9,
             "band_mm": 8, "span_mm": 20, "confidence": "HIGH",
             "coordinate_frame": "source_largest_part_min_normalized", "transform_4x4": second},
        ],
        "instance_arrangement": {
            "candidate_arrangement_search_complete": True,
            "uniqueness_basis": "SEARCHED_CANDIDATE_POSES_ONLY",
            "separation_proof": "NONOVERLAPPING_AXIS_ALIGNED_BOUNDING_BOXES",
            "minimum_instance_gap_mm": 0.0,
        },
    })
    return contract


class ProductGeometryContractTests(unittest.TestCase):
    def test_unmapped_live_shape_is_blocked_before_provider_call(self):
        contract = {
            "design_scope_contract": {"protect_mating_interface": True,
                                      "execution": {"required_capability": "interface_preserving_exterior_rebuild_v1", "available": False, "ready": False}},
            "product_geometry_contract": {"status": "READY_FOR_INTERFACE_ENGINE", "executor_ready": False,
                                          "required_capabilities": ["interface_preserving_exterior_rebuild_v1"],
                                          "hard_blockers": ["feature_geometry_mapping_incomplete:zone:device_contact_cradle"],
                                          "feature_graph": {"nodes": []}},
        }
        self.assertTrue(is_interface_preserving_scope(contract))
        blockers = protected_rebuild_blockers(contract)
        self.assertIn("DESIGN_SCOPE_EXECUTOR_NOT_READY", blockers)
        self.assertIn("PRODUCT_GEOMETRY_EXECUTOR_NOT_READY", blockers)
        self.assertIn("PROTECTED_SOURCE_CORE_BINDING_MISSING", blockers)
        self.assertTrue(any(x.startswith("HARD_BLOCKER:") for x in blockers))

    def test_explicitly_mapped_contract_passes_contract_checks_but_not_executor_gate(self):
        contract = ready_contract()
        self.assertEqual(interface_preserving_contract_issues(contract), [])
        self.assertEqual(protected_rebuild_blockers(contract), ["INTERFACE_PRESERVING_REBUILD_EXECUTOR_NOT_IMPLEMENTED"])

    def test_invalid_or_unverified_clearance_is_rejected(self):
        contract = ready_contract()
        proof = contract["product_geometry_contract"]["interface_execution"]["fit_clearance_proofs"][0]
        proof["actual_mm"] = 0.1
        blockers = interface_preserving_contract_issues(contract)
        self.assertIn("FIT_CLEARANCE_PROOF_INVALID:cradle_clearance", blockers)

    def test_shell_registration_requires_unique_scale_preserving_transform_evidence(self):
        contract = ready_contract()
        registration = contract["product_geometry_contract"]["interface_execution"]["exterior_shell_registration"]
        registration["source_to_design_transform_4x4"] = [[2,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
        blockers = interface_preserving_contract_issues(contract)
        self.assertIn("EXTERIOR_SHELL_REGISTRATION_NOT_RESOLVED", blockers)

    def test_device_envelope_counterpart_is_excluded_from_print_geometry(self):
        contract = ready_device_envelope_contract()
        self.assertEqual(interface_preserving_contract_issues(contract), [])
        blockers = protected_rebuild_blockers(contract)
        self.assertEqual(blockers, ["INTERFACE_PRESERVING_REBUILD_EXECUTOR_NOT_IMPLEMENTED"])

        geometry = contract["product_geometry_contract"]
        geometry["geometry_bindings"][0]["geometry_node_id"] = "DEVICE_REFERENCE"
        geometry["geometry_bindings"][0]["operation"] = "PRESERVE_SOURCE_GEOMETRY"
        blockers = interface_preserving_contract_issues(contract)
        self.assertIn("DEVICE_ENVELOPE_REFERENCE_BOUND_AS_PRINTABLE_GEOMETRY:DEVICE_REFERENCE", blockers)

    def test_device_envelope_ambiguous_pose_and_missing_export_policy_are_blocked(self):
        contract = ready_device_envelope_contract()
        reference = contract["product_geometry_contract"]["interface_reference"]
        reference["alignment"]["pose_unique"] = False
        reference["alignment"]["selected_transform_4x4"] = None
        blockers = interface_preserving_contract_issues(contract)
        self.assertIn("DEVICE_ENVELOPE_ALIGNMENT_UNRESOLVED", blockers)

        contract = ready_device_envelope_contract()
        geometry = contract["product_geometry_contract"]
        geometry["feature_graph"]["nodes"] = [n for n in geometry["feature_graph"]["nodes"] if n["id"] != "DEVICE_REFERENCE"]
        geometry["interface_reference"]["part_ids"] = ["CORE"]
        blockers = interface_preserving_contract_issues(contract)
        self.assertIn("DEVICE_ENVELOPE_COUNTERPART_GEOMETRY_NODE_NOT_FOUND:CORE", blockers)

        contract = ready_device_envelope_contract()
        contract["product_geometry_contract"]["interface_reference"].pop("export_policy")
        blockers = interface_preserving_contract_issues(contract)
        self.assertIn("DEVICE_ENVELOPE_REFERENCE_CLASSIFICATION_INVALID", blockers)

    def test_device_envelope_multi_instance_requires_complete_interface_selections(self):
        contract = ready_multi_device_envelope_contract()
        self.assertEqual(interface_preserving_contract_issues(contract), [])
        self.assertEqual(protected_rebuild_blockers(contract), ["INTERFACE_PRESERVING_REBUILD_EXECUTOR_NOT_IMPLEMENTED"])

        alignment = contract["product_geometry_contract"]["interface_reference"]["alignment"]
        alignment["instance_interface_selections"].pop()
        blockers = interface_preserving_contract_issues(contract)
        self.assertIn("DEVICE_ENVELOPE_INSTANCE_ARRANGEMENT_INCOMPLETE", blockers)
        self.assertIn("DEVICE_ENVELOPE_ALIGNMENT_UNRESOLVED", blockers)

        contract = ready_multi_device_envelope_contract()
        contract["product_geometry_contract"]["interface_reference"]["alignment"]["instance_solution_complete"] = False
        blockers = interface_preserving_contract_issues(contract)
        self.assertIn("DEVICE_ENVELOPE_INSTANCE_ARRANGEMENT_INCOMPLETE", blockers)


    def test_nonprotected_hybrid_contract_does_not_force_protected_route(self):
        self.assertFalse(is_interface_preserving_scope({"design_scope_contract": {"protect_mating_interface": False}}))
        self.assertEqual(protected_rebuild_blockers({}), ["PRODUCT_GEOMETRY_CONTRACT_MISSING"])


    def test_protected_hybrid_request_stops_before_external_provider(self):
        import importlib.util
        import types

        heavy_dir = Path(__file__).resolve().parents[1] / "runtime" / "heavy"
        if str(heavy_dir) not in sys.path:
            sys.path.insert(0, str(heavy_dir))
        if "svgpathtools" not in sys.modules:
            svg_stub = types.ModuleType("svgpathtools")
            svg_stub.parse_path = lambda _path: None
            sys.modules["svgpathtools"] = svg_stub
        spec = importlib.util.spec_from_file_location("makersence_heavy_contract_test", heavy_dir / "app.py")
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        provider_calls = []
        runtime.route_design_model = lambda payload: provider_calls.append(payload)
        contract = {
            "design_scope_contract": {
                "protect_mating_interface": True,
                "execution": {"required_capability": "interface_preserving_exterior_rebuild_v1", "available": False, "ready": False},
            },
            "product_geometry_contract": {
                "status": "READY_FOR_INTERFACE_ENGINE", "executor_ready": False,
                "required_capabilities": ["interface_preserving_exterior_rebuild_v1"],
                "hard_blockers": ["feature_geometry_mapping_incomplete:zone:device_contact_cradle"],
                "feature_graph": {"nodes": []},
            },
            "design_model": {"source_image_url": "https://example.com/approved.png", "concept_approved": True},
        }
        with self.assertRaisesRegex(ValueError, "INTERFACE_PRESERVING_REBUILD_BLOCKED"):
            runtime.design_model_hybrid(contract, {})
        self.assertEqual(provider_calls, [])

    def test_counterpart_alignment_transfers_closed_geometry_and_blocks_unmatched_instances(self):
        import base64
        import importlib.util
        import json
        import types
        import zlib

        heavy_dir = Path(__file__).resolve().parents[1] / "runtime" / "heavy"
        if str(heavy_dir) not in sys.path:
            sys.path.insert(0, str(heavy_dir))
        if "svgpathtools" not in sys.modules:
            svg_stub = types.ModuleType("svgpathtools")
            svg_stub.parse_path = lambda _path: None
            sys.modules["svgpathtools"] = svg_stub
        spec = importlib.util.spec_from_file_location("makersence_heavy_mesh_evidence_test", heavy_dir / "app.py")
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)

        tetra = {
            "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
            "triangles": [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]],
        }
        evidence = runtime._ca_counterpart_mesh_evidence(tetra)
        self.assertEqual(evidence["status"], "ready")
        self.assertEqual(evidence["coordinate_frame"], "COUNTERPART_3MF_OBJECT_FRAME")
        self.assertEqual(evidence["open_edges"], 0)
        self.assertEqual(evidence["nonmanifold_edges"], 0)
        payload = json.loads(zlib.decompress(base64.b64decode(evidence["payload"])))
        self.assertEqual(len(payload["v"]), 4)
        self.assertEqual(len(payload["t"]), 4)

        open_cube = {
            "vertices": [[0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1],[1,1,1],[0,1,1]],
            "triangles": [
                [0,2,1],[0,3,2],[4,5,6],[4,6,7],
                [0,1,5],[0,5,4],[3,7,6],[3,6,2],
                [0,4,7],[0,7,3],[1,2,6],[1,6,5],
            ][:-2],
        }
        rejected = runtime._ca_counterpart_mesh_evidence(open_cube)
        self.assertEqual(rejected["status"], "unavailable")
        self.assertNotIn("payload", rejected)
        self.assertGreater(rejected["open_edges"], 0)

        transform = {"rotation_matrix": [[1,0,0],[0,1,0],[0,0,1]], "translation_mm": [0,0,0]}
        one = runtime._ca_instance_alignment_evidence(1, "HIGH", transform)
        two = runtime._ca_instance_alignment_evidence(2, "HIGH", transform)
        self.assertEqual(one["status"], "ALIGNED")
        self.assertTrue(one["instance_solution_complete"])
        self.assertEqual(two["status"], "REVIEW_REQUIRED")
        self.assertEqual(two["expected_instances"], 2)
        self.assertEqual(two["matched_instances"], 1)
        self.assertFalse(two["instance_solution_complete"])

if __name__ == "__main__":
    unittest.main()
