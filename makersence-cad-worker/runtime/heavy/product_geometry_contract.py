"""Fail-closed contract checks for protected-interface product rebuilds.

This module validates the Hub-to-Heavy geometry contract only. It does not
advertise or implement the CAD executor; protected rebuilds remain blocked
until the geometry executor consumes the validated plan and returns evidence.
"""
from __future__ import annotations

from math import isfinite, sqrt
from typing import Any

CAPABILITY_ID = "interface_preserving_exterior_rebuild_v1"
READY_BINDING_STATES = {"READY", "PASS", "CAD_EXECUTOR_BOUND", "PARAMETRIC_MAPPING_RESOLVED"}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _scope(contract: dict) -> dict:
    geometry = _dict(contract.get("product_geometry_contract"))
    return _dict(contract.get("design_scope_contract") or geometry.get("design_scope_contract"))


def is_interface_preserving_scope(contract: Any) -> bool:
    if not isinstance(contract, dict):
        return False
    geometry = _dict(contract.get("product_geometry_contract"))
    scope = _scope(contract)
    execution = _dict(scope.get("execution"))
    required = str(execution.get("required_capability") or "")
    capabilities = {str(x) for x in _list(geometry.get("required_capabilities"))}
    return scope.get("protect_mating_interface") is True or required == CAPABILITY_ID or CAPABILITY_ID in capabilities


def _valid_transform(value: Any) -> bool:
    matrix = value
    if not isinstance(matrix, list):
        return False
    if len(matrix) == 3 and all(isinstance(row, list) and len(row) == 4 for row in matrix):
        matrix = matrix + [[0.0, 0.0, 0.0, 1.0]]
    if len(matrix) != 4 or any(not isinstance(row, list) or len(row) != 4 for row in matrix):
        return False
    numbers = [[_number(x) for x in row] for row in matrix]
    if any(x is None for row in numbers for x in row):
        return False
    if any(abs(numbers[3][i] - expected) > 1e-6 for i, expected in enumerate((0.0, 0.0, 0.0, 1.0))):
        return False
    columns = [[numbers[row][col] for row in range(3)] for col in range(3)]
    lengths = [sqrt(sum(x * x for x in col)) for col in columns]
    if min(lengths) <= 1e-6:
        return False
    mean = sum(lengths) / 3.0
    if max(abs(x - mean) for x in lengths) > max(1e-4, mean * 0.02):
        return False
    for i in range(3):
        for j in range(i + 1, 3):
            dot = sum(columns[i][k] * columns[j][k] for k in range(3))
            if abs(dot) > lengths[i] * lengths[j] * 0.02:
                return False
    a, b, c = columns
    determinant = (
        a[0] * (b[1] * c[2] - b[2] * c[1])
        - b[0] * (a[1] * c[2] - a[2] * c[1])
        + c[0] * (a[1] * b[2] - a[2] * b[1])
    )
    return determinant > 1e-8


