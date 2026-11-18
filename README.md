# AML Investigation Copilot

[![CI](https://github.com/sanskrutimahajan/AML-Investigation-Copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/sanskrutimahajan/AML-Investigation-Copilot/actions/workflows/ci.yml)

An investigation assistant for anti-money-laundering (AML) analysts. You ask a question in plain
English, such as *"Is account A connected to account B through payments?"* or *"Does this customer
match anyone on a sanctions list?"*. The answer comes from a graph of customers, accounts, payments,
sanctions records and offshore-company records, and every sentence cites the graph IDs it rests on.
If the evidence does not support an answer, the system says so instead of guessing.

Around the assistant sits an investigator workbench for the analyst's whole loop: an alert queue fed
by four laundering-pattern detectors, subject profiles, name screening, and SAR (suspicious activity
report) drafts labeled for analyst review.

Everything runs on a laptop: Neo4j and Qdrant in Docker, and the LLM (`qwen2.5:7b-instruct`) through
Ollama. No API key is needed and nothing is billed.

> This is a portfolio prototype, not a production AML system. Customer records are synthetic, and
> the links between customers and real sanctions or ICIJ names are planted for evaluation. They are
> not evidence about any real person or company, and ICIJ inclusion does not imply wrongdoing.

## Results at a glance

Each figure is from a held-out run, scored once after all tuning on a separate development split.
Confidence intervals, baselines and known failures are in [docs/RESULTS.md](docs/RESULTS.md).

| Capability | Held-out result | Comparison |
| --- | --- | --- |
| Entity resolution (customer ↔ sanctions/offshore record) | F1 0.642 (95% CI 0.611–0.672) | Exact-name baseline 0.411 |
| Cited question answering (60 questions, gold answers) | 47/60 correct (0.78) | List questions 0/10 (disclosed failure) |
| Typology detection (4 detectors, seed accounts) | Pattern recall 0.41–0.96 by typology | Fan-out does not clearly beat a degree baseline |
| SAR drafts (20 alerts) | 16/20 grounded drafts, 4 abstained | Every draft labeled `DRAFT – requires analyst review` |
| Synthetic-data leakage check | Max ROC-AUC 0.621 (target ≤ 0.60) | Reported as FAIL; disclosed judgment call |

## Who it is for

**Financial-crime analysts and investigators**: transaction-monitoring analysts at banks and
payment companies who clear alerts, investigators who build cases and write SARs, and compliance
reviewers who check that work. The workbench has one page per step of their day:

| Analyst's step | Workbench page |
| --- | --- |
| See what monitoring flagged, review an alert's accounts and payments | **Overview**, **Alert queue** |
| Find out who the customer is and what else they do | **Subject profile** |
| Check a name against sanctions and offshore-leak records | **Name screening** |
| Ask follow-up questions no fixed screen answers | **Investigation assistant** |
| Write up the case | **SAR drafting** |

The slow part is the investigation in the middle: *"where did this money go next?"* or *"who else is
behind this company?"* usually takes several queries across several systems. The copilot answers
them directly and shows the evidence, so the analyst can check every claim.

## Why a graph, and why Graph-RAG

**Money laundering is a pattern in relationships, not in single records.** One payment rarely looks
suspicious. The signal is in the structure: money passed along a chain of accounts (layering), money
that returns to where it started (a cycle), many senders paying one account (fan-in), or a customer
whose name matches a sanctioned company's director. Those are paths and neighbourhoods, which is
what a graph database stores and queries well.

| Analyst question | What it is in the graph |
| --- | --- |
| Is account A connected to account B through payments? | Shortest path over `SENT` edges, capped at 4 hops |
| Does this customer match a sanctions or offshore record? | `Customer -[SAME_AS]-> SourceEntity` |
| Who is behind this offshore company? | Officer and intermediary edges around an ICIJ node |
| Is money going round in a circle? | Time-ordered cycle of `SENT` edges |
| How much did this customer send, per currency? | Aggregate over the edges of the customer's accounts |

**Why not plain vector RAG?** Standard RAG embeds text passages and retrieves the most similar ones.
This data has no passages: it is accounts, amounts, timestamps and names. "Is A connected to B?" has
no similar paragraph to find; it needs a traversal. So the graph is the retriever, and Qdrant does
the one thing embeddings do well here: finding the record a typed name refers to, despite typos and
transliteration ([ADR 009](docs/decisions/009-retrieval-and-qa.md)).

**Why not just SQL?** Multi-hop questions become recursive self-joins whose cost grows with every
hop. In Cypher a 4-hop payment path is one line.

**What is the LLM allowed to do?** Language work only: read the question, pick the query that
answers it, and turn the query results into cited sentences. It never computes a fact. Totals,
paths and matches come from the graph, and a grounding check rejects any answer whose IDs or
numbers are not in the retrieved evidence.

## How it works

```text
 IBM AML HI-Small ─┐
 OpenSanctions ────┼─> stage: parse, sample, ─> Neo4j graph ─┬─> entity resolution ─> SAME_AS /
 ICIJ Offshore ────┘   synthesize customers                  │   (Splink)              POSSIBLE_SAME_AS
                                                             └─> 4 typology ────────> Alert nodes
                                                                 detectors

 Streamlit workbench ─> FastAPI ─┬─ /ask: router ─> name linking (Qdrant + full-text)
                                 │        ─> Cypher template or guarded text-to-Cypher (read-only)
                                 │        ─> LLM answer ─> grounding check ─> cited answer
                                 ├─ /sar: alert evidence ─> LLM draft ─> grounding check ─> DRAFT
                                 └─ alerts, subjects, screening: fixed read-only queries, no LLM
```

**Offline, building the graph:**
1. **Ingest** payments from IBM AML, sanctioned people and companies from OpenSanctions, and
   offshore entities and officers from ICIJ; create synthetic customers to own the accounts.
2. **Resolve entities:** blocking (rare name tokens, phonetic keys, embedding neighbours) finds
   candidate customer–record pairs, and a Splink model scores each on name, suffix, birth date and
   country. High scores become `SAME_AS`; the band below becomes `POSSIBLE_SAME_AS` for review.
3. **Detect typologies:** fan-in, fan-out, cycle and scatter-gather detectors write `Alert` nodes.

**Online, answering a question:**
1. **Route** the question to one of six query templates (profile, transactions, payment path,
   watchlist links, offshore network, alerts) or to guarded text-to-Cypher.
2. **Link** the names and IDs it mentions to graph records. An ambiguous name gets a clarifying
   question, not a guess.
3. **Retrieve** with the parameterized, read-only query.
4. **Answer:** the LLM writes sentences citing the IDs of the rows they use.
5. **Check** every cited ID and number against the evidence; regenerate once, then reply
   "insufficient evidence".

A SAR draft follows the same path for one alert: its accounts, owners and payments, then draft,
check and label.

## Data

| Source | What it provides | Staged |
| --- | --- | --- |
| [IBM AML (HI-Small)](https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml) | Synthetic payments with labeled laundering patterns | 270,523 of 5,078,345 payments, incl. all 5,177 laundering ones |
| [OpenSanctions](https://www.opensanctions.org/datasets/) | Sanctioned people and companies (OFAC SDN, EU, UN) | 20,395 entities |
| [ICIJ Offshore Leaks](https://offshoreleaks.icij.org/pages/database) | Offshore companies, officers, intermediaries | 22,000 of 2,016,523 nodes |

**Why synthetic customers?** IBM's payments carry no names, and no public data links bank customers
to sanctions lists. So the project creates 37,515 customers and plants known links: 1,125 get a name
derived from a real record with realistic edits (typos, transliteration, reordering, initials,
aliases, dropped suffixes). Two harder groups test false matches: 375 *homonyms* share a real
record's exact name but not its birth date or country, and 375 *look-alikes* have similar names.
That gives entity resolution a known answer key. `make leak-test` checks that seeded customers
cannot be spotted from their other fields alone.

The sample keeps every laundering payment, so laundering is about 19× more common than in the full
dataset; detection precision here is not real-world precision
([ADR 005](docs/decisions/005-sampling-bias-and-detection-evaluation.md)). Data is downloaded by
hand and never committed ([licenses](docs/DATA_LICENSES.md)).

## Keeping answers trustworthy

| Risk | Guard |
| --- | --- |
| The LLM invents a fact or number | Cited IDs and numbers must be in the evidence; one regeneration, then "insufficient evidence" |
| LLM-written Cypher changes or floods the database | Read-only session; `EXPLAIN` check against the schema; mandatory row limit (50), hop limit (4), timeout (10 s) |
| Injection through query values | Values are passed only as parameters, never formatted into Cypher |
| Text in a record acts as an instruction | Prompts treat evidence lines as data, not instructions |
| An ambiguous name picks the wrong person | A clarifying question instead of a guess |
| Overstating guilt | Fixed notices on customer and ICIJ answers; SARs labeled `DRAFT – requires analyst review` |
| Evaluations that flatter the model | Tuning on a development split; each held-out split scored once; baselines and confidence intervals |

The workbench calls only the API, so every guard lives in one place.

## Design decisions and trade-offs

Each is recorded as an ADR in [docs/decisions/](docs/decisions/).

| Decision | Instead of | Why |
| --- | --- | --- |
| Query templates, plus guarded text-to-Cypher for the rest | Text-to-Cypher for everything | Common questions don't depend on the LLM writing correct Cypher ([ADR 009](docs/decisions/009-retrieval-and-qa.md)) |
| Splink decides matches | An LLM judging every pair | Fast and explainable per field; the local LLM's review of borderline pairs *lowered* F1, so it changes no edge ([ADR 007](docs/decisions/007-entity-resolution.md)) |
| Embeddings in blocking, not in the match score | Embedding similarity as a Splink feature | Correlated name features counted as independent evidence pushed scores to 1.0 and recall to 0.18 ([ADR 007](docs/decisions/007-entity-resolution.md)) |
| Borderline matches go to review | Forcing match or non-match | An uncertain screening hit belongs with an analyst |
| Local LLM by default | A hosted API | Free and private; any provider works through `.env` alone ([ADR 008](docs/decisions/008-llm-provider.md)) |
| Rule-based detectors | A trained classifier | Explainable, and each alert states what it counted ([ADR 010](docs/decisions/010-typology-detection.md)) |
| Gold-answer evaluation | LLM-judged scores (RAGAS) | Exact and reproducible ([ADR 009](docs/decisions/009-retrieval-and-qa.md)) |

## What went wrong, and what changed

The evaluations found real problems; the full list is in
[docs/RESULTS.md](docs/RESULTS.md#failures-found-and-fixed).
- **Label noise:** 216 "non-match" customers had a real record's exact name, so correct matches
  were scored as errors. Every synthetic name is now checked against all source names.
- **Leaky synthetic data:** a classifier could spot seeded customers from country and address alone
  (AUC 0.795). All non-name attributes now come from one shared function, guarded by `make leak-test`.
- **The LLM made entity resolution worse:** its "match" calls on borderline pairs were 18% correct,
  so it no longer changes any edge.
- **A SAR misread a number:** it called a detection threshold "6 sender accounts". The grounding
  check can't catch a misread number that is present, so alerts now carry a code-written summary.

## Limitations and next steps

- **List questions fail** (0/10): the router sends them to a single-customer template. Next: route
  them to text-to-Cypher and score a new held-out set.
- **Homonyms are the main matching error** (0.79 get a false `SAME_AS`). Next: send exact-name
  matches with conflicting or missing birth dates and countries to review.
- **Watchlist questions are not yet in the QA evaluation;** fan-out detection overfits its
  development split.
- **The data is synthetic,** so the scores measure the method, not real-world performance; the
  evaluation sets are small.
- **Latency on a laptop:** 12.6 s per question and 90 s per SAR draft with the local 7B model. A
  hosted model needs only a `.env` change.

## Quick start

Needs Docker Desktop, [Ollama](https://ollama.com), and Python 3.11 with
[`uv`](https://docs.astral.sh/uv/) for loading data and running tests. Full instructions,
including how to build the graph and use another LLM provider, are in
[docs/SETUP.md](docs/SETUP.md).

```sh
cp .env.example .env               # set NEO4J_PASSWORD
ollama pull qwen2.5:7b-instruct    # Ollama runs natively, for GPU access
docker compose up -d --wait        # Neo4j, Qdrant, API (:8000) and workbench (:8501)
uv sync && make seed               # once, after downloading the data: graph, ER edges, index, alerts
```

To develop, run the databases with `make up` and the API and workbench natively with `make api` and
`make ui`; `make test && make lint` need no services.

With the graph built, ask through the API or draft a SAR:

```sh
curl -s localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "How much did account 10:8000588D0 send between 1 and 5 September 2022?"}'
curl -s localhost:8000/sar -H 'content-type: application/json' \
  -d '{"alert_id": "alert-0016267745f71997"}'
```

## Repository

```text
src/tracegraph/  ingest/ graph/ er/ retrieval/ generation/ detection/ llm/ api/
ui/              Streamlit workbench
eval/            evaluation commands; cited results in eval/results/
docs/            RESULTS.md, SETUP.md, PROPOSAL.md, DATA_LICENSES.md, decisions/ (ADRs), worklog.md
tests/           unit tests, plus integration tests against throwaway Neo4j/Qdrant containers
```

**Stack:** Python 3.11, `uv`, Polars, Neo4j 5 (APOC, GDS), Qdrant, `sentence-transformers`
(`BAAI/bge-small-en-v1.5`), Splink, FastAPI, Streamlit, Ollama, pytest, Ruff, mypy.
