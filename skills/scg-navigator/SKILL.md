---
name: scg-navigator
description: Use this skill whenever Semantic Code Graph (SCG) MCP tools are available (search_code, get_node_context, get_node_summary, get_class_hierarchy, find_path, list_package_classes, query_neo4j, get_graph_stats) and the user is asking about a codebase's structure, dependencies, hierarchies, impact, modularity, refactoring, or "how does this work / where does this connect / what depends on this / where do I start reading this." Apply it even if the user does not mention "graph," "SCG," or any specific tool name — these questions are exactly what the graph is built for, and going straight to Read/Grep first will burn tokens that a single graph call would have answered. The skill teaches which MCP tool fits which sub-question, how to read truncation and omitted-node summaries, and how to stop exploring once the question is answered.
---

# SCG Navigator

You have access to a Neo4j-backed Semantic Code Graph of the project. Each `:CodeNode` carries `id`, `kind`, `displayName`, `uri`, `startLine`, `endLine`, and edges encode call / inheritance / declaration / type relations.

**Why this skill exists.** Reading source files line-by-line burns tokens. The graph already knows which classes are connected, which methods override what, which file contains which type, and how a call propagates. A well-placed graph query usually replaces dozens of Grep/Read calls. Lean on the graph; fall through to source only when implementation detail is what the question is actually about.

## Mental model

### Node kinds

| Kind | Meaning | Notes |
|---|---|---|
| `CLASS` | concrete class | usually the most useful starting point |
| `TRAIT` | interface / trait | inheritance roots |
| `ENUM` | enum type | rare, treat like CLASS |
| `METHOD` | callable | what `CALL` edges connect |
| `CONSTRUCTOR` | constructor | usually a sub-question of its CLASS |
| `FILE` | source file | use for "where is this defined" / package questions |
| `VALUE`, `VARIABLE`, `PARAMETER`, `TYPE_PARAMETER` | declarations inside methods/types | almost always noise — leave them filtered out |

### Edge kinds and how to read them

- `CALL` — A invokes B (runtime data flow & impact)
- `EXTEND` — A inherits from / implements B (type lattice)
- `OVERRIDE` — A overrides a method on B (polymorphism)
- `CONTAINS` — file/class lexically contains entity (locality)
- `TYPE` / `RETURN_TYPE` / `PARAMETER` / `DECLARATION` — typing relations
- `TYPE_ARGUMENT`, `EXTEND_TYPE_ARGUMENT`, `RETURN_TYPE_ARGUMENT` — generics

### Node IDs are the addressing primitive

`displayName` is for humans and is not unique. Always carry the `id` from one tool call to the next; never feed a display name into a tool that expects an ID.

## Open every session with project priors

Before anything else, call `get_graph_stats` once. The kind distribution and node count are the cheapest, most useful single piece of context you will get all session, and they shape every decision below.

### How to read the priors

- **Total nodes large (>10k)**: aggressive filtering required. Avoid `kinds=['ALL']` on `search_code` and `get_node_context`; trust the defaults.
- **High share of `VARIABLE` / `PARAMETER`**: noisy graph; leave the default kind filters in place.
- **Few `CLASS` nodes relative to `METHOD` count**: the project is method-heavy. Lead with method-level search rather than class-level.
- **Few `EXTEND` edges relative to `CLASS` count**: hierarchies are shallow; `get_class_hierarchy` will rarely be revealing — prefer `get_node_context`.

(Density, global clustering coefficient, and degree assortativity are not yet exposed by this MCP. When they are, they will refine these heuristics further. See `references/expansion.md`.)

## Tool decision tree

You have eight tools. Pick the cheapest one that answers your sub-question. The order below is roughly the order you should call them in a typical session.

### `search_code(query, limit, kinds=)` — entry point when you don't have an ID

Vector search over node embeddings. Returns IDs + scores. Defaults strip noise kinds.

- **Good queries**: short noun phrases at the *concept* level — "image loading pipeline," "lifecycle manager," "disk cache eviction." The embeddings reward semantic similarity, not literal-string matches.
- **Bad queries**: a class name you already know. Vector search on a known name is wasted; use `query_neo4j` with `WHERE n.displayName = 'X'` or `list_package_classes` to locate it deterministically.
- **Limit**: start with 5. Bump to 10 only when the top-5 look off-topic — that is itself a signal you should rephrase the query, not request more of the same.