def interface_preserving_contract_issues(contract: Any) -> list[str]:
    if not isinstance(contract, dict):
        return ["CAD_CONTRACT_OBJECT_REQUIRED"]
    geometry = _dict(contract.get("product_geometry_contract"))
    scope = _scope(contract)
    scope_execution = _dict(scope.get("execution"))
    issues: list[str] = []
    if not geometry:
        return ["PRODUCT_GEOMETRY_CONTRACT_MISSING"]
    if scope.get("protect_mating_interface") is not True:
        issues.append("DESIGN_SCOPE_DOES_NOT_PROTECT_MATING_INTERFACE")
    if scope_execution.get("required_capability") != CAPABILITY_ID:
        issues.append("INTERFACE_REBUILD_CAPABILITY_ID_MISMATCH")
    if scope_execution.get("available") is not True or scope_execution.get("ready") is not True:
        issues.append("DESIGN_SCOPE_EXECUTOR_NOT_READY")
    if str(geometry.get("status") or "").upper() != "READY":
        issues.append("PRODUCT_GEOMETRY_NOT_READY")
    if geometry.get("executor_ready") is not True:
        issues.append("PRODUCT_GEOMETRY_EXECUTOR_NOT_READY")
    if CAPABILITY_ID not in {str(x) for x in _list(geometry.get("required_capabilities"))}:
        issues.append("REQUIRED_CAPABILITY_NOT_DECLARED")
    for blocker in _list(geometry.get("hard_blockers")):
        issues.append("HARD_BLOCKER:" + str(blocker))

    nodes = _list(_dict(geometry.get("feature_graph")).get("nodes"))
    execution = _dict(geometry.get("interface_execution"))
    mode = str(_dict(geometry.get("geometry_evidence")).get("interface_mode") or scope.get("interface_mode") or "").upper()
    core = _dict(execution.get("protected_core"))
    core_id = str(core.get("part_id") or "")
    device_reference_ids: set[str] = set()
    protected_node = next((n for n in nodes if _dict(n).get("type") == "protected_interface"
                           and str(_dict(n).get("operation") or "").upper() == "PRESERVE"), None)

    if mode == "DEVICE_ENVELOPE":
        reference = _dict(geometry.get("interface_reference"))
        reference_ids = {str(x) for x in _list(reference.get("part_ids")) if str(x)}
        device_reference_ids = reference_ids
        if (str(reference.get("status") or "").upper() != "IDENTIFIED"
                or reference.get("assembly_role") != "NON_PRINTABLE_COUNTERPART_REFERENCE"
                or reference.get("export_policy") != "EXCLUDE_FROM_PRINTABLE_OUTPUT"
                or not reference_ids):
            issues.append("DEVICE_ENVELOPE_REFERENCE_CLASSIFICATION_INVALID")
        if core_id and core_id in reference_ids:
            issues.append("DEVICE_ENVELOPE_COUNTERPART_MUST_NOT_BE_PRINTED")
        for reference_id in reference_ids:
            source = next((n for n in nodes if str(_dict(n).get("id") or "") == reference_id
                           and _dict(n).get("type") == "source_part"), None)
            if source is None or str(_dict(source).get("role") or "").lower() != "interface":
                issues.append("DEVICE_ENVELOPE_COUNTERPART_SOURCE_NOT_FOUND:" + reference_id)
                continue
            mesh = _dict(_dict(source).get("geometry_evidence")).get("mesh_brep")
            mesh = _dict(mesh)
            if (str(mesh.get("status") or "").lower() != "ready"
                    or str(mesh.get("strategy") or "") != "FACETED_MESH_BREP"
                    or str(mesh.get("encoding") or "") != "zlib_base64_json_v1"
                    or not isinstance(mesh.get("payload"), str) or not mesh.get("payload")
                    or (_number(mesh.get("triangle_count")) or 0) < 4):
                issues.append("DEVICE_ENVELOPE_COUNTERPART_MESH_INVALID:" + reference_id)
        if protected_node is None or str(_dict(protected_node).get("mode") or "").upper() != "DEVICE_ENVELOPE":
            issues.append("DEVICE_ENVELOPE_PROTECTED_INTERFACE_LINK_MISSING")
        alignment = _dict(reference.get("alignment"))
        transform = alignment.get("selected_transform_4x4")
        if (alignment.get("pose_unique") is not True or str(alignment.get("status") or "").upper() != "ALIGNED"
                or str(alignment.get("confidence") or "").upper() != "HIGH" or not _valid_transform(transform)):
            issues.append("DEVICE_ENVELOPE_ALIGNMENT_UNRESOLVED")
        fit = _dict(reference.get("fit"))
        clearance = _number(fit.get("xy_clearance_mm"))
        if clearance is None or clearance <= 0:
            issues.append("DEVICE_ENVELOPE_CLEARANCE_UNRESOLVED")
        if fit.get("physical_validation_required") is not True:
            issues.append("DEVICE_ENVELOPE_PHYSICAL_FIT_VALIDATION_MUST_REMAIN_REQUIRED")
    else:
        if core.get("preserve_source_geometry") is not True or not core_id:
            issues.append("PROTECTED_SOURCE_CORE_BINDING_MISSING")
        source = next((n for n in nodes if str(_dict(n).get("id") or "") == core_id and _dict(n).get("type") == "source_part"), None)
        if source is None:
            issues.append("PROTECTED_SOURCE_PART_NOT_FOUND")
        else:
            mesh = _dict(_dict(source).get("geometry_evidence")).get("mesh_brep")
            mesh = _dict(mesh)
            if (str(mesh.get("status") or "").lower() != "ready"
                    or str(mesh.get("strategy") or "") != "FACETED_MESH_BREP"
                    or str(mesh.get("encoding") or "") != "zlib_base64_json_v1"
                    or not isinstance(mesh.get("payload"), str) or not mesh.get("payload")
                    or (_number(mesh.get("triangle_count")) or 0) < 4):
                issues.append("PROTECTED_SOURCE_MESH_BREP_INVALID")
        if protected_node is None or str(_dict(protected_node).get("source_part_id") or "") != core_id:
            issues.append("PROTECTED_INTERFACE_SOURCE_LINK_MISSING")

    bindings = _list(geometry.get("geometry_bindings"))
    def matching_binding(key: str, value: str) -> dict | None:
        for item in bindings:
            row = _dict(item)
            if str(row.get(key) or "") == value:
                state = str(row.get("execution_state") or row.get("status") or "").upper()
                if state in READY_BINDING_STATES and row.get("geometry_node_id") and row.get("operation"):
                    return row
        return None

    if mode == "DEVICE_ENVELOPE":
        for item in bindings:
            row = _dict(item)
            if str(row.get("geometry_node_id") or "") in device_reference_ids:
                issues.append("DEVICE_ENVELOPE_REFERENCE_BOUND_AS_PRINTABLE_GEOMETRY:" + str(row.get("geometry_node_id")))

    understanding = _dict(geometry.get("product_understanding_contract"))
    for zone in _list(understanding.get("functional_zones")):
        row = _dict(zone)
        zone_id = str(row.get("id") or "")
        if row.get("required", True) and zone_id and not matching_binding("zone_id", zone_id):
            issues.append("FUNCTIONAL_ZONE_GEOMETRY_BINDING_MISSING:" + zone_id)

    fidelity = _dict(geometry.get("design_fidelity_gate"))
    for signature in _list(fidelity.get("required_signatures")):
        row = _dict(signature)
        signature_id = str(row.get("id") or row.get("signature_id") or "")
        if row.get("priority") == "REQUIRED" and signature_id:
            binding = matching_binding("signature_id", signature_id)
            if row.get("mapped_to_executor") is not True or not _dict(row.get("executor_binding")) or binding is None:
                issues.append("DESIGN_SIGNATURE_GEOMETRY_BINDING_MISSING:" + signature_id)

    registration = _dict(execution.get("exterior_shell_registration"))
    transform = registration.get("source_to_design_transform_4x4")
    rms = _number(registration.get("rms_residual_mm"))
    if str(registration.get("status") or "").upper() != "PASS" or not _valid_transform(transform):
        issues.append("EXTERIOR_SHELL_REGISTRATION_NOT_RESOLVED")
    if (_number(registration.get("matched_landmark_count")) or 0) < 3 or rms is None or rms > 0.5:
        issues.append("EXTERIOR_SHELL_REGISTRATION_EVIDENCE_INSUFFICIENT")

    fit_proofs = _list(execution.get("fit_clearance_proofs"))
    if not fit_proofs:
        issues.append("FIT_CLEARANCE_PROOF_MISSING")
    for index, proof in enumerate(fit_proofs):
        row = _dict(proof)
        actual = _number(row.get("actual_mm"))
        required = _number(row.get("required_mm"))
        if (str(row.get("status") or "").upper() != "PASS" or actual is None or required is None
                or required <= 0 or actual + 1e-6 < required or row.get("evidence_verified") is not True):
            issues.append("FIT_CLEARANCE_PROOF_INVALID:" + str(row.get("id") or index + 1))
    return issues


def protected_rebuild_blockers(contract: Any) -> list[str]:
    issues = interface_preserving_contract_issues(contract)
    if issues:
        return issues
    # The executor is intentionally not exposed until it also builds, validates,
    # and exports both protected core and exterior shell as an assembly.
    return ["INTERFACE_PRESERVING_REBUILD_EXECUTOR_NOT_IMPLEMENTED"]
