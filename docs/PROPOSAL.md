# AML Investigation Copilot

## Proposal status

This initial proposal records only the project brief supplied at kickoff. Dataset-specific volumes, exact source snapshots, and deployment choices remain to be verified against primary sources before ingestion work begins.

Docker execution is currently deferred at the user's request. The Compose and Dockerfile scaffold remains present but unused, and the `make up`/`make down` targets do not operate containers. LLM provider calls are disabled by default; do not enable or connect credentials without explicit user approval.

## Problem and goal

AML investigators need to explore transactions alongside contextual entity information. AML Investigation Copilot will provide natural-language investigation support over a knowledge graph and vector index. Responses must be evidence-grounded, cite retrieved graph IDs, and distinguish synthetic links from source records.

The project is a portfolio prototype, not a production AML decision system. It will not make claims of criminality about real people, and SAR narratives are drafts requiring analyst review.

## Proposed architecture

```text
 IBM AML HI-Small ─┐
 OpenSanctions ────┼─> Ingestion / normalization ─> Entity resolution
 ICIJ Offshore ────┘                                 │
                                                    v
                                         Neo4j knowledge graph
                                         (accounts, transactions,
                                          source entities, links)
                                                    │
 Analyst question ─> FastAPI ─> routing / entity linking
                                     │              │
                                     v              v
                             Qdrant retrieval   Guarded graph retrieval
                                     └──────┬───────┘
                                            v
                                   Evidence fusion / LLM
                                            │
                                            v
                             Cited answer + grounding check
                             (later: alerts, SAR drafts, eval, UI)
```

## Build sequence

1. **Setup:** establish project structure, configuration, local services, health checks, test/lint/CI, and LLM provider boundaries.
2. **Ingestion and graph build:** inspect and subset the IBM AML HI-Small, OpenSanctions FollowTheMoney, and ICIJ Offshore Leaks sources; normalize and load source entities, accounts, and transactions.
3. **Entity resolution:** generate candidate pairs, score with probabilistic linkage and embeddings, adjudicate gray-zone pairs through an LLM interface, and record `SAME_AS` edges with provenance.
4. **Retrieval and Q&A:** route analyst questions, link entities, use parameterized Cypher templates or guarded read-only text-to-Cypher, fuse graph and vector results, and return cited answers verified against retrieved context.
5. **Typologies, evaluation, and UI:** detect transaction patterns and create alerts, draft review-labeled SAR narratives, measure performance with a reproducible evaluation harness, then add a Streamlit interface.

## Non-negotiable safeguards

- User/LLM-derived Cypher is read-only, schema-validated, limited in hops and rows, parameterized, and subject to a timeout.
- Every factual response claim is tied to retrieved node/edge IDs. If grounding cannot be verified after one regeneration, respond with “insufficient evidence.”
- Synthetic account-to-real-entity links are labeled synthetic and are never evidence of wrongdoing.
- ICIJ inclusion does not imply wrongdoing. SAR content is labeled `DRAFT – requires analyst review`.
- Credentials remain in ignored environment files; source datasets remain outside version control.
- Evaluation results are reported only after running the evaluation harness.

## Phase 1 planning items

### Loader design and laptop-sized scope

These are configured ingestion caps and planning estimates, **not measured source-dataset counts**. Actual row counts, schemas, and unique endpoint counts must be collected from the exact licensed snapshots before ingestion. The caps are in `src/tracegraph/config.py` and mirrored in `.env.example`.

| Source | Loader design | Laptop-sized cap and estimated graph contribution |
| --- | --- | --- |
| IBM AML HI-Small | `ibm_aml.py` will scan the transaction CSV with Polars, validate observed headers and types against a documented mapping, retain stable source transaction IDs (or generate deterministic IDs from source file/version plus row number if none exist), and join the patterns file using confirmed keys. Select a deterministic stratified sample of at most 100,000 transaction rows so available typology labels are represented; retain source attributes and provenance. Emit unique `Account` nodes and `SENT` relationships. | At most 100,000 `SENT` edges and 200,000 endpoint `Account` nodes (two endpoints per sampled row; actual unique-account count will be lower or equal). |
| OpenSanctions | `opensanctions.py` will stream the selected FollowTheMoney JSON records, preserve the source entity ID, schema, names/aliases, date/country fields, dataset membership, and provenance, and map references only when both endpoints are in scope. The allowlist of datasets is selected after reviewing current dataset metadata and terms; do not load or merge unrelated records merely because they share a name. | Cap at 5,000 `Person`/`Company`/`Organization` records and 15,000 mapped source relationships. |
| ICIJ Offshore Leaks | `icij.py` will inspect the downloaded CSV headers, then read the node-type files and relationship file from the official archive. Select a deterministic bounded connected subgraph from a documented release/dataset subset, preserving record IDs and source-specific node/edge types. Retain relationships only when both endpoints are selected. | Cap at 2,000 source nodes and 6,000 relationships. |

