"""Conservative, product-agnostic placement arrangement selection."""

from __future__ import annotations

import math
from typing import Any


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _valid_pose(row: dict[str, Any]) -> dict[str, Any] | None:
    matrix = row.get("rotation_matrix")
    translation = row.get("translation_mm")
    box = row.get("pose_bbox_mm")
    if not isinstance(matrix, list) or len(matrix) != 3 or any(not isinstance(r, list) or len(r) != 3 for r in matrix):
        return None
    if not isinstance(translation, list) or len(translation) != 3 or not isinstance(box, dict):
        return None
    try:
        m = [[float(v) for v in r] for r in matrix]
        t = [float(v) for v in translation]
        low = [float(v) for v in box["min"]]
        high = [float(v) for v in box["max"]]
        score = float(row["score"])
        contact_count = int(row.get("contact_count") or 0)
        sample_count = int(row.get("sample_count") or 0)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(v) for r in m for v in r) or not all(math.isfinite(v) for v in t + low + high + [score]):
        return None
    if any(high[i] <= low[i] for i in range(3)):
        return None
    # Accept only a proper rigid rotation; reject scale, shear, and reflection.
    for i in range(3):
        for j in range(3):
            dot = sum(m[k][i] * m[k][j] for k in range(3))
            if abs(dot - (1.0 if i == j else 0.0)) > 1e-3:
                return None
    determinant = (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )
    if abs(determinant - 1.0) > 1e-3:
        return None
    return {
        "rotation_matrix": m,
        "translation_mm": t,
        "score": score,
        "pose_bbox_mm": {"min": low, "max": high, "dimensions": [high[i] - low[i] for i in range(3)]},
        "contact_bbox_mm": row.get("contact_bbox_mm") or {},
        "contact_count": contact_count,
        "sample_count": sample_count,
        "intrusion_ratio": _number(row.get("intrusion_ratio")),
        "pose_metrics": row.get("pose_metrics") or {},
    }


def _center(row: dict[str, Any]) -> list[float]:
    box = row["pose_bbox_mm"]
    return [(box["min"][i] + box["max"][i]) / 2.0 for i in range(3)]


