from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any
from app.models import AdapterResult

class AdapterError(ValueError):
    pass

class CadAdapter(ABC):
    id: str = "BASE"

    @abstractmethod
    def build(self, contract: dict[str, Any], context: dict[str, Any]) -> AdapterResult:
        raise NotImplementedError


def num(v: Any, default: float) -> float:
    try:
        x = float(v)
        if x != x:
            return default
        return x
    except Exception:
        return default


def pos(v: Any, default: float, floor: float = 0.01) -> float:
    return max(floor, num(v, default))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AdapterError(message)