### `get_node_summary(node_ids)` — your second call, almost always

Returns metadata + counts of each relationship kind + 5 example neighbor names per kind. **Cheap**: no edges traversed, no source text. Use it to decide *whether* spending tokens on a fuller subgraph is worthwhile.

If a node has 200 incoming `CALL` edges, a `get_node_context(hops=2)` on it will explode. Look at the counts first; let them set the budget.

### `get_node_context(node_ids, hops=1, kinds=)` — the workhorse

The k-hop subgraph view. Defaults filter `PARAMETER`, `VARIABLE`, `TYPE_PARAMETER` — you almost never want them.

- `hops=1` is your default. It answers "what does this entity directly touch."
- `hops=2` is dangerous on hub classes (>50 incident edges). Verify with `get_node_summary` first.
- `hops=3` is rarely useful — fan-out becomes incomprehensible. Prefer `find_path` when you need to reach a known target.
- The output is capped at 50 nodes. The "omitted by kind" summary is your map of what got pruned: if `CLASS (32)` is in the omitted bucket, you have just learned this entity sits in a hub of 30+ classes — that itself is part of the answer to many questions. Drill into specific listed IDs with `get_node_summary`, not by re-running with bigger hops.

### `get_class_hierarchy(node_id, direction='both')` — for inheritance questions

Pull the `EXTEND` chain up and/or down. Cheaper and clearer than `get_node_context` for hierarchy-shaped questions.

- `'up'` — "what does this implement / extend"
- `'down'` — "who implements / extends this"
- `'both'` — "where does this sit in the type lattice"

### `find_path(from_id, to_id, max_depth=5)` — for "how does A reach B"

Shortest path between two known IDs. The right tool when the question asks how data flows from one specific entity to another, or whether two entities are connected at all.

If no path exists at depth 5, raise `max_depth` once to 10. Failing that, the entities are not directly related — that is a real answer, not a tool failure.

### `list_package_classes(package_path, include_methods=False)` — structural / package questions

Pure lexical filter on the `uri` property. Use for "what's in package X," "show me the classes in the engine module," or "is there anything in this folder."

Set `include_methods=True` only when the user explicitly asks about methods at the package level — otherwise it triples the response size.

### `query_neo4j(cypher, params)` — escape hatch

Only when the prebuilt tools cannot express the question. Read-only. **Always include `LIMIT` and a `kind` filter.**

Patterns where this earns its keep:
- "Top-N classes by incoming `CALL` edges" — degree analysis the prebuilt tools don't expose.
- "All `METHOD`s in package X that have `OVERRIDE` edges" — combined kind + edge filter.
- "Find all classes that `EXTEND` any class containing 'Cache' in displayName" — multi-hop pattern matching.

If you find yourself writing more than ~10 lines of Cypher, step back; you have probably missed a simpler tool.

### `get_graph_stats()` — once per session

Already covered in priors. It does not change; do not call it again.

## How to read tool outputs

### Truncation summaries from `get_node_context`
`⚠️ Showing 50 of N nodes` plus an omitted-by-kind table is not a failure — it is information. The kinds list tells you the *shape* of what you missed. CLASS-heavy omissions mean a hub; METHOD-heavy omissions mean a busy interface; FILE-heavy omissions usually mean you queried something at module granularity.

Pick specific IDs from the listed examples and `get_node_summary` them. Do not re-run with larger limits; you will hit the same cap.

### Hidden relationships
The `⚠️ N relationships to/from omitted nodes` line tells you the *kind* of edges crossing the visible boundary. Many `CALL` edges leaving the visible set ⇒ the entity is a heavy caller. Many `CALL` edges arriving ⇒ heavily called. Both are answers to "how central is this."

### Empty results from `search_code`
If `search_code` returns nothing or low-relevance hits, do not retry with three synonyms. Switch tool: `list_package_classes` with a path fragment, or `query_neo4j` with `WHERE n.displayName CONTAINS 'X'` for a literal-name lookup.

## Workflows

Pick the workflow whose shape matches the question. Each is a tool sequence; deviate when an intermediate result invalidates the next step (e.g. a 200-incoming-edge summary should make you abandon `hops=2`).

