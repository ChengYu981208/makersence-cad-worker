"""Transform-aware, bounded 3MF build/component mesh loader for counterpart alignment."""

from __future__ import annotations

import math
import zipfile
import xml.etree.ElementTree as ET
from typing import Any


_IDENTITY = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)
_UNIT_MM = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "meter": 1000.0,
}


def _tag(element: ET.Element) -> str:
    return str(element.tag).rsplit("}", 1)[-1]


def _normal_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").lstrip("/")


def _path_attribute(element: ET.Element) -> str | None:
    for key, value in element.attrib.items():
        if key == "path" or key.endswith("}path"):
            return _normal_path(value)
    return None


def _matrix(raw: Any, unit_scale_mm: float) -> tuple[tuple[float, ...], ...]:
    if not raw:
        return _IDENTITY
    try:
        values = [float(item) for item in str(raw).split()]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("3MF_ALIGNMENT_TRANSFORM_INVALID") from exc
    if len(values) != 12 or not all(math.isfinite(value) for value in values):
        raise ValueError("3MF_ALIGNMENT_TRANSFORM_INVALID")
    return (
        (values[0], values[3], values[6], values[9] * unit_scale_mm),
        (values[1], values[4], values[7], values[10] * unit_scale_mm),
        (values[2], values[5], values[8], values[11] * unit_scale_mm),
        (0.0, 0.0, 0.0, 1.0),
    )


def _multiply(left: tuple[tuple[float, ...], ...], right: tuple[tuple[float, ...], ...]):
    return tuple(
        tuple(sum(left[row][k] * right[k][column] for k in range(4)) for column in range(4))
        for row in range(4)
    )


def _point(point: tuple[float, float, float], transform: tuple[tuple[float, ...], ...]):
    x, y, z = point
    return (
        transform[0][0] * x + transform[0][1] * y + transform[0][2] * z + transform[0][3],
        transform[1][0] * x + transform[1][1] * y + transform[1][2] * z + transform[1][3],
        transform[2][0] * x + transform[2][1] * y + transform[2][2] * z + transform[2][3],
    )


