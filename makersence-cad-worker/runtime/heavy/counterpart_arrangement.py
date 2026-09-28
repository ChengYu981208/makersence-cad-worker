"""Conservative, product-agnostic placement arrangement selection."""

from __future__ import annotations

import math
from typing import Any, Callable


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


def _projection_clearance_lower_bound(
    source_mesh: Any, left: dict[str, Any], right: dict[str, Any], required_gap: float
) -> dict[str, Any] | None:
    """Prove a conservative mesh gap from exact vertex projection intervals.

    Every triangle lies inside the interval of its vertices on any unit axis.
    A positive interval gap is therefore a lower bound on the Euclidean
    distance between the complete transformed meshes. This is only a sufficient
    proof; unresolved pairs still use the exact Manifold3D distance check.
    """
    if not isinstance(source_mesh, dict):
        return None
    raw_vertices = source_mesh.get("vertices")
    if raw_vertices is None:
        return None
    try:
        import numpy as np

        vertices = np.asarray(raw_vertices, dtype=np.float64)
        if vertices.ndim != 2 or vertices.shape[1] != 3 or len(vertices) < 4 or len(vertices) > 1_000_000:
            return None
        if not np.isfinite(vertices).all():
            return None
        left_rotation = np.asarray(left["rotation_matrix"], dtype=np.float64)
        right_rotation = np.asarray(right["rotation_matrix"], dtype=np.float64)
        left_translation = np.asarray(left["translation_mm"], dtype=np.float64)
        right_translation = np.asarray(right["translation_mm"], dtype=np.float64)
        if left_rotation.shape != (3, 3) or right_rotation.shape != (3, 3):
            return None
        if left_translation.shape != (3,) or right_translation.shape != (3,):
            return None
        if not np.isfinite(left_rotation).all() or not np.isfinite(right_rotation).all():
            return None
        if not np.isfinite(left_translation).all() or not np.isfinite(right_translation).all():
            return None

        axes = [
            np.asarray(_center(right), dtype=np.float64) - np.asarray(_center(left), dtype=np.float64),
            right_translation - left_translation,
        ]
        axes.extend(np.eye(3, dtype=np.float64))
        axes.extend(left_rotation[:, index] for index in range(3))
        axes.extend(right_rotation[:, index] for index in range(3))

        coordinate_scale = max(
            1.0,
            float(np.max(np.abs(vertices))),
            float(np.max(np.abs(left_translation))),
            float(np.max(np.abs(right_translation))),
        )
        numeric_margin = max(1e-4, coordinate_scale * 1e-6)
        seen_axes: list[Any] = []
        for raw_axis in axes:
            axis = np.asarray(raw_axis, dtype=np.float64)
            norm = float(np.linalg.norm(axis))
            if not math.isfinite(norm) or norm <= 1e-9:
                continue
            axis = axis / norm
            if any(abs(float(np.dot(axis, prior))) >= 1.0 - 1e-9 for prior in seen_axes):
                continue
            seen_axes.append(axis)

            left_local_axis = left_rotation.T @ axis
            right_local_axis = right_rotation.T @ axis
            left_projection = vertices @ left_local_axis
            right_projection = vertices @ right_local_axis
            left_offset = float(np.dot(left_translation, axis))
            right_offset = float(np.dot(right_translation, axis))
            left_low = float(left_projection.min()) + left_offset
            left_high = float(left_projection.max()) + left_offset
            right_low = float(right_projection.min()) + right_offset
            right_high = float(right_projection.max()) + right_offset

            if left_high < right_low:
                raw_gap = right_low - left_high
                separated_order = "first_before_second"
            elif right_high < left_low:
                raw_gap = left_low - right_high
                separated_order = "second_before_first"
            else:
                continue
            intervals = {"first_mm": [left_low, left_high], "second_mm": [right_low, right_high]}

            lower_bound = raw_gap - numeric_margin
            if not math.isfinite(lower_bound) or lower_bound <= 0:
                continue
            conservative_mm = math.floor(lower_bound * 1_000_000.0) / 1_000_000.0
            if conservative_mm + 1e-7 < required_gap:
                continue
            return {
                "method": "VERTEX_PROJECTION_SEPARATION_LOWER_BOUND",
                "clearance_lower_bound_mm": conservative_mm,
                "required_gap_mm": round(required_gap, 6),
                "axis_world": [round(float(value), 9) for value in axis],
                "projection_intervals_mm": {
                    key: [round(float(value), 6) for value in values]
                    for key, values in intervals.items()
                },
                "separated_order": separated_order,
                "numerical_margin_mm": round(numeric_margin, 6),
                "status": "PASS",
            }
    except Exception:
        return None
    return None