### W1 — Find the critical entity
*Question shape: "what is the role of X" / "what classes are central to Y" / "what is responsible for Z."*

1. `get_graph_stats` (priors)
2. `search_code(query=concept, limit=5)` to surface candidate IDs
3. `get_node_summary([top 1–2 ids])` to confirm relevance and read relation counts
4. `get_node_context(top id, hops=1)` for its immediate surface
5. Read source via `Read` only if the question demands implementation detail

### W2 — Trace impact
*Question shape: "what depends on X" / "if X changes, what breaks" / "how does change propagate."*

1. Locate X via `search_code` or `query_neo4j`
2. `get_node_summary([X])` — read the *incoming* relationship counts. That is the first-order blast radius, in one cheap call.
3. If incoming count is small (<20), `get_node_context([X], hops=1)` to enumerate every dependent.
4. If incoming count is large, do not `get_node_context` blindly. Use `query_neo4j`:
   ```cypher
   MATCH (caller:CodeNode)-[:CALL|TYPE|EXTEND]->(x:CodeNode {id: $xid})
   RETURN caller.id, caller.kind, caller.displayName
   LIMIT 50
   ```
5. For each major caller class, check whether *it* is depended on widely — that is how impact propagates outward. Stop when the propagation no longer reaches anything the question cares about.

### W3 — Hierarchy
*Question shape: "what implements X" / "what is the class hierarchy of Y" / "show me the inheritance chain."*

1. Locate the type via `search_code` (kinds=['CLASS','TRAIT'])
2. `get_class_hierarchy(id, direction='both')` — usually a single call answers it
3. For each level, `get_node_summary` only if the user asks about the role of intermediate classes

### W4 — Trace data flow / reachability
*Question shape: "how does call A end up at B" / "trace the pipeline from X to Y."*

1. Locate both endpoint IDs (`search_code` for each)
2. `find_path(from_id, to_id, max_depth=5)` for the shortest chain
3. For each node on the path, `get_node_summary` to explain its role
4. `Read` source only at the entry and exit points; the middle of a path is usually self-evident from the path nodes' kinds and display names

### W5 — Explore a package / module
*Question shape: "what's in package X" / "how is module Y organized" / "what are the main pieces of this subsystem."*

1. `list_package_classes(package_path, include_methods=False)` for the file → class map
2. For the most prominent classes (by name salience or count), `get_node_summary`
3. `get_node_context(hops=1, kinds=['CLASS','TRAIT','METHOD'])` for one or two of them, to surface cross-class relationships *within* the package

## Anti-patterns

These burn tokens and rarely help.

- **`get_node_context(hops=2)` on a hub class without checking summary first.** Hubs in cache/loading/scheduling code commonly have 100+ incident edges; hops=2 brings in thousands.
- **`query_neo4j` without `LIMIT`.** The server caps at 50 anyway, but you may also pull more than you can reason about. Always add `LIMIT 50` or smaller, and a `kind` filter.
- **`search_code` with a class name you already have.** Vector search is for *concepts*. For known names, `query_neo4j` with `WHERE n.displayName = ...` or `list_package_classes` is faster and exact.
- **Reading source files before consulting the graph.** Files are big; the graph tells you which file matters. Read source last, not first.
- **Calling `get_graph_stats` more than once.** It does not change.
- **Treating display name as the ID.** Two entities can share a name. Always copy the `id` field between calls.
- **Asking the graph the same question twice with different phrasings** when the first answer was clear. The graph is deterministic; rephrasing does not give you a second opinion.

## When to fall back to Read/Grep/Glob

The MCP gives you structure; sometimes you genuinely need the source. Fall back when:

- You have an entity ID, you have its location (uri/startLine/endLine from `get_node_summary`), and you need the *implementation* — comments, exact logic, branch conditions.
- The question asks about strings, format constants, regex patterns, or any text-level detail the graph does not encode.
- You are debugging a specific failure and need the actual statement that throws.

In those cases, use the URI and line range from `get_node_summary` to read precisely the lines you need — never the whole file.

## Stopping rules

You have enough to answer when:
- The user's question maps cleanly to nodes whose summaries / hierarchies / paths you have inspected
- You can name the specific IDs that anchor the answer
- Further tool calls would only add detail the question does not ask for

Resist the urge to keep exploring. Correctness tied to the question scores higher than breadth.
