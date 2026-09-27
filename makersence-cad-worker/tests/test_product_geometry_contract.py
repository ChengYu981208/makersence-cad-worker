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

    def test_nonprotected_hybrid_contract_does_not_force_protected_route(self):
        self.assertFalse(is_interface_preserving_scope({"design_scope_contract": {"protect_mating_interface": False}}))
        self.assertEqual(protected_rebuild_blockers({}), ["CAD_CONTRACT_OBJECT_REQUIRED"])


if __name__ == "__main__":
    unittest.main()
