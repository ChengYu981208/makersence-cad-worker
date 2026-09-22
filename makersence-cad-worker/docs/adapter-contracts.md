# Adapter contracts

## INTERFACE_LOCKED_CAD
Use when the product must fit an external system and the mating geometry is protected.

Required:
- `reference_asset_id`: uploaded via `POST /v1/reference-assets?format=3mf|stl`
- `generation_strategy: "INTERFACE_LOCKED_CAD"` or `adapter: "INTERFACE_LOCKED_CAD"`

Important optional inputs:
- `interface_axis`: `X|Y|Z`
- `interface_plane_mm`: section plane in source coordinates
- `protected_band_mm`: narrow functional band that may derive from the reference
- `interface_offset_mm`: explicit mating clearance, never inferred from overall scaling
- `redesign_offset_mm`, `wall_mm`, `body_length_mm`: MakerSence geometry outside the protected band
- `retention_features`: explicit lip/nose/stop geometry only when dimensions are known

The adapter hashes the protected section and returns `protected_interface_hash` in validation/manifest.
It fails closed if no reference asset is available.

## MECHANISM_CAD
Use for internal product motion without an external mating system.

Supported mechanism types in v3.0:
- `hinged_box`
- `print_in_place_hinge`
- `sliding_lid_box`
- `pivot_arm` / `hinge_stand`

All motion clearances must be explicit (`joint_clearance_mm`). The adapter does not invent an unknown joint.

## INTERFACE_MECHANISM_CAD
Use when both external compatibility and internal motion are required.

This composes `INTERFACE_LOCKED_CAD` with an explicit `MECHANISM_CAD` contract. The carrier can be fused to the protected interface body while moving parts remain separate.

Typical examples:
- phone stand that must fit a device envelope and also pivot
- launcher/controller accessory with a protected external interface plus internal motion

## Safety / design policy
- Never global-scale a fit-critical source to make it fit.
- Never use a silhouette plate or generic box as a fallback for unknown 3D geometry.
- Only a narrow functional interface may be reference-derived; redesignable appearance is rebuilt separately.
- If the worker cannot identify the protected interface region, return a structured engineering-prep failure instead of generating guessed geometry.
