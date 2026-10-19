# ADR 004: Graph load (Phase 1 Step 3)

- **Status:** Accepted (2026-10-19)
- **Decisions:**
  1. `graph/loaders.py` reads only `data/processed/staging/` and re-validates every table with the
     strict pandera schemas before it writes anything. It loads with parameterized `UNWIND $rows`
     batches (`graph_batch_size`) and `MERGE` on stable IDs. Uniqueness constraints cover all node
     keys, `SENT.transaction_id`, and `relationship_id` on every source relationship type (Neo4j 5.26
     Community supports relationship uniqueness constraints).
  2. Cypher cannot take labels or relationship types as parameters. They are formatted only from code
     constants: the allowlists `opensanctions.REL_TYPES` and `icij.REL_TYPES`, which the staging schemas
     also enforce. They are never taken from data or user input, and every value is a parameter.
  3. **Fail loudly on mismatch:** if the graph contains any node ID, `SENT` transaction ID, or source
     relationship ID that is not in the current stage, or any node with an unknown label, the load
     raises `StagingMismatchError` before writing and points to `make graph-reset`. Nothing is deleted
     silently. `make graph-reset` asks the user to type `reset` first.
  4. **Post-load verification:** Neo4j counts per node label and per relationship type must equal the
     counts implied by the staging tables; otherwise the load raises `LoadVerificationError`. The counts
     are written to `data/processed/load_report.json`.
  5. **Integration tests** run against a throwaway `neo4j-test` container (compose profile `test`,
     port 7688, tmpfs, auth off, localhost only). The tests refuse to run against port 7687. CI starts
     the same image as a service container.
  6. **Pattern coverage** is reported by `eval/pattern_coverage.py`, not by the loader, because it reads
     eval ground truth.
- **Consequences:** Re-staging with different settings requires `make graph-reset` before
  `make ingest`. Loading the full stage takes about 90 seconds on a laptop; a repeated load of the
  same stage takes about 40 seconds and changes nothing.
