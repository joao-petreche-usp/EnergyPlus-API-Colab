"""
Block-level IDF patcher for EnergyPlus models.

Parses the IDF into object blocks keyed by (obj_type, name), allows setting
specific field values by positional index, and writes the patched IDF.

This is more robust than line-scanning because it locates fields by their
sequential position within the block, not by matching inline comment text.
"""

from pathlib import Path


def _strip_comment(line: str) -> str:
    idx = line.find('!')
    return line[:idx] if idx >= 0 else line


def _parse_blocks(lines: list[str]) -> dict[tuple[str, str], list[int]]:
    """
    Parse IDF lines into {(obj_type_lower, name): [field_line_idx, ...]} dict.

    Object type lines are identified by ≤2 characters of leading whitespace and
    a trailing comma after stripping inline comments (standard EnergyPlus IDF
    formatting). Fields have deeper indentation and end with a comma (non-final)
    or semicolon (final field).

    Each value in the returned dict is an ordered list of line indices, one per
    field (index 0 = Name field, index 1 = second field, etc.).
    """
    result: dict[tuple[str, str], list[int]] = {}
    current_type: str | None = None
    field_lines: list[int] = []

    for i, line in enumerate(lines):
        content = _strip_comment(line).strip()
        if not content:
            continue

        indent = len(line) - len(line.lstrip())

        if content.endswith(',') and indent <= 2:
            # Object type declaration line (e.g., "  Material,")
            current_type = content.rstrip(',').strip()
            field_lines = []
        elif current_type is not None and (content.endswith(',') or content.endswith(';')):
            # Field value line — collect it
            field_lines.append(i)
            if content.endswith(';'):
                # Last field: store completed block keyed by (type, name)
                name_raw = _strip_comment(lines[field_lines[0]]).strip().rstrip(',').rstrip(';').strip()
                result[(current_type.lower(), name_raw)] = list(field_lines)
                current_type = None
                field_lines = []

    return result


def _set_field_value(line: str, new_value: str) -> str:
    """Replace the value in an IDF field line, preserving indentation, terminator, and comment."""
    comment_idx = line.find('!')
    comment = line[comment_idx:] if comment_idx >= 0 else ''
    pre = line[:comment_idx] if comment_idx >= 0 else line

    indent = len(pre) - len(pre.lstrip())
    indent_str = pre[:indent]
    term = ';' if pre.rstrip().endswith(';') else ','

    value_and_term = f'{indent_str}{new_value}{term}'
    if comment:
        return f'{value_and_term:<33} {comment}'
    return f'{value_and_term}\n'