def load_3mf_assembly_mesh(
    path: str,
    *,
    root_object_ids: list[str] | None = None,
    max_vertices: int = 180_000,
    max_triangles: int = 320_000,
) -> dict[str, Any]:
    """Expand selected reviewed build roots, or all roots, with component transforms.

    User-reviewed root IDs can exclude unrelated print-plate objects from
    counterpart alignment. Limits apply after selected assembly instances are
    expanded. Oversized or unresolved selections fail closed.
    """
    if max_vertices < 4 or max_triangles < 4:
        raise ValueError("3MF_ALIGNMENT_BUDGET_INVALID")

    scales: dict[str, float] = {}
    docs: dict[str, dict[str, Any]] = {}
    raw_vertices = 0
    raw_triangles = 0

    with zipfile.ZipFile(path, "r") as archive:
        if archive.testzip() is not None:
            raise ValueError("3MF_ALIGNMENT_ARCHIVE_DAMAGED")
        model_paths = [
            _normal_path(name)
            for name in archive.namelist()
            if name.lower().endswith(".model") and not name.lower().startswith("metadata/")
        ]
        if not model_paths:
            raise ValueError("3MF_ALIGNMENT_MODEL_MISSING")
        main_path = next((name for name in model_paths if name.lower() == "3d/3dmodel.model"), model_paths[0])

        for model_path in model_paths:
            unit_scale = 1.0
            current: dict[str, Any] | None = None
            document: dict[str, Any] = {"objects": {}, "build": []}
            stack: list[str] = []
            with archive.open(model_path, "r") as model_file:
                for event, element in ET.iterparse(model_file, events=("start", "end")):
                    name = _tag(element)
                    if event == "start":
                        stack.append(name)
                        if name == "model":
                            unit_scale = _UNIT_MM.get(str(element.attrib.get("unit", "millimeter")).lower(), 1.0)
                        elif name == "object":
                            current = {
                                "id": str(element.attrib.get("id") or ""),
                                "name": str(element.attrib.get("name") or "Object"),
                                "vertices": [],
                                "triangles": [],
                                "components": [],
                            }
                        elif name == "component" and current is not None:
                            current["components"].append(
                                {
                                    "id": str(element.attrib.get("objectid") or ""),
                                    "path": _path_attribute(element) or model_path,
                                    "transform": _matrix(element.attrib.get("transform"), unit_scale),
                                }
                            )
                        elif name == "item" and model_path == main_path and "build" in stack:
                            document["build"].append(
                                {
                                    "id": str(element.attrib.get("objectid") or ""),
                                    "path": model_path,
                                    "transform": _matrix(element.attrib.get("transform"), unit_scale),
                                }
                            )
                        continue

                    if current is not None and name == "vertex":
                        try:
                            point = (
                                float(element.attrib.get("x")) * unit_scale,
                                float(element.attrib.get("y")) * unit_scale,
                                float(element.attrib.get("z")) * unit_scale,
                            )
                        except (TypeError, ValueError, OverflowError) as exc:
                            raise ValueError("3MF_ALIGNMENT_VERTEX_INVALID") from exc
                        if not all(math.isfinite(value) for value in point):
                            raise ValueError("3MF_ALIGNMENT_VERTEX_INVALID")
                        current["vertices"].append(point)
                    elif current is not None and name == "triangle":
                        try:
                            triangle = (
                                int(element.attrib.get("v1")),
                                int(element.attrib.get("v2")),
                                int(element.attrib.get("v3")),
                            )
                        except (TypeError, ValueError, OverflowError) as exc:
                            raise ValueError("3MF_ALIGNMENT_TRIANGLE_INVALID") from exc
                        current["triangles"].append(triangle)
                    elif name == "object" and current is not None:
                        for a, b, c in current["triangles"]:
                            if min(a, b, c) < 0 or max(a, b, c) >= len(current["vertices"]) or len({a, b, c}) != 3:
                                raise ValueError("3MF_ALIGNMENT_TRIANGLE_INDEX_INVALID")
                        raw_vertices += len(current["vertices"])
                        raw_triangles += len(current["triangles"])
                        if raw_vertices > max_vertices * 4 or raw_triangles > max_triangles * 8:
                            raise ValueError("3MF_ALIGNMENT_RAW_MESH_BUDGET_EXCEEDED")
                        document["objects"][current["id"]] = current
                        current = None
                    element.clear()
                    if stack:
                        stack.pop()
            docs[model_path] = document
            scales[model_path] = unit_scale

    build = docs.get(main_path, {}).get("build") or []
    identity = _IDENTITY
    if not build:
        referenced = {
            (_normal_path(component.get("path") or model_path), str(component.get("id") or ""))
            for model_path, document in docs.items()
            for obj in document["objects"].values()
            for component in obj.get("components") or []
        }
        build = [
            {"id": object_id, "path": model_path, "transform": identity}
            for model_path, document in docs.items()
            for object_id in document["objects"]
            if (model_path, str(object_id)) not in referenced
        ]
    if not build:
        raise ValueError("3MF_ALIGNMENT_BUILD_GRAPH_EMPTY")

    selected_ids: set[str] | None = None
    unselected_root_count = 0
    if root_object_ids is not None:
        selected_ids = {str(value).strip() for value in root_object_ids if str(value).strip()}
        if not selected_ids:
            raise ValueError("3MF_ALIGNMENT_SELECTED_ROOT_IDS_EMPTY")
        available_ids = {str(root.get("id") or "") for root in build}
        missing_ids = sorted(selected_ids - available_ids)
        if missing_ids:
            raise ValueError("3MF_ALIGNMENT_SELECTED_ROOT_MISSING:" + ",".join(missing_ids))
        original_root_count = len(build)
        build = [root for root in build if str(root.get("id") or "") in selected_ids]
        unselected_root_count = original_root_count - len(build)

    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    included_parts: list[str] = []
    exact_min = [float("inf")] * 3
    exact_max = [float("-inf")] * 3

    def collect(model_path: str, object_id: str, world, ancestry: frozenset[tuple[str, str]]) -> None:
        key = (_normal_path(model_path), str(object_id))
        if key in ancestry:
            raise ValueError("3MF_ALIGNMENT_COMPONENT_CYCLE")
        obj = (docs.get(key[0]) or {}).get("objects", {}).get(key[1])
        if obj is None:
            raise ValueError("3MF_ALIGNMENT_COMPONENT_MISSING:" + key[0] + ":" + key[1])
        next_ancestry = ancestry | {key}
        raw = obj["vertices"]
        faces = obj["triangles"]
        if raw and faces:
            if len(vertices) + len(raw) > max_vertices or len(triangles) + len(faces) > max_triangles:
                raise ValueError("3MF_ALIGNMENT_EXPANDED_MESH_BUDGET_EXCEEDED")
            base = len(vertices)
            for point in raw:
                world_point = _point(point, world)
                vertices.append(world_point)
                for axis in range(3):
                    exact_min[axis] = min(exact_min[axis], world_point[axis])
                    exact_max[axis] = max(exact_max[axis], world_point[axis])
            for a, b, c in faces:
                triangles.append((base + a, base + b, base + c))
            included_parts.append(key[0] + "#" + key[1])
        for component in obj.get("components") or []:
            child_path = _normal_path(component.get("path") or key[0])
            collect(
                child_path,
                str(component.get("id") or ""),
                _multiply(world, component["transform"]),
                next_ancestry,
            )

    for root in build:
        root_path = _normal_path(root.get("path") or main_path)
        collect(root_path, str(root.get("id") or ""), root["transform"], frozenset())

    if len(vertices) < 4 or len(triangles) < 4:
        raise ValueError("3MF_ALIGNMENT_MESH_EMPTY")
    dimensions = [exact_max[i] - exact_min[i] for i in range(3)]
    if any(not math.isfinite(value) or value <= 0 for value in dimensions):
        raise ValueError("3MF_ALIGNMENT_BOUNDS_INVALID")
    return {
        "name": "3MF printable assembly",
        "vertices": vertices,
        "triangles": triangles,
        "bbox": {
            "min": exact_min,
            "max": exact_max,
            "dimensions": dimensions,
            "center": [(exact_min[i] + exact_max[i]) / 2.0 for i in range(3)],
        },
        "assembly": {
            "root_count": len(build),
            "total_root_count": len(build) + unselected_root_count,
            "unselected_root_count": unselected_root_count,
            "selected_root_object_ids": sorted(selected_ids) if selected_ids is not None else None,
            "part_instance_count": len(included_parts),
            "source_structure": "TRANSFORM_AWARE_3MF_BUILD_GRAPH",
            "included_part_ids": included_parts,
        },
    }