The conservative combined upper bound for the source graph is approximately **207,000 source nodes and 121,000 source relationships**, before synthetic profiles: the IBM cap assumes every sampled transaction has two unique accounts; the external-source caps are additive even if records overlap. This is a capacity bound derived from the caps, not a claim about observed dataset volumes.

### Synthetic profiles and seeded-link ground truth

Generate 1,000 deterministic synthetic customer profiles, with a configurable seed (`random_seed`, default 42) and no unmarked real-world personal data. Create a customer profile for a bounded selection of IBM accounts and one `OWNS` edge per profile. For a configurable 3% default seeded-link rate (30 of 1,000), derive a synthetic profile name from a selected OpenSanctions or ICIJ record and apply a recorded perturbation such as a typo, transliteration, token reorder, suffix removal, or alias substitution. Keep these generated profiles explicitly marked synthetic. Including the profiles adds at most 1,000 customer nodes and 1,000 ownership relationships, for a combined configured ceiling of approximately **208,000 nodes and 122,000 relationships**.

Persist the gold mapping separately at `data/processed/seed_ground_truth.parquet`, with at minimum `synthetic_customer_id`, `source_entity_id`, `source_dataset`, `perturbation_type`, `random_seed`, and `source_snapshot_id`. Do not place a gold `SAME_AS` edge in the retrieval graph: doing so would leak the ER answer. Phase 2 can compare predicted links against this table.

### Neo4j schema and loading

Create uniqueness constraints for `Account.account_id`, `Customer.customer_id`, and the composite `(SourceEntity.source_dataset, SourceEntity.source_id)`. Add a range index for normalized entity names and a full-text index over source-entity names and aliases for later lookup. Store a stable `transaction_id` on every `SENT` relationship; because relationship uniqueness constraints may depend on Neo4j edition/version, rely on deterministic, parameterized `MERGE` patterns for idempotent edge writes and verify repeat-load behavior in integration tests.

Normalize to Parquet staging files with Polars, then write parameterized `UNWIND $rows` batches through the official Neo4j driver, default 2,000 rows per transaction. Use separate node and relationship batches, explicit transactions, and bounded retries only for transient driver errors. Prefer `UNWIND` over `neo4j-admin import`: these datasets are intentionally capped for a laptop, this loader must be rerunnable/idempotent, and records need source provenance and controlled endpoint filtering. If measured volumes or throughput make this unreasonable, benchmark before changing strategy.

### Phase 1 tests

- Unit-test each source parser with tiny synthetic fixtures: required/optional columns, IDs preserved, null/malformed rows handled explicitly, type mapping, and provenance retained.
- Verify deterministic sampling, the per-source caps, relationship endpoint filtering, and pattern stratification without downloading public data.
- Verify seeded names are reproducible for a fixed seed, every perturbation is recorded, the configured rate is respected, and Parquet gold rows map to generated profiles without producing graph `SAME_AS` edges.
- Test graph writes against a Dockerized Neo4j fixture: constraints/index creation, node/relationship properties, parameterized batch writes, and idempotence across a repeated load.
- Keep license/source integration tests opt-in and require a manually downloaded snapshot; unit/CI tests must not fetch third-party data.

### Risks and open checks before coding

- The IBM HI-Small public source URL, license, exact CSV headers, pattern-file join key, and stable transaction-ID availability remain unverified. The specific source/version must be established before writing its parser; do not silently substitute a third-party mirror.
- OpenSanctions provides dataset-specific coverage and licensing details that may change. Verify the selected data snapshot's current terms and whether the required dump is available under the intended non-commercial use before choosing the allowlist.
- ICIJ documents ODbL for the database and CC BY-SA 3.0 for its contents. Record attribution and verify obligations for the precise downloaded release and any redistributed database.
- A sampled account endpoint count can only be known after parsing the selected IBM rows. The stated 200,000 count is an upper bound, not an expected observed count.
- Schema variants, missing keys, and changes to source snapshots may require a revised mapping or a smaller/alternate subset. If any source contradicts the brief, pause and present the discrepancy and options before implementation.
