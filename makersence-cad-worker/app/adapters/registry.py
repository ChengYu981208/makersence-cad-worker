from __future__ import annotations
from typing import Any
from app.adapters.base import AdapterError
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
LEGACY_STRATEGIES={'LEGACY','LEGACY_CAD'}

def resolve_adapter(payload:dict[str,Any]):
    recipe=payload.get('cad_contract') or payload.get('recipe') or payload.get('parameters',{}).get('geometry_recipe') or {}
    strategy=str(recipe.get('generation_strategy') or recipe.get('adapter') or payload.get('generation_strategy') or '').upper().strip()
    if strategy in NEW:
        return NEW[strategy],recipe
    if strategy in LEGACY_STRATEGIES:
        return LEGACY,recipe
    if not strategy:
        raise AdapterError(
            'explicit CAD generation_strategy/adapter is required; '
            'silent fallback to LEGACY/silhouette_plate is disabled'
        )
    raise AdapterError(f'unsupported CAD generation strategy: {strategy}')
