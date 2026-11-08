# Stream: GraphRAG communities

Design status: decided for the Leiden library (task T1). The module, the reports and the MCP tools are not built yet.

## Decision

The optional extra `graphrag` pins `graspologic-native==1.3.1`. The pure-Python seeded Louvain fallback stays the default when the library is absent.

`networkx==3.6.1` was the first candidate and it FAILS. Its `leiden_communities` exists, but it only dispatches to an installable backend. Without a backend it raises:
`NotImplementedError: 'leiden_communities' is not implemented by 'networkx' backend`. The known backends are igraph (GPL) or GPU (cugraph), so networkx gives no usable Leiden under the licence rule.

## Candidates, measured on 2026-11-08

| Candidate | Result |
|---|---|
| `networkx==3.6.1` | BSD-3-Clause, pure wheel, no dependencies. Signature `leiden_communities(G, weight, resolution, max_level, seed, *, backend)` but it raises `NotImplementedError` without a backend. Rejected. |
| `graspologic-native==1.3.1` | MIT (classifier `License :: OSI Approved :: MIT License`, source headers "Licensed under the MIT license"). Works. Chosen. |
| `python-igraph`, `leidenalg` | GPL-2.0+ and GPL-3.0. Rejected by the project rule. |

## Evidence

Scratch venvs were made under the scratchpad, never in `pipeline/.venv`. Graph: three 8-node cliques (weight 1.0), joined by two edges of weight 0.2, plus one isolated node.

| Check | Command or call | Result |
|---|---|---|
| Install | `pip install graspologic-native==1.3.1` (Python 3.13.14, Windows) | installs `numpy 2.5.4` and `scipy 1.18.1` as dependencies (metadata: `numpy>=1.24`, `scipy>=1.10`) |
| networkx deps | `pip install networkx==3.6.1`, `pip freeze` | only `networkx`, no dependencies |
| networkx Leiden | `leiden_communities(G, seed=42)` | `NotImplementedError` (no backend) |
| Release date | PyPI JSON `upload_time_iso_8601` | 1.3.1 uploaded 2026-06-14, 147 days before 2026-11-08 (minimum 14) |
| Wheels | PyPI JSON | `cp39-abi3` for win_amd64, win_arm64, manylinux2014 x86_64 and aarch64, musllinux x86_64 and aarch64, macOS universal2. No source build. |
| Seed | `leiden(edges, None, 1.0, 0.001, 1, True, 42, 1)` twice | identical dict; 3 communities, modularity 0.6619 |
| Order | same edges in a different order, same seed | identical partition |
| Resolution | 0.1, 1.0, 5.0 | 3, 3, 24 communities (monotonic) |
| Empty input | `leiden([], ...)` | raises `EmptyNetworkError` |
| Hierarchy | `hierarchical_leiden(edges, None, 1.0, 0.001, 1, True, max_cluster_size, 42)` | deterministic, rows have `node, cluster, level, parent_cluster, is_final_cluster`; on this graph it gives only level 0 because the cliques do not split |
| Levels by aggregation | `leiden` at resolution 5.0 (24 communities), then `leiden` on the quotient graph | 3 communities; each fine community maps to one coarse community |

## What the next tasks must know

1. API: `graspologic_native.leiden(edges, starting_communities, resolution, randomness, iterations, use_modularity, seed, trials) -> (modularity, {node: int})`. Nodes are `str`; edges are `(str, str, float)`. Use `randomness=0.001`, `iterations=1`, `use_modularity=True`, `trials=1`.
2. The library takes edges only. An isolated node is not in the result. `detect()` must add each isolated node as its own community. An empty edge list raises `EmptyNetworkError`: catch it and return no communities.
3. Levels: build them ourselves, the same way for the library and for the fallback. Level 0 is the finest partition. Each next level runs on the quotient graph (edge weights summed between communities). The child to parent map then nests by construction. Do not rely on `hierarchical_leiden`: it splits by cluster size, not by level count.
4. Seed: pass the `KG_COMMUNITY_SEED` value as `seed`. Sort nodes and edges before the call so the result does not depend on the order of the input.
5. Import `graspologic_native` lazily inside the library backend. If it is missing, raise an error that names the extra: `pip install 'kg-pipeline[graphrag]'`.
6. The fallback must give the same shape of result, so every test runs without the library. Library tests use `pytest.importorskip("graspologic_native")`.

## Platform notes

- Python 3.13: the wheel is `abi3` (`cp39`), so 3.13 needs no build. The project floor is 3.11 and works too.
- Windows: the `win_amd64` wheel was installed and run. Linux: `manylinux2014` wheels exist for x86_64 and aarch64. The Docker image (Linux) uses them.
- Size: the extra adds `numpy` and `scipy` (both BSD). It is optional, so the default image stays small.

## Fallback plan

If the library is absent, broken or removed, `KG_COMMUNITY_BACKEND=auto` uses the pure-Python Louvain with refinement (`kg_pipeline/leiden_fallback.py`, task T2). `KG_COMMUNITY_BACKEND=library` fails with the message above. The fallback is a Louvain variant with a refinement pass, not a full Leiden, so partitions differ between backends. Both are deterministic for a fixed seed.
