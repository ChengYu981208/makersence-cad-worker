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


def _boxes_separated(a: dict[str, Any], b: dict[str, Any], gap: float) -> bool:
    aa = a["pose_bbox_mm"]
    bb = b["pose_bbox_mm"]
    # AABB separation is conservative: overlap rejects an arrangement even if
    # the detailed meshes might not collide. A positive requested gap is proved.
    return any(
        aa["max"][i] + gap <= bb["min"][i] + 1e-7
        or bb["max"][i] + gap <= aa["min"][i] + 1e-7
        for i in range(3)
    )


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

    arrangements: list[tuple[float, tuple[int, ...]]] = []
    nodes = 0
    exhausted = False

    def record(indices: tuple[int, ...], score: float) -> None:
        arrangements.append((score / expected, indices))
        arrangements.sort(key=lambda x: x[0], reverse=True)
        del arrangements[4:]

    def visit(start: int, chosen: tuple[int, ...], score: float) -> None:
        nonlocal nodes, exhausted
        if nodes >= max_search_nodes:
            exhausted = True
            return
        nodes += 1
        if len(chosen) == expected:
            record(chosen, score)
            return
        remaining = expected - len(chosen)
        if len(unique) - start < remaining:
            return
        for index in range(start, len(unique)):
            if len(unique) - index < remaining:
                break
            candidate = unique[index]
            if all(_boxes_separated(candidate, unique[prior], gap) for prior in chosen):
                visit(index + 1, chosen + (index,), score + candidate["score"])
            if exhausted:
                return

    visit(0, (), 0.0)
    best = arrangements[0] if arrangements else None
    second = arrangements[1] if len(arrangements) > 1 else None
    score_gap = (best[0] - second[0]) if best and second else None
    required_gap = max(uniqueness_absolute_gap, abs(best[0]) * uniqueness_relative_gap) if best else None
    unique_solution = bool(best and not exhausted and (second is None or score_gap >= required_gap))
    reason = "unique_nonoverlapping_arrangement" if unique_solution else (
        "arrangement_search_budget_exhausted" if exhausted else
        "arrangement_not_unique" if best else "no_supported_nonoverlapping_arrangement"
    )

    alternatives = []
    for score, indices in arrangements[:3]:
        alternatives.append({"mean_score": round(score, 4), "instance_transforms": [unique[i] for i in indices]})
    selected = [unique[i] for i in best[1]] if unique_solution and best else []
    return {
        "status": "ALIGNED" if unique_solution else "REVIEW_REQUIRED",
        "confidence": "HIGH" if unique_solution else "MEDIUM" if best and not exhausted else "LOW",
        "expected_instances": expected,
        "matched_instances": len(best[1]) if best else 0,
        "instance_transforms": selected,
        "arrangement_unique": unique_solution,
        "instance_solution_complete": unique_solution and len(selected) == expected,
        "instance_solution_method": "BOUNDED_NONOVERLAPPING_AABB_ARRANGEMENT_V1",
        "reason": reason,
        "candidate_arrangement_search_complete": not exhausted,
        "uniqueness_basis": "SEARCHED_CANDIDATE_POSES_ONLY",
        "search_nodes": nodes,
        "supported_pose_count": len(unique),
        "minimum_instance_gap_mm": gap,
        "arrangement_score_gap": round(score_gap, 4) if score_gap is not None else None,
        "arrangement_score_gap_required": round(required_gap, 4) if required_gap is not None else None,
        "candidate_arrangements": alternatives,
        "separation_proof": "NONOVERLAPPING_AXIS_ALIGNED_BOUNDING_BOXES",
    }

