"""Compile the business ontology into quack-server/seed-ontology.sql.

Why this exists: the agent-facing ontology toolset (the `ontology_*`
tools in tools.yaml) answers "what does this business term mean, and
which cube/tool/model implements it?" from small tables served by
the sales Quack server: ontology_nodes, ontology_edges,
ontology_glossary, plus two PRECOMPUTED traversals —
ontology_bindings (the entity/term -> implementation closure) and
ontology_paths (all-pairs shortest paths). The traversals are
precomputed here rather than written as recursive CTEs in the tools
because (a) the graph is static — it rebuilds from git, so runtime
recursion buys nothing — and (b) a duckdb-sql tool statement may
reference an ATTACHed Quack table only once (single streaming scan),
and the parametrized alternative, push_down_to_remote, rejects bound
parameters. Single-scan lookups sidestep both limits.

Most of the graph already exists in structured form across the repo;
this script derives it deterministically and merges in the
hand-authored business layer:

  derived (never edit the output by hand):
    cube/model/cubes/*.yml      cubes, measures, dimensions, joins
    cube/codegen.yml            cube -> generated-tool edges
    tools.yaml                  tools, toolsets, sources, memberships
    dagster/dbt_project/models  dbt models, sources, ref()/source() lineage

  authored (the reviewed source of truth):
    ontology/entities/*.yaml    business entities + relations
    ontology/glossary.yaml      term definitions with provenance
    ontology/bindings.yaml      entity/term -> cube/tool/model edges

Bindings are VALIDATED: a binding that references a cube, tool,
measure, or dbt model that no longer exists fails the run — so a
rename surfaces in CI (make ontology-check), not at agent runtime.
Cubes no entity claims are reported as warnings: visible coverage
gaps, not silent ones.

Run (default — rewrite quack-server/seed-ontology.sql):

    uv run --no-project --with pyyaml python3 ontology/gen_ontology.py

Stdout-only mode (for review or piping):

    uv run --no-project --with pyyaml python3 ontology/gen_ontology.py --stdout
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ONTOLOGY_DIR = REPO_ROOT / "ontology"
CUBE_MODEL_DIR = REPO_ROOT / "cube" / "model" / "cubes"
CUBE_CODEGEN = REPO_ROOT / "cube" / "codegen.yml"
TOOLS_YAML = REPO_ROOT / "tools.yaml"
DBT_MODELS_DIR = REPO_ROOT / "dagster" / "dbt_project" / "models"
OUTPUT = REPO_ROOT / "quack-server" / "seed-ontology.sql"

REF_RE = re.compile(r"""{{\s*ref\(\s*['"](\w+)['"]\s*\)\s*}}""")
SOURCE_RE = re.compile(r"""{{\s*source\(\s*['"](\w+)['"]\s*,\s*['"](\w+)['"]\s*\)\s*}}""")


def oneline(text: str | None) -> str | None:
    """Collapse YAML block-scalar whitespace to a single line."""
    if not text:
        return None
    return " ".join(str(text).split()) or None


class Graph:
    """Accumulates nodes/edges/glossary; rejects duplicate node ids."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: set[tuple[str, str, str, str]] = set()
        self.glossary: list[dict] = []

    def add_node(self, kind: str, name: str, origin: str, *, description=None,
                 synonyms=None, owner=None, caveats=None) -> str:
        node_id = f"{kind}:{name}"
        if node_id in self.nodes:
            raise ValueError(f"duplicate ontology node {node_id!r}")
        self.nodes[node_id] = {
            "id": node_id, "kind": kind, "name": name,
            "description": oneline(description),
            "synonyms": ", ".join(synonyms) if synonyms else None,
            "owner": owner,
            "caveats": " | ".join(oneline(c) for c in caveats) if caveats else None,
            "origin": origin,
        }
        return node_id

    def add_edge(self, src: str, rel: str, dst: str, origin: str) -> None:
        self.edges.add((src, rel, dst, origin))


def load_derived(g: Graph) -> None:
    """Nodes + edges that are mechanically derivable from the repo."""
    # --- cube model: cubes, measures, public dimensions, joins -------
    cube_joins: list[tuple[str, str]] = []
    for f in sorted(CUBE_MODEL_DIR.glob("*.yml")):
        for cube in (yaml.safe_load(f.read_text()) or {}).get("cubes", []):
            cube_id = g.add_node("cube", cube["name"], "cube-model",
                                 description=cube.get("description"))
            for m in cube.get("measures", []):
                mid = g.add_node("measure", f'{cube["name"]}.{m["name"]}',
                                 "cube-model", description=m.get("description"))
                g.add_edge(cube_id, "has_measure", mid, "cube-model")
            for d in cube.get("dimensions", []):
                if d.get("public") is False:
                    continue
                did = g.add_node("dimension", f'{cube["name"]}.{d["name"]}',
                                 "cube-model", description=d.get("description"))
                g.add_edge(cube_id, "has_dimension", did, "cube-model")
            for j in cube.get("joins", []):
                cube_joins.append((cube_id, f'cube:{j["name"]}'))
    for src, dst in cube_joins:  # after all cube nodes exist
        if dst not in g.nodes:
            raise ValueError(f"cube join references unknown cube {dst!r}")
        g.add_edge(src, "joins", dst, "cube-model")

    # --- tools.yaml: sources, tools, toolsets -------------------------
    tools_cfg = yaml.safe_load(TOOLS_YAML.read_text())
    for name, src in sorted((tools_cfg.get("sources") or {}).items()):
        g.add_node("source", name, "tools-config",
                   description=f'Toolbox source ({src.get("type", "?")}), '
                               f'attach alias {src.get("attach_alias", "?")}.')
    for name, tool in sorted((tools_cfg.get("tools") or {}).items()):
        tid = g.add_node("tool", name, "tools-config",
                         description=tool.get("description"))
        if tool.get("source"):
            g.add_edge(tid, "wired_to", f'source:{tool["source"]}', "tools-config")
    for name, members in sorted((tools_cfg.get("toolsets") or {}).items()):
        tsid = g.add_node("toolset", name, "tools-config")
        for member in members:
            tool_id = f"tool:{member}"
            if tool_id not in g.nodes:
                raise ValueError(f"toolset {name!r} lists unknown tool {member!r}")
            g.add_edge(tool_id, "in_toolset", tsid, "tools-config")

    # --- cube/codegen.yml: cube -> generated tool ---------------------
    for slc in (yaml.safe_load(CUBE_CODEGEN.read_text()).get("slices") or []):
        cube_id, tool_id = f'cube:{slc["cube"]}', f'tool:{slc["tool_name"]}'
        for ref in (cube_id, tool_id):
            if ref not in g.nodes:
                raise ValueError(f"cube/codegen.yml references unknown {ref!r}")
        g.add_edge(cube_id, "exposed_by", tool_id, "cube-model")

    # --- dbt project: models, sources, ref()/source() lineage ---------
    schema = yaml.safe_load((DBT_MODELS_DIR / "schema.yml").read_text())
    for model in schema.get("models", []):
        g.add_node("dbt_model", model["name"], "dbt-project",
                   description=model.get("description"))
    sources = yaml.safe_load((DBT_MODELS_DIR / "sources.yml").read_text())
    for src in sources.get("sources", []):
        for table in src.get("tables", []):
            g.add_node("dbt_source", f'{src["name"]}.{table["name"]}',
                       "dbt-project", description=table.get("description"))
    for f in sorted(DBT_MODELS_DIR.glob("*.sql")):
        model_id = f"dbt_model:{f.stem}"
        if model_id not in g.nodes:  # model without a schema.yml entry
            g.add_node("dbt_model", f.stem, "dbt-project")
        sql = f.read_text()
        for ref in sorted(set(REF_RE.findall(sql))):
            g.add_edge(model_id, "depends_on", f"dbt_model:{ref}", "dbt-project")
        for src_name, table in sorted(set(SOURCE_RE.findall(sql))):
            g.add_edge(model_id, "depends_on",
                       f"dbt_source:{src_name}.{table}", "dbt-project")


def binding_ref(side: dict) -> str:
    """{cube: sales} -> 'cube:sales'; exactly one key per side."""
    if len(side) != 1:
        raise ValueError(f"binding side must have exactly one key: {side!r}")
    kind, name = next(iter(side.items()))
    return f"{kind}:{name}"


def load_authored(g: Graph) -> None:
    """The hand-authored business layer: entities, glossary, bindings."""
    entity_relations: list[tuple[str, str, str]] = []
    for f in sorted((ONTOLOGY_DIR / "entities").glob("*.yaml")):
        e = yaml.safe_load(f.read_text())
        eid = g.add_node("entity", e["entity"], "authored",
                         description=e.get("description"),
                         synonyms=e.get("synonyms"), owner=e.get("owner"),
                         caveats=e.get("caveats"))
        for r in e.get("relates_to", []):
            entity_relations.append((eid, r["rel"], f'entity:{r["entity"]}'))
    for src, rel, dst in entity_relations:  # after all entities exist
        if dst not in g.nodes:
            raise ValueError(f"{src!r} relates_to unknown entity {dst!r}")
        g.add_edge(src, rel, dst, "authored")

    for entry in yaml.safe_load((ONTOLOGY_DIR / "glossary.yaml").read_text()):
        g.add_node("term", entry["term"], "authored",
                   description=entry.get("definition"),
                   synonyms=entry.get("synonyms"))
        g.glossary.append({
            "term": entry["term"],
            "definition": oneline(entry["definition"]),
            "synonyms": ", ".join(entry.get("synonyms") or []) or None,
            "decided_by": entry.get("decided_by"),
        })

    for b in yaml.safe_load((ONTOLOGY_DIR / "bindings.yaml").read_text()):
        src, dst = binding_ref(b["from"]), binding_ref(b["to"])
        for ref in (src, dst):
            if ref not in g.nodes:
                raise ValueError(
                    f"bindings.yaml references unknown node {ref!r} "
                    f"(in {b!r}) — was something renamed?"
                )
        g.add_edge(src, b["rel"], dst, "authored")


def warn_unbound_cubes(g: Graph) -> None:
    """Coverage gaps stay visible: every cube should have an entity."""
    bound = {dst for src, rel, dst, _ in g.edges
             if rel == "measured_by" and src.startswith("entity:")}
    for node_id, node in sorted(g.nodes.items()):
        if node["kind"] == "cube" and node_id not in bound:
            sys.stderr.write(
                f"WARNING: cube {node['name']!r} has no entity binding — "
                f"add a measured_by edge in ontology/bindings.yaml\n"
            )


def annotate_relations(g: Graph) -> None:
    """Precompute per-node relation summaries (single-scan describe)."""
    outgoing: dict[str, list[str]] = {}
    incoming: dict[str, list[str]] = {}
    for src, rel, dst, _ in sorted(g.edges):
        outgoing.setdefault(src, []).append(f"{rel} -> {dst}")
        incoming.setdefault(dst, []).append(f"{src} -> {rel}")
    for node_id, node in g.nodes.items():
        node["relations_out"] = "; ".join(outgoing.get(node_id, [])) or None
        node["relations_in"] = "; ".join(incoming.get(node_id, [])) or None


# Node kinds an entity/term binding walk is allowed to END on — the
# implementation artifacts an agent can act on. Dimensions and other
# entities may appear as intermediate hops but are not destinations.
BINDING_TARGET_KINDS = {
    "cube", "measure", "tool", "dbt_model", "dbt_source", "toolset", "source",
}
BINDING_MAX_HOPS = 3


def bindings_closure(g: Graph) -> list[dict]:
    """All simple forward paths (<= BINDING_MAX_HOPS) from every
    entity/term node to implementation artifacts."""
    forward: dict[str, list[tuple[str, str]]] = {}
    for src, rel, dst, _ in sorted(g.edges):
        forward.setdefault(src, []).append((rel, dst))

    rows: list[dict] = []
    starts = [n for n in sorted(g.nodes) if g.nodes[n]["kind"] in ("entity", "term")]
    for start in starts:
        stack = [(start, start, 0, frozenset([start]))]
        while stack:
            node_id, path, depth, visited = stack.pop()
            for rel, dst in forward.get(node_id, []):
                if dst in visited:  # simple paths only
                    continue
                dst_path = f"{path} -[{rel}]-> {dst}"
                dst_node = g.nodes[dst]
                if dst_node["kind"] in BINDING_TARGET_KINDS:
                    rows.append({
                        "from_id": start,
                        "from_name": g.nodes[start]["name"],
                        "from_kind": g.nodes[start]["kind"],
                        "kind": dst_node["kind"],
                        "name": dst_node["name"],
                        "description": dst_node["description"],
                        "path": dst_path,
                        "hops": depth + 1,
                    })
                if depth + 1 < BINDING_MAX_HOPS:
                    stack.append((dst, dst_path, depth + 1, visited | {dst}))
    rows.sort(key=lambda r: (r["from_id"], r["kind"], r["name"], r["path"]))
    return rows


PATH_MAX_HOPS = 4


def shortest_paths(g: Graph) -> list[dict]:
    """Undirected all-pairs shortest paths (<= PATH_MAX_HOPS), one row
    per unordered pair, endpoints excluding leaf-only dimensions. The
    ontology_path tool matches either orientation; the stored path
    string reads in (a, b) order."""
    und: dict[str, list[tuple[str, str]]] = {}
    for src, rel, dst, _ in sorted(g.edges):
        und.setdefault(src, []).append((rel, dst))
        und.setdefault(dst, []).append((f"{rel} (reverse)", src))

    endpoints = [n for n in sorted(g.nodes) if g.nodes[n]["kind"] != "dimension"]
    rows: list[dict] = []
    for a in endpoints:
        # BFS with sorted adjacency -> deterministic shortest paths.
        paths: dict[str, str] = {a: a}
        frontier, hops_from = [a], {a: 0}
        while frontier:
            nxt: list[str] = []
            for node_id in frontier:
                if hops_from[node_id] >= PATH_MAX_HOPS:
                    continue
                for rel, dst in und.get(node_id, []):
                    if dst in paths:
                        continue
                    paths[dst] = f"{paths[node_id]} -[{rel}]-> {dst}"
                    hops_from[dst] = hops_from[node_id] + 1
                    nxt.append(dst)
            frontier = nxt
        for b in endpoints:
            if b <= a or b not in paths:
                continue
            rows.append({
                "a_name": g.nodes[a]["name"], "a_kind": g.nodes[a]["kind"],
                "b_name": g.nodes[b]["name"], "b_kind": g.nodes[b]["kind"],
                "path": paths[b], "hops": hops_from[b],
            })
    rows.sort(key=lambda r: (r["a_name"], r["b_name"], r["path"]))
    return rows


def sql_str(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def values_block(rows: list[dict], columns: list[str]) -> str:
    rendered = [
        "    (" + ", ".join(
            sql_str(row[c]) if not isinstance(row[c], int) else str(row[c])
            for c in columns
        ) + ")"
        for row in rows
    ]
    return ",\n".join(rendered) + ";"


def render(g: Graph, bindings: list[dict], paths: list[dict]) -> str:
    lines = [
        "-- Compiled business ontology — GENERATED, DO NOT EDIT BY HAND.",
        "-- Source of truth: ontology/*.yaml (authored) + cube model,",
        "-- tools.yaml, and the dbt project (derived). Regenerate with:",
        "--",
        "--   uv run --no-project --with pyyaml python3 ontology/gen_ontology.py",
        "--",
        "-- Served by the sales quack-server (read at the end of seed.sql)",
        "-- and queried by the `ontology` toolset in tools.yaml. The",
        "-- bindings/paths tables are traversals precomputed at codegen",
        "-- time — see the generator's docstring for why (single",
        "-- streaming scan per tool statement; static graph).",
        "",
        "CREATE TABLE IF NOT EXISTS ontology_nodes (",
        "    id            VARCHAR PRIMARY KEY,  -- '<kind>:<name>'",
        "    kind          VARCHAR NOT NULL,",
        "    name          VARCHAR NOT NULL,",
        "    description   VARCHAR,",
        "    synonyms      VARCHAR,              -- comma-joined",
        "    owner         VARCHAR,",
        "    caveats       VARCHAR,              -- ' | '-joined",
        "    origin        VARCHAR NOT NULL,     -- authored | cube-model | tools-config | dbt-project",
        "    relations_out VARCHAR,              -- '; '-joined 'rel -> dst'",
        "    relations_in  VARCHAR               -- '; '-joined 'src -> rel'",
        ");",
        "",
        "INSERT INTO ontology_nodes VALUES",
        values_block(
            [node for _, node in sorted(g.nodes.items())],
            ["id", "kind", "name", "description", "synonyms", "owner",
             "caveats", "origin", "relations_out", "relations_in"],
        ),
        "",
        "CREATE TABLE IF NOT EXISTS ontology_edges (",
        "    src    VARCHAR NOT NULL,",
        "    rel    VARCHAR NOT NULL,",
        "    dst    VARCHAR NOT NULL,",
        "    origin VARCHAR NOT NULL",
        ");",
        "",
        "INSERT INTO ontology_edges VALUES",
        values_block(
            [dict(zip(("src", "rel", "dst", "origin"), e)) for e in sorted(g.edges)],
            ["src", "rel", "dst", "origin"],
        ),
        "",
        "CREATE TABLE IF NOT EXISTS ontology_glossary (",
        "    term       VARCHAR PRIMARY KEY,",
        "    definition VARCHAR NOT NULL,",
        "    synonyms   VARCHAR,              -- comma-joined",
        "    decided_by VARCHAR               -- provenance: who decided, when",
        ");",
        "",
        "INSERT INTO ontology_glossary VALUES",
        values_block(
            sorted(g.glossary, key=lambda e: e["term"]),
            ["term", "definition", "synonyms", "decided_by"],
        ),
        "",
        "-- Precomputed entity/term -> implementation closure.",
        "CREATE TABLE IF NOT EXISTS ontology_bindings (",
        "    from_id     VARCHAR NOT NULL,",
        "    from_name   VARCHAR NOT NULL,",
        "    from_kind   VARCHAR NOT NULL,     -- entity | term",
        "    kind        VARCHAR NOT NULL,     -- cube | measure | tool | dbt_model | dbt_source | toolset | source",
        "    name        VARCHAR NOT NULL,",
        "    description VARCHAR,",
        "    path        VARCHAR NOT NULL,",
        "    hops        INTEGER NOT NULL",
        ");",
        "",
        "INSERT INTO ontology_bindings VALUES",
        values_block(bindings, ["from_id", "from_name", "from_kind", "kind",
                                "name", "description", "path", "hops"]),
        "",
        "-- Precomputed undirected shortest paths, one row per unordered",
        "-- pair (path string reads in (a, b) order; query both ways).",
        "CREATE TABLE IF NOT EXISTS ontology_paths (",
        "    a_name VARCHAR NOT NULL,",
        "    a_kind VARCHAR NOT NULL,",
        "    b_name VARCHAR NOT NULL,",
        "    b_kind VARCHAR NOT NULL,",
        "    path   VARCHAR NOT NULL,",
        "    hops   INTEGER NOT NULL",
        ");",
        "",
        "INSERT INTO ontology_paths VALUES",
        values_block(paths, ["a_name", "a_kind", "b_name", "b_kind", "path", "hops"]),
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the compiled seed SQL to stdout instead of writing it.",
    )
    args = parser.parse_args()

    g = Graph()
    load_derived(g)
    load_authored(g)
    warn_unbound_cubes(g)
    annotate_relations(g)
    bindings = bindings_closure(g)
    paths = shortest_paths(g)
    sql = render(g, bindings, paths)

    if args.stdout:
        sys.stdout.write(sql)
        return 0
    OUTPUT.write_text(sql)
    sys.stderr.write(
        f"wrote {len(g.nodes)} nodes, {len(g.edges)} edges, "
        f"{len(g.glossary)} glossary terms, {len(bindings)} bindings, "
        f"{len(paths)} paths to {OUTPUT.relative_to(REPO_ROOT)}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
