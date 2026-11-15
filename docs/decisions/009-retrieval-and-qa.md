# ADR 009: Retrieval and cited Q&A (Phase 3)

- **Status:** Accepted (2026-11-15)

## Context
Phase 3 answers investigator questions from the graph, with every factual claim citing a node or
edge ID. Inspecting the data before design showed it contains **no free text**:
- OpenSanctions has names, aliases, topics, datasets, countries and birth dates;
- ICIJ has names, jurisdictions, company types and statuses;
- IBM has amounts, currencies, payment formats and timestamps.

The proposal's "fuse graph and vector results" assumed documents for vector search to retrieve.
There are none.

## Decisions
1. **Qdrant is used only for name linking** (user decision, 2026-11-15).
   - `make qa-index` embeds customer, source-entity (with aliases) and bank names.
   - Linking fuses that search with the Neo4j full-text index by reciprocal rank fusion.
   - IDs found in a mention, also inside a longer mention, are looked up directly.
   - A node found only by vector search needs a cosine score of at least 0.9. Nearest neighbours
     always exist, and nonsense scored about 0.83, close to real names.
   - A node is chosen only when that is unambiguous; otherwise the user gets a clarifying question.
2. **Five Cypher templates** (customer profile, transactions, payment path, watchlist links,
   source-entity network), plus guarded text-to-Cypher for everything else.
   - Every template returns `id`, `type` and `props` rows keyed by citable IDs.
   - Amounts are summed per currency, never across currencies.
3. **Graph safety:**
   - Every query runs in a read session with a timeout and a row cap. Neo4j itself rejects writes
     (`Neo.ClientError.Statement.AccessMode`).
   - Generated Cypher must also pass a static check (no write, procedure, admin or `UNION`
     keywords; `LIMIT`; bounded hops; no quantified paths) and `EXPLAIN`: read-only type, exact
     columns, and no unknown label, relationship type or property.
4. **Grounding:**
   - Every sentence cites IDs from the evidence.
   - Every number must appear in its cited evidence or in the question; digits inside IDs do not
     count.
   - A failing draft is regenerated once, then the answer is "insufficient evidence".
   - The router may only return mentions that occur in the question; the 7B model invented IDs.
5. **Payment-path answers are written by code** from the template result; the 7B model misread a
   4-payment chain as "not connected".
   - `shortestPath` ignores time, so an answer claims a flow of funds only when the payments are
     in time order. Otherwise it reports a connection.
   - "No path" answers cite the two parties and the search depth.
6. **Fixed notices** about synthetic links and ICIJ data are added by code, never by the LLM.
7. **The API runs natively** (`make api`), like Ollama (user decision, 2026-11-15).
   - The Docker image left out the embedding model (`er` group), and from inside a container
     `localhost` does not reach Ollama.
   - Docker Compose now runs only Neo4j and Qdrant.
   - `make smoke` hits `/health` and the README's `/ask` example on the running stack. It runs
     before any commit that touches the API or the README examples.
8. **Evaluation is `make qa-check`; RAGAS is dropped** (user decision, 2026-11-15).
   - Questions are generated from staging, with gold answers computed from Parquet, never from the
     graph or the system under test.
   - The seed-42 set is the development set: fixes were made after looking at it. Reported numbers
     come from a set with a different seed, run once, with no tuning afterwards.

## Evidence
- **Tests:**
  - Unit: text-to-Cypher attack strings, grounding (including a cross-currency sum), routing,
    fusion, path answers, the draft-regenerate loop, the API.
  - Integration on the fixture graph: template totals against Parquet, read-only enforcement,
    `EXPLAIN` checks, linking with an in-memory Qdrant.
- **Development run (seed 42), not a result:** answer accuracy 0.52 at first.
  - Its 24 failures were refusals, clarifying questions or "not found", from three causes: an ID
    with a leading word, the "no path" sentence failing my own grounding check, and list questions
    routed to a template.
  - After fixing the first two, the dev re-run reached link accuracy 1.00. The list-routing prompt
    change did not work: lists stayed at 0 of 10.
- **Held-out run (seed 43, run once, no tuning afterwards;**
  `eval/results/qa_check_20261115T051015Z.json`):
  - Answer accuracy 40/50 (0.80, Wilson 95% CI 0.67–0.89). Router intent 0.80. Linking 40/40.
  - Holdings, totals, paths and networks 10/10 each; lists 0/10.
  - 42 answered, 7 clarifying questions, 1 not found. Mean latency 16.9 s.
  - All ten list questions were routed to the single-customer template. Two of them were answered
    with grounded facts about a customer whose name resembled the description ("Ml Isvest" for
    "ML").
- **Phase 4 re-run:** adding the `alerts` intent changed the router, so the README QA table now
  comes from a fresh held-out set (seed 44, run once, `qa_check_20261115T072915Z`). It scored
  47/60, with lists 0/10 and three incomplete answers. Seed 43 remains the recorded Phase 3 run.
- **Known limitation, confirmed held-out:** the 7B router sends list questions to a
  single-customer template, and linking can then match a description to a similar name. This is
  reported, not tuned.