class IDFPatcher:
    """
    Block-level IDF patcher.

    Load a base IDF once, then for each candidate: call reset(), apply
    set_* methods, then call save() to write the patched file.

    Example::

        patcher = IDFPatcher('data/5ZoneAirCooled_Opt.idf')
        patcher.reset()
        patcher.set_material_thickness('IN02', wall_r * 0.043)
        patcher.set_material_thickness('IN46', roof_r * 0.023)
        patcher.set_north_axis(orientation_deg)
        patcher.save('output/candidate_001.idf')
    """

    # Zero-based field indices within each EnergyPlus object type.
    # Field 0 is always the Name field.
    _MATERIAL_THICKNESS_IDX = 2          # Material: Name(0) Roughness(1) Thickness(2) ...
    _NOMASS_RESISTANCE_IDX = 2           # Material:NoMass: Name(0) Roughness(1) ThermalResistance(2) ...
    _BUILDING_NORTH_AXIS_IDX = 1         # Building: Name(0) NorthAxis(1) Terrain(2) ...
    _FENESTRATION_CONSTRUCTION_IDX = 2   # FenestrationSurface:Detailed: Name(0) Type(1) Construction(2) ...

    def __init__(self, base_idf_path: str | Path):
        self.base_idf_path = Path(base_idf_path)
        if not self.base_idf_path.exists():
            raise FileNotFoundError(f'Base IDF not found: {self.base_idf_path}')
        with open(self.base_idf_path, 'r', encoding='utf-8') as f:
            self._base_lines: list[str] = f.readlines()
        self._blocks = _parse_blocks(self._base_lines)
        self._lines: list[str] = list(self._base_lines)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset working copy to the original base IDF before patching a new candidate."""
        self._lines = list(self._base_lines)

    def save(self, output_path: str | Path) -> None:
        """Write the current working copy to output_path, creating parent dirs as needed."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.writelines(self._lines)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_objects(self, obj_type: str) -> list[str]:
        """Return the names of all parsed objects of the given IDF type (case-insensitive)."""
        prefix = obj_type.lower()
        return [name for (t, name) in self._blocks if t == prefix]

    def field_count(self, obj_type: str, name: str) -> int:
        """Return the number of fields in a named IDF object."""
        key = (obj_type.lower(), name)
        if key not in self._blocks:
            raise KeyError(f'IDF object not found: {obj_type!r} named {name!r}')
        return len(self._blocks[key])

    # ------------------------------------------------------------------
    # Generic low-level setter (used by higher-level methods below)
    # ------------------------------------------------------------------

    def _set_field(self, obj_type: str, name: str, field_idx: int, new_value: str) -> None:
        """
        Set the value of a field identified by object type, object name, and
        zero-based field position (0 = Name field, 1 = second field, ...).
        """
        key = (obj_type.lower(), name)
        if key not in self._blocks:
            raise KeyError(f'IDF object not found: {obj_type!r} named {name!r}')
        indices = self._blocks[key]
        if field_idx >= len(indices):
            raise IndexError(
                f'Field index {field_idx} out of range for {obj_type!r} {name!r} '
                f'(object has {len(indices)} fields)'
            )
        line_idx = indices[field_idx]
        self._lines[line_idx] = _set_field_value(self._lines[line_idx], new_value)

    # ------------------------------------------------------------------
    # Physical injection helpers (M1.3 / M1.4 / M1.6)
    # ------------------------------------------------------------------

    def set_material_thickness(self, material_name: str, thickness_m: float) -> None:
        """Set the Thickness field of a Material object (field index 2)."""
        self._set_field('Material', material_name, self._MATERIAL_THICKNESS_IDX, f'{thickness_m:.6f}')

    def set_nomass_resistance(self, material_name: str, resistance_m2kw: float) -> None:
        """Set the Thermal Resistance field of a Material:NoMass object (field index 2)."""
        self._set_field('Material:NoMass', material_name, self._NOMASS_RESISTANCE_IDX, f'{resistance_m2kw:.6f}')

    def set_north_axis(self, degrees: float, building_name: str = 'Building') -> None:
        """Set the Building North Axis field (field index 1) on the named Building object."""
        self._set_field('Building', building_name, self._BUILDING_NORTH_AXIS_IDX, str(float(degrees)))

    def set_fenestration_construction(self, surface_name: str, construction_name: str) -> None:
        """Swap the Construction Name field (field index 2) of a FenestrationSurface:Detailed."""
        self._set_field(
            'FenestrationSurface:Detailed',
            surface_name,
            self._FENESTRATION_CONSTRUCTION_IDX,
            construction_name,
        )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys

    base_idf = Path(__file__).resolve().parents[2] / 'data' / '5ZoneAirCooled_Opt.idf'
    if not base_idf.exists():
        print(f'Base IDF not found: {base_idf}')
        sys.exit(1)

    patcher = IDFPatcher(base_idf)
    print(f'Parsed {len(patcher._blocks)} IDF objects.\n')

    expected = [
        ('Material',    'IN02'),
        ('Material',    'IN46'),
        ('Building',    'Building'),
        ('Construction', 'ROOF-1'),
        ('Construction', 'WALL-1'),
    ]
    all_ok = True
    for obj_type, name in expected:
        key = (obj_type.lower(), name)
        if key in patcher._blocks:
            n = len(patcher._blocks[key])
            print(f'  OK  {obj_type} "{name}" — {n} fields')
        else:
            print(f'  MISSING  {obj_type} "{name}"')
            all_ok = False

    windows = patcher.list_objects('FenestrationSurface:Detailed')
    print(f'\nFenestrationSurface:Detailed ({len(windows)}): {windows}')

    # Smoke test: patch IN02 thickness and North Axis, save, verify
    out = Path(__file__).resolve().parents[2] / 'output' / '_test_idf_patcher.idf'
    patcher.reset()
    patcher.set_material_thickness('IN02', 2.5 * 0.043)   # R=2.5 → t=0.1075 m
    patcher.set_material_thickness('IN46', 4.0 * 0.023)   # R=4.0 → t=0.0920 m
    patcher.set_north_axis(90.0)
    patcher.save(out)
    print(f'\nSmoke test IDF written to: {out}')

    if all_ok:
        print('\nAll checks passed.')
    else:
        print('\nSome objects were not found — check IDF format.')
        sys.exit(1)