def _aabb_clearance_lower_bound(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    aa = a["pose_bbox_mm"]
    bb = b["pose_bbox_mm"]
    gaps = []
    for axis in range(3):
        if aa["max"][axis] <= bb["min"][axis]:
            gaps.append(bb["min"][axis] - aa["max"][axis])
        elif bb["max"][axis] <= aa["min"][axis]:
            gaps.append(aa["min"][axis] - bb["max"][axis])
    return max(gaps) if gaps else None


def _manifold_source(mesh: Any) -> dict[str, Any] | None:
    if not isinstance(mesh, dict):
        return None
    vertices = mesh.get("vertices")
    triangles = mesh.get("triangles")
    if not isinstance(vertices, list) or not isinstance(triangles, list) or len(triangles) < 4:
        return None
    # Bound CPU and memory before constructing the exact collision kernel.
    if len(vertices) > 1_000_000 or len(triangles) > 1_000_000:
        return None
    try:
        import numpy as np
        import manifold3d as m3d

        vp = np.ascontiguousarray(vertices, dtype=np.float32)
        tv = np.ascontiguousarray(triangles, dtype=np.uint32)
        if vp.ndim != 2 or vp.shape[1] != 3 or tv.ndim != 2 or tv.shape[1] != 3:
            return None
        if not np.isfinite(vp).all() or int(tv.max(initial=0)) >= len(vp):
            return None
        source = m3d.Manifold(m3d.Mesh(vert_properties=vp, tri_verts=tv))
        if source.status() != m3d.Error.NoError:
            return None
        volume = abs(float(source.volume()))
        if not math.isfinite(volume) or volume <= 1e-9:
            return None
        return {"base": source, "module": m3d, "numpy": np, "volume": volume}
    except Exception:
        return None


def _pose_manifold(state: dict[str, Any], pose: dict[str, Any]):
    np = state["numpy"]
    matrix = [
        [pose["rotation_matrix"][row][0], pose["rotation_matrix"][row][1],
         pose["rotation_matrix"][row][2], pose["translation_mm"][row]]
        for row in range(3)
    ]
    result = state["base"].transform(np.ascontiguousarray(matrix, dtype=np.float64))
    if result.status() != state["module"].Error.NoError:
        return None
    return result


def _exact_mesh_clearance(a: Any, b: Any, state: dict[str, Any], required_gap: float) -> dict[str, Any] | None:
    try:
        module = state["module"]
        intersection = a ^ b
        if intersection.status() != module.Error.NoError:
            return None
        overlap_volume = abs(float(intersection.volume()))
        if not math.isfinite(overlap_volume) or overlap_volume > max(1e-8, state["volume"] * 1e-12):
            return None
        if required_gap > 0:
            search_length = max(required_gap + 0.01, required_gap * 2.0)
            clearance = float(a.min_gap(b, search_length))
            if not math.isfinite(clearance) or clearance + 1e-4 < required_gap:
                return None
        else:
            clearance = 0.0
        return {
            "method": "MANIFOLD3D_EXACT_MESH_GAP",
            "clearance_lower_bound_mm": round(clearance, 6),
            "overlap_volume_mm3": round(overlap_volume, 9),
            "required_gap_mm": round(required_gap, 6),
            "status": "PASS",
        }
    except Exception:
        return None


def solve_instance_arrangement(
    candidates: list[dict[str, Any]],
    expected_instances: int,
    *,
    source_dimensions_mm: list[float],
    minimum_contact_count: int,
    minimum_gap_mm: float = 0.0,
    maximum_intrusion_ratio: float = 0.22,
    max_candidates: int = 60,
    max_search_nodes: int = 50000,
    uniqueness_absolute_gap: float = 8.0,
    uniqueness_relative_gap: float = 0.035,
    source_mesh: dict[str, Any] | None = None,
    max_exact_pair_checks: int = 500,
) -> dict[str, Any]:
    """Select a unique, supported set of mutually separated instance poses.

    This deliberately uses non-overlapping AABBs as the proof of inter-instance
    separation. It therefore rejects some safe mesh arrangements but never
    treats overlapping bounds as proof of a collision-free arrangement.
    """
    try:
        expected = int(expected_instances)
    except (TypeError, ValueError, OverflowError):
        expected = 0
    if expected < 2 or expected > 12:
        return {"status": "REVIEW_REQUIRED", "confidence": "LOW", "expected_instances": expected,
                "matched_instances": 0, "instance_transforms": [], "arrangement_unique": False,
                "instance_solution_complete": False,
                "instance_solution_method": "BOUNDED_NONOVERLAPPING_AABB_ARRANGEMENT_V1",
                "reason": "expected_instance_count_out_of_range", "candidate_arrangement_search_complete": True}
    gap = _number(minimum_gap_mm)
    if gap is None or gap < 0:
        return {"status": "REVIEW_REQUIRED", "confidence": "LOW", "expected_instances": expected,
                "matched_instances": 0, "instance_transforms": [], "arrangement_unique": False,
                "instance_solution_complete": False,
                "instance_solution_method": "BOUNDED_NONOVERLAPPING_AABB_ARRANGEMENT_V1",
                "reason": "minimum_instance_gap_invalid", "candidate_arrangement_search_complete": True}

    try:
        source_dims = [_number(v) for v in source_dimensions_mm]
    except TypeError:
        source_dims = []
    if len(source_dims) != 3 or any(v is None or v <= 0 for v in source_dims):
        return {"status": "REVIEW_REQUIRED", "confidence": "LOW", "expected_instances": expected,
                "matched_instances": 0, "instance_transforms": [], "arrangement_unique": False,
                "instance_solution_complete": False,
                "instance_solution_method": "BOUNDED_NONOVERLAPPING_AABB_ARRANGEMENT_V1",
                "reason": "source_dimensions_invalid", "candidate_arrangement_search_complete": True}

    valid: list[dict[str, Any]] = []
    for raw in candidates or []:
        if not isinstance(raw, dict):
            continue
        row = _valid_pose(raw)
        if row is None or row["contact_count"] < max(1, int(minimum_contact_count)):
            continue
        if row["intrusion_ratio"] is None or row["intrusion_ratio"] > maximum_intrusion_ratio:
            continue
        contact_dims = row["contact_bbox_mm"].get("dimensions") or []
        if len(contact_dims) != 3:
            continue
        if sum(1 for i, value in enumerate(contact_dims)
               if (_number(value) or 0.0) >= max(5.0, source_dims[i] * 0.07)) < 2:
            continue
        valid.append(row)

    # Collapse nearby optimizer samples for the same physical seat. Keep the
    # strongest evidence at that seat, independent of pose orientation.
    valid.sort(key=lambda x: x["score"], reverse=True)
    unique: list[dict[str, Any]] = []
    source_diag = math.sqrt(sum(v * v for v in source_dims))
    cluster_tolerance = max(4.0, min(20.0, source_diag * 0.06))
    for row in valid:
        if any(math.dist(_center(row), _center(prior)) <= cluster_tolerance for prior in unique):
            continue
        unique.append(row)
        if len(unique) >= max(2, min(int(max_candidates), 120)):
            break

    arrangements: list[tuple[float, tuple[int, ...], list[dict[str, Any]]]] = []
    nodes = 0
    exact_pair_checks = 0
    exhausted = False
    mesh_state: dict[str, Any] | None = None
    mesh_state_checked = False
    transformed: dict[int, Any] = {}
    pair_cache: dict[tuple[int, int], dict[str, Any] | None] = {}

    def transformed_pose(index: int):
        nonlocal mesh_state, mesh_state_checked
        if index in transformed:
            return transformed[index]
        if not mesh_state_checked:
            mesh_state = _manifold_source(source_mesh)
            mesh_state_checked = True
        if mesh_state is None:
            transformed[index] = None
        else:
            transformed[index] = _pose_manifold(mesh_state, unique[index])
        return transformed[index]

    def pair_proof(left_index: int, right_index: int) -> dict[str, Any] | None:
        nonlocal exact_pair_checks, exhausted
        key = tuple(sorted((left_index, right_index)))
        if key in pair_cache:
            return pair_cache[key]
        left, right = unique[left_index], unique[right_index]
        lower_bound = _aabb_clearance_lower_bound(left, right)
        if lower_bound is not None and lower_bound + 1e-7 >= gap:
            proof = {
                "method": "AABB_SEPARATION_LOWER_BOUND",
                "clearance_lower_bound_mm": round(lower_bound, 6),
                "required_gap_mm": round(gap, 6),
                "status": "PASS",
            }
            pair_cache[key] = proof
            return proof
        if source_mesh is None or exact_pair_checks >= max(1, int(max_exact_pair_checks)):
            if source_mesh is not None:
                exhausted = True
            pair_cache[key] = None
            return None
        left_mesh = transformed_pose(left_index)
        right_mesh = transformed_pose(right_index)
        if left_mesh is None or right_mesh is None:
            pair_cache[key] = None
            return None
        exact_pair_checks += 1
        proof = _exact_mesh_clearance(left_mesh, right_mesh, mesh_state, gap)
        if proof is not None:
            pair_cache[key] = proof
        else:
            pair_cache[key] = None
        return proof

    def record(indices: tuple[int, ...], score: float, proofs: list[dict[str, Any]]) -> None:
        arrangements.append((score / expected, indices, proofs))
        arrangements.sort(key=lambda x: x[0], reverse=True)
        del arrangements[4:]

    def visit(start: int, chosen: tuple[int, ...], score: float, proofs: list[dict[str, Any]]) -> None:
        nonlocal nodes, exhausted
        if nodes >= max_search_nodes:
            exhausted = True
            return
        nodes += 1
        if len(chosen) == expected:
            record(chosen, score, proofs)
            return
        remaining = expected - len(chosen)
        if len(unique) - start < remaining:
            return
        for index in range(start, len(unique)):
            if len(unique) - index < remaining:
                break
            candidate = unique[index]
            current_proofs = list(proofs)
            separated = True
            for prior in chosen:
                proof = pair_proof(index, prior)
                if proof is None:
                    separated = False
                    break
                current_proofs.append({
                    **proof,
                    "first_instance_index": min(index, prior),
                    "second_instance_index": max(index, prior),
                })
            if separated:
                visit(index + 1, chosen + (index,), score + candidate["score"], current_proofs)
            if exhausted:
                return

    visit(0, (), 0.0, [])
    best = arrangements[0] if arrangements else None
    second = arrangements[1] if len(arrangements) > 1 else None
    score_gap = (best[0] - second[0]) if best and second else None
    required_score_gap = max(uniqueness_absolute_gap, abs(best[0]) * uniqueness_relative_gap) if best else None
    unique_solution = bool(best and not exhausted and (second is None or score_gap >= required_score_gap))
    reason = "unique_collision_checked_arrangement" if unique_solution else (
        "arrangement_search_budget_exhausted" if exhausted else
        "arrangement_not_unique" if best else
        "no_supported_collision_free_arrangement" if source_mesh is not None else
        "no_supported_nonoverlapping_arrangement"
    )

    alternatives = []
    for score, indices, proofs in arrangements[:3]:
        alternatives.append({
            "mean_score": round(score, 4),
            "instance_transforms": [unique[i] for i in indices],
            "clearance_pair_proofs": proofs,
        })
    selected = [unique[i] for i in best[1]] if unique_solution and best else []
    selected_proofs = best[2] if unique_solution and best else []
    exact_used = any(row.get("method") == "MANIFOLD3D_EXACT_MESH_GAP" for row in selected_proofs)
    return {
        "status": "ALIGNED" if unique_solution else "REVIEW_REQUIRED",
        "confidence": "HIGH" if unique_solution else "MEDIUM" if best and not exhausted else "LOW",
        "expected_instances": expected,
        "matched_instances": len(best[1]) if best else 0,
        "instance_transforms": selected,
        "arrangement_unique": unique_solution,
        "instance_solution_complete": unique_solution and len(selected) == expected,
        "instance_solution_method": (
            "BOUNDED_MANIFOLD3D_MESH_GAP_ARRANGEMENT_V1" if exact_used
            else "BOUNDED_NONOVERLAPPING_AABB_ARRANGEMENT_V1"
        ),
        "reason": reason,
        "candidate_arrangement_search_complete": not exhausted,
        "uniqueness_basis": "SEARCHED_CANDIDATE_POSES_ONLY",
        "search_nodes": nodes,
        "exact_pair_checks": exact_pair_checks,
        "supported_pose_count": len(unique),
        "minimum_instance_gap_mm": gap,
        "arrangement_score_gap": round(score_gap, 4) if score_gap is not None else None,
        "arrangement_score_gap_required": round(required_score_gap, 4) if required_score_gap is not None else None,
        "candidate_arrangements": alternatives,
        "clearance_pair_proofs": selected_proofs,
        "separation_proof": (
            "AABB_AND_MANIFOLD3D_EXACT_GAP" if exact_used
            else "NONOVERLAPPING_AXIS_ALIGNED_BOUNDING_BOXES"
        ),
    }

