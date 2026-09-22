from __future__ import annotations
from typing import Any
from app.adapters.interface_locked import InterfaceLockedAdapter
from app.adapters.interface_mechanism import InterfaceMechanismAdapter
from app.adapters.mechanism import MechanismAdapter
from app.adapters.legacy import LegacyAdapter

NEW={
    'INTERFACE_LOCKED_CAD':InterfaceLockedAdapter(),
    'INTERFACE_MECHANISM_CAD':InterfaceMechanismAdapter(),
    'MECHANISM_CAD':MechanismAdapter(),
}
LEGACY=LegacyAdapter()

def resolve_adapter(payload:dict[str,Any]):
    recipe=payload.get('cad_contract') or payload.get('recipe') or payload.get('parameters',{}).get('geometry_recipe') or {}
    strategy=str(recipe.get('generation_strategy') or recipe.get('adapter') or payload.get('generation_strategy') or '').upper()
    if strategy in NEW:return NEW[strategy],recipe
    return LEGACY,recipe