def _manifold_source(mesh: Any) -> dict[str, Any] | None:
    if not isinstance(mesh, dict):
        return None
    vertices = mesh.get("vertices")
    triangles = mesh.get("triangles")
    if vertices is None or triangles is None:
        return None
    try:
        vertex_count = len(vertices)
        triangle_count = len(triangles)
    except TypeError:
        return None
    if triangle_count < 4:
        return None
    # Bound CPU and memory before constructing the exact collision kernel.
    if vertex_count > 1_000_000 or triangle_count > 1_000_000:
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
        diagonal = float(np.linalg.norm(vp.max(axis=0) - vp.min(axis=0)))
        if not math.isfinite(diagonal) or diagonal <= 1e-9:
            return None
        # Replace any nested Python lists with the exact contiguous arrays
        # already used by Manifold3D; later projection checks need the vertices.
        mesh["vertices"] = vp
        mesh["triangles"] = tv
        return {"base": source, "module": m3d, "numpy": np, "volume": volume, "diagonal": diagonal}
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


def _relative_pose(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Express the right instance relative to the left in the left object's frame."""
    left_rotation = left["rotation_matrix"]
    right_rotation = right["rotation_matrix"]
    relative_rotation = [
        [
            sum(left_rotation[k][row] * right_rotation[k][column] for k in range(3))
            for column in range(3)
        ]
        for row in range(3)
    ]
    delta = [
        right["translation_mm"][axis] - left["translation_mm"][axis]
        for axis in range(3)
    ]
    relative_translation = [
        sum(left_rotation[k][axis] * delta[k] for k in range(3))
        for axis in range(3)
    ]
    return {"rotation_matrix": relative_rotation, "translation_mm": relative_translation}


def _exact_mesh_clearance(a: Any, b: Any, state: dict[str, Any], required_gap: float) -> dict[str, Any] | None:
    try:
        if not math.isfinite(required_gap) or required_gap < 0:
            return None
        # Both solids are rigid transforms of the same source mesh. A positive
        # exact surface distance therefore proves they neither touch nor overlap;
        # avoid allocating a full boolean-intersection mesh for this proof.
        # We only need a conservative lower bound that proves the required
        # clearance. MinGap returns a value in [0, search_length], so searching
        # beyond the manufacturing gap adds no evidence and needlessly explores
        # distant triangle pairs on dense imported meshes.
        search_length = min(25.0, max(0.01, required_gap + 0.01))
        clearance = float(a.min_gap(b, search_length))
        if not math.isfinite(clearance) or clearance <= 1e-7 or clearance + 1e-4 < required_gap:
            return None
        conservative_clearance = max(0.0, math.floor(clearance * 1_000_000.0) / 1_000_000.0)
        return {
            "method": "MANIFOLD3D_EXACT_MESH_GAP",
            "clearance_lower_bound_mm": conservative_clearance,
            "overlap_volume_mm3": 0.0,
            "required_gap_mm": round(required_gap, 6),
            "search_length_mm": round(search_length, 6),
            "separation_basis": "POSITIVE_BOUNDARY_GAP_FOR_CONGRUENT_RIGID_INSTANCES",
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
    stage_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Select supported poses with bounded, auditable pairwise clearance proofs.

    AABB lower bounds avoid exact mesh work when they prove the required gap.
    Overlapping bounds require exact mesh clearance; incomplete or exhausted
    searches fail closed. Relative-pose checks keep one transformed mesh resident.
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
    projection_pair_checks = 0
    exhausted = False
    mesh_state: dict[str, Any] | None = None
    mesh_state_checked = False
    source_triangles = source_mesh.get("triangles") if isinstance(source_mesh, dict) else None
    source_triangle_count = len(source_triangles) if source_triangles is not None else 0
    transformed_cache_capacity = 1 if source_mesh is not None else 0
    transformed_cache_peak = 0
    pair_cache: dict[tuple[int, int], dict[str, Any] | None] = {}

    def report_stage(name: str) -> None:
        if callable(stage_callback):
            try:
                stage_callback(name)
            except Exception:
                pass

    def ensure_mesh_state() -> dict[str, Any] | None:
        nonlocal mesh_state, mesh_state_checked
        if not mesh_state_checked:
            report_stage("manifold_source_start")
            mesh_state = _manifold_source(source_mesh)
            mesh_state_checked = True
            report_stage("manifold_source_ready" if mesh_state is not None else "manifold_source_unavailable")
        return mesh_state

    def pair_proof(left_index: int, right_index: int) -> dict[str, Any] | None:
        nonlocal exact_pair_checks, projection_pair_checks, exhausted, transformed_cache_peak
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
        if source_mesh is None:
            pair_cache[key] = None
            return None
        state = ensure_mesh_state()
        if state is None:
            pair_cache[key] = None
            return None
        projection_pair_checks += 1
        report_stage("projection_separation_start")
        projection_proof = _projection_clearance_lower_bound(source_mesh, left, right, gap)
        report_stage("projection_separation_proven" if projection_proof is not None else "projection_separation_unresolved")
        if projection_proof is not None:
            pair_cache[key] = projection_proof
            return projection_proof
        if exact_pair_checks >= max(1, int(max_exact_pair_checks)):
            exhausted = True
            pair_cache[key] = None
            return None
        relative = _relative_pose(left, right)
        report_stage("relative_pose_start")
        relative_mesh = _pose_manifold(state, relative)
        if relative_mesh is None:
            report_stage("relative_pose_unavailable")
            pair_cache[key] = None
            return None
        transformed_cache_peak = max(transformed_cache_peak, 1)
        report_stage("relative_pose_ready")
        first_exact_check = exact_pair_checks == 0
        if first_exact_check:
            report_stage("exact_pair_clearance_start")
        exact_pair_checks += 1
        proof = _exact_mesh_clearance(state["base"], relative_mesh, state, gap)
        del relative_mesh
        if first_exact_check:
            report_stage("exact_pair_clearance_complete" if proof is not None else "exact_pair_clearance_failed")
        if proof is not None:
            pair_cache[key] = proof
        else:
            pair_cache[key] = None
        return proof

    def record(indices: tuple[int, ...], score: float, proofs: list[dict[str, Any]]) -> None:
        instance_position = {candidate_index: position for position, candidate_index in enumerate(indices)}
        normalized_proofs = []
        for proof in proofs:
            left = instance_position[proof["first_candidate_index"]]
            right = instance_position[proof["second_candidate_index"]]
            normalized = {key: value for key, value in proof.items()
                          if key not in {"first_candidate_index", "second_candidate_index"}}
            normalized["first_instance_index"] = min(left, right)
            normalized["second_instance_index"] = max(left, right)
            normalized_proofs.append(normalized)
        arrangements.append((score / expected, indices, normalized_proofs))
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
                    "first_candidate_index": min(index, prior),
                    "second_candidate_index": max(index, prior),
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
    geometric_gap_used = any(
        row.get("method") in {"MANIFOLD3D_EXACT_MESH_GAP", "VERTEX_PROJECTION_SEPARATION_LOWER_BOUND"}
        for row in selected_proofs
    )
    return {
        "status": "ALIGNED" if unique_solution else "REVIEW_REQUIRED",
        "confidence": "HIGH" if unique_solution else "MEDIUM" if best and not exhausted else "LOW",
        "expected_instances": expected,
        "matched_instances": len(best[1]) if best else 0,
        "instance_transforms": selected,
        "arrangement_unique": unique_solution,
        "instance_solution_complete": unique_solution and len(selected) == expected,
        "instance_solution_method": (
            "BOUNDED_GEOMETRIC_SEPARATION_ARRANGEMENT_V1" if geometric_gap_used
            else "BOUNDED_NONOVERLAPPING_AABB_ARRANGEMENT_V1"
        ),
        "reason": reason,
        "candidate_arrangement_search_complete": not exhausted,
        "uniqueness_basis": "SEARCHED_CANDIDATE_POSES_ONLY",
        "search_nodes": nodes,
        "exact_pair_checks": exact_pair_checks,
        "projection_pair_checks": projection_pair_checks,
        "transformed_pose_cache_capacity": transformed_cache_capacity,
        "transformed_pose_cache_peak": transformed_cache_peak,
        "supported_pose_count": len(unique),
        "minimum_instance_gap_mm": gap,
        "arrangement_score_gap": round(score_gap, 4) if score_gap is not None else None,
        "arrangement_score_gap_required": round(required_score_gap, 4) if required_score_gap is not None else None,
        "candidate_arrangements": alternatives,
        "clearance_pair_proofs": selected_proofs,
        "separation_proof": (
            "AABB_AND_GEOMETRIC_SEPARATION_LOWER_BOUNDS" if geometric_gap_used
            else "NONOVERLAPPING_AXIS_ALIGNED_BOUNDING_BOXES"
        ),
    }

