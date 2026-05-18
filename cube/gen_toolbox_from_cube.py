"""Generate cube_* Toolbox tool entries from cube/model/cubes/*.yml + cube/codegen.yml.

Why this exists: the Cube YAML is the authoring authority for measures,
dimensions, and the underlying table they live on. The `cube_*` tools
in tools.yaml are the agent surface mirroring the same model. Without
runtime federation (see NOTES.md), the SQL in those tools needs to be
kept in sync with the cube YAML by hand — which is what this script
automates. Edit cube/model/cubes/*.yml (or cube/codegen.yml for the
slice list), rerun the script, the tools.yaml block between the
sentinels is regenerated.

Run (default — in-place rewrite of tools.yaml between sentinels):

    uv run --no-project --with pyyaml python3 cube/gen_toolbox_from_cube.py

Stdout-only mode (for review or piping):

    uv run --no-project --with pyyaml python3 cube/gen_toolbox_from_cube.py --stdout

What stays hand-maintained:

- The `analytics_cube_backed` toolset entry in tools.yaml. After
  adding a slice, add its tool_name to that toolset by hand.
- Tools that need bound `parameters:` (filter pushdowns etc.) — this
  codegen only emits parameterless aggregates today. Hand-author
  parametric variants alongside.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import textwrap

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CUBE_MODEL_DIR = REPO_ROOT / "cube" / "model" / "cubes"
CODEGEN_CFG = REPO_ROOT / "cube" / "codegen.yml"
TOOLS_YAML = REPO_ROOT / "tools.yaml"

SENTINEL_BEGIN = "  # BEGIN cube-generated"
SENTINEL_END = "  # END cube-generated"

# Cube measure type → (aggregate function, argument template). The
# template's `{sql}` placeholder is filled with the measure's `sql:`
# fragment from the cube YAML (e.g., `amount` for sales.revenue).
AGGREGATE_BY_TYPE: dict[str, tuple[str, str]] = {
    "sum":            ("SUM",   "{sql}"),
    "count":          ("COUNT", "*"),
    "count_distinct": ("COUNT", "DISTINCT {sql}"),
    "countDistinct":  ("COUNT", "DISTINCT {sql}"),  # Cube accepts both spellings
    "avg":            ("AVG",   "{sql}"),
    "min":            ("MIN",   "{sql}"),
    "max":            ("MAX",   "{sql}"),
}


def load_cubes() -> dict[str, dict]:
    """Return {cube_name: cube_dict} merged across every YAML in cube/model/cubes/."""
    cubes: dict[str, dict] = {}
    for f in sorted(CUBE_MODEL_DIR.glob("*.yml")):
        doc = yaml.safe_load(f.read_text()) or {}
        for c in doc.get("cubes", []):
            if c["name"] in cubes:
                raise ValueError(f"duplicate cube {c['name']!r} in {f}")
            cubes[c["name"]] = c
    return cubes


def render_measure(cube_name: str, m: dict) -> str:
    """Render one measure as a SELECT column with the Cube-style alias."""
    try:
        fn, arg_tmpl = AGGREGATE_BY_TYPE[m["type"]]
    except KeyError as exc:
        raise ValueError(
            f"unsupported measure type {m['type']!r} on {cube_name}.{m['name']!r} "
            f"— extend AGGREGATE_BY_TYPE in {__file__} to add it"
        ) from exc
    arg = arg_tmpl.format(sql=m.get("sql", ""))
    return f'{fn}({arg}) AS "{cube_name}.{m["name"]}"'


def render_dimension(cube_name: str, d: dict) -> str:
    """Render one dimension as a SELECT column with the Cube-style alias."""
    return f'{d["sql"]} AS "{cube_name}.{d["name"]}"'


def remote_table(cube: dict, attach_alias: str) -> str:
    """Translate the cube's sql_table to the Toolbox ATTACH alias.

    Cube YAML expresses the warehouse-side name (e.g. `main.sales`); the
    Toolbox-side in-process DuckDB sees the same table under the source's
    attach_alias (e.g. `remote.sales`). The cube's sql_table can be
    either `<table>` or `<schema>.<table>`; we always take the last
    segment as the table name.
    """
    raw = cube["sql_table"]
    table_only = raw.split(".")[-1]
    return f"{attach_alias}.{table_only}"


def render_order_by(cube_name: str, order_by: str | None) -> str:
    """Translate codegen's `order_by: revenue desc` into a SQL ORDER BY line.

    The codegen syntax is `<measure_or_dimension> [asc|desc]`. The
    output uses the Cube-style alias so the ORDER BY column matches a
    SELECT alias and works without a subquery.
    """
    if not order_by:
        return ""
    parts = order_by.strip().split()
    if not parts or len(parts) > 2:
        raise ValueError(f"bad order_by clause: {order_by!r}")
    member = parts[0]
    direction = parts[1].upper() if len(parts) == 2 else "ASC"
    if direction not in {"ASC", "DESC"}:
        raise ValueError(f"order_by direction must be asc or desc, got {direction!r}")
    return f'      ORDER BY "{cube_name}.{member}" {direction}\n'


def emit_tool(slice_cfg: dict, cubes: dict, target_source: str, attach_alias: str) -> str:
    """Render one duckdb-sql tool entry as a YAML snippet (2-space indent)."""
    cube_name = slice_cfg["cube"]
    if cube_name not in cubes:
        raise ValueError(
            f"slice {slice_cfg['tool_name']!r} references unknown cube {cube_name!r}; "
            f"known cubes: {sorted(cubes)}"
        )
    cube = cubes[cube_name]
    measures_by_name = {m["name"]: m for m in cube.get("measures", [])}
    dimensions_by_name = {d["name"]: d for d in cube.get("dimensions", [])}

    measures = [measures_by_name[name] for name in slice_cfg["measures"]]
    dimensions = [dimensions_by_name[name] for name in slice_cfg["dimensions"]]

    select_cols = (
        [render_dimension(cube_name, d) for d in dimensions]
        + [render_measure(cube_name, m) for m in measures]
    )
    group_by_cols = [d["sql"] for d in dimensions]

    # Description: optional human-authored prose prefix from
    # codegen.yml (since prose can't be generated from cube YAML
    # alone), then a generated suffix that names the cube + members.
    desc_measures = ", ".join(f"{cube_name}.{m['name']}" for m in measures)
    desc_dimensions = ", ".join(f"{cube_name}.{d['name']}" for d in dimensions)
    suffix = (
        f"Generated from the `{cube_name}` cube — measures: {desc_measures}; "
        f"dimensions: {desc_dimensions}. Returns the same rows Cube would "
        f"for that {{measures, dimensions}} query against the same warehouse "
        f"data. To change, edit cube/model/cubes/{cube_name}.yml or the slice "
        f"in cube/codegen.yml and rerun cube/gen_toolbox_from_cube.py."
    )
    prefix = (slice_cfg.get("description_prefix") or "").strip()
    description = f"{prefix} {suffix}" if prefix else suffix

    indent = " " * 8  # SELECT-column indent inside the statement block
    select_clause = (",\n" + indent).join(select_cols)
    group_clause = ", ".join(group_by_cols)

    statement_lines = [
        "      SELECT",
        f"{indent}{select_clause}",
        f"      FROM {remote_table(cube, attach_alias)}",
    ]
    if group_clause:
        statement_lines.append(f"      GROUP BY {group_clause}")
    order_line = render_order_by(cube_name, slice_cfg.get("order_by"))
    if order_line:
        statement_lines.append(order_line.rstrip("\n"))
    statement_block = "\n".join(statement_lines)

    # Folded scalar (`>-`) keeps the description on one logical line in
    # YAML output but lets us hard-wrap the source for readability.
    desc_wrapped = textwrap.fill(
        description, width=68, initial_indent="      ", subsequent_indent="      "
    )

    return (
        f"  {slice_cfg['tool_name']}:\n"
        f"    type: duckdb-sql\n"
        f"    source: {target_source}\n"
        f"    description: >-\n"
        f"{desc_wrapped}\n"
        f"    parameters: []\n"
        f"    statement: |\n"
        f"{statement_block}\n"
    )


def render_snippet(cfg: dict, cubes: dict) -> str:
    """Render the full sentinel-wrapped block, ready to drop into tools.yaml."""
    target_source = cfg["target_source"]
    attach_alias = cfg["attach_alias"]
    toolset_name = cfg.get("toolset_name", "")
    slices = cfg.get("slices", []) or []

    header = [
        SENTINEL_BEGIN + " — DO NOT EDIT BY HAND",
        "  # Regenerate after changing cube/model/cubes/*.yml or cube/codegen.yml:",
        "  #   uv run --no-project --with pyyaml python3 cube/gen_toolbox_from_cube.py",
    ]
    if toolset_name:
        header.append(
            f"  # Toolset to add new tool_names to (hand-maintained): {toolset_name}"
        )
    parts = ["\n".join(header)]
    for slc in slices:
        parts.append("")
        parts.append(emit_tool(slc, cubes, target_source, attach_alias))
    parts.append(SENTINEL_END)
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the regenerated block to stdout instead of editing tools.yaml.",
    )
    args = parser.parse_args()

    cubes = load_cubes()
    cfg = yaml.safe_load(CODEGEN_CFG.read_text())
    snippet = render_snippet(cfg, cubes)

    if args.stdout:
        sys.stdout.write(snippet)
        return 0

    src = TOOLS_YAML.read_text()
    pattern = re.compile(
        rf"^{re.escape(SENTINEL_BEGIN)}.*?^{re.escape(SENTINEL_END)}\s*\n",
        re.DOTALL | re.MULTILINE,
    )
    if not pattern.search(src):
        sys.stderr.write(
            f"ERROR: sentinels not found in {TOOLS_YAML}.\n"
            f"Add a stub block somewhere in tools.yaml that this script can replace:\n\n"
            f"{SENTINEL_BEGIN}\n{SENTINEL_END}\n\n"
            f"Then rerun the script (it will fill the block in).\n"
        )
        return 1
    new_src = pattern.sub(lambda _m: snippet, src)
    TOOLS_YAML.write_text(new_src)
    n = len(cfg.get("slices", []) or [])
    sys.stderr.write(
        f"wrote {n} generated tool(s) between sentinels in {TOOLS_YAML.relative_to(REPO_ROOT)}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
