from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import cadquery as cq

@dataclass
class Part:
    name: str
    role: str
    shape: cq.Shape
    color: str = "#8a8a8a"
    physical_separate: bool = True
    editable_separate: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class AdapterResult:
    adapter: str
    parts: list[Part]
    protected_interface_hash: str | None = None
    assembly_contract: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def require_parts(self) -> None:
        if not self.parts:
            raise ValueError("adapter produced no printable parts")
        for p in self.parts:
            if p.shape is None or p.shape.isNull():
                raise ValueError(f"part {p.name} has null geometry")
