from __future__ import annotations
import cadquery as cq
from app.adapters.base import num, pos


def rounded_box(width: float, depth: float, height: float, radius: float = 2.0) -> cq.Workplane:
    width, depth, height = pos(width, 40), pos(depth, 30), pos(height, 10)
    radius = max(0.0, min(float(radius or 0), width/2-0.01, depth/2-0.01))
    wp = cq.Workplane("XY").rect(width, depth).extrude(height)
    if radius > 0.05:
        try:
            wp = wp.edges("|Z").fillet(radius)
        except Exception:
            pass
    return wp


def open_box(width: float, depth: float, height: float, wall: float = 2.4, floor: float = 2.4, radius: float = 2.0) -> cq.Workplane:
    width, depth, height = pos(width, 70), pos(depth, 50), pos(height, 30)
    wall, floor = pos(wall, 2.4, .8), pos(floor, wall, .8)
    if width <= wall*2 or depth <= wall*2 or height <= floor:
        raise ValueError("open_box dimensions are smaller than required walls")
    outer = rounded_box(width, depth, height, radius)
    inner_h = height - floor + 0.02
    inner = cq.Workplane("XY").workplane(offset=floor).rect(width-2*wall, depth-2*wall).extrude(inner_h)
    return outer.cut(inner)


def frame_rect(outer_w: float, outer_h: float, inner_w: float, inner_h: float, thickness: float) -> cq.Workplane:
    outer = cq.Workplane("XY").rect(outer_w, outer_h).extrude(thickness)
    inner = cq.Workplane("XY").rect(inner_w, inner_h).extrude(thickness + .2)
    return outer.cut(inner)


def cylinder(diameter: float, height: float) -> cq.Workplane:
    return cq.Workplane("XY").circle(pos(diameter, 6)/2).extrude(pos(height, 2))


def ring(outer_d: float, inner_d: float, height: float) -> cq.Workplane:
    outer = cq.Workplane("XY").circle(pos(outer_d, 8)/2).extrude(pos(height, 2))
    inner = cq.Workplane("XY").circle(pos(inner_d, 5)/2).extrude(pos(height, 2)+.2)
    return outer.cut(inner)


def box(width: float, depth: float, height: float) -> cq.Workplane:
    return cq.Workplane("XY").box(pos(width, 10), pos(depth, 10), pos(height, 2), centered=(True, True, False))
