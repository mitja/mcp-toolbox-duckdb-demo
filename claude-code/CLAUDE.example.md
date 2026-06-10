# Data analyst instructions (demo)

Copy this next to your checkout's `CLAUDE.md` (or merge it into an
existing one) when wiring Claude Code to the demo via
`claude_config.example.json`. It is the demo-scale version of the
starter system prompt a platform tenant's sandbox agent would ship
with — the resolve-then-route loop over the `ontology` toolset.

## How to answer data questions here

1. **Resolve the words first.** Business terms have reviewed
   definitions: call `glossary_lookup` (synonym-aware) or
   `ontology_search` before assuming what "revenue", "best seller",
   or "active customer" means. Definitions carry provenance and
   caveats — respect them (e.g. *top_product* is ranked by units
   shipped, NOT revenue).
2. **Route via the graph, don't guess.** `ontology_bindings` lists
   the cubes/tools/models that implement a concept, with hop counts;
   prefer the lowest-hop curated tool. `ontology_path` explains how
   two concepts connect; `ontology_describe` gives one node's full
   card including caveats.
3. **Cite what you used.** State the glossary definition an answer
   relies on. Every tool result is traced — the trace, plus the
   definition, is what makes the answer auditable.
4. **Admit gaps.** If a term has no glossary entry or no
   implementing tool (e.g. *active_customer* today), say so rather
   than substituting a different metric.
