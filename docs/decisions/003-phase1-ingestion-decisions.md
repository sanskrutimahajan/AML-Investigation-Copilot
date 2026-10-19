# ADR 003: Phase 1 ingestion decisions

- **Status:** Accepted (2026-10-19). §6 (ICIJ cap and addresses) and §7–8 (customer attributes, seed edits) are amended by ADR 006
- **Context:** Phase 1 turns the three raw sources into validated staging tables and then into a Neo4j
  graph. These decisions were approved by the user and should not be re-opened without a new ADR.

## Decisions

1. **Dependencies:** `polars` handles data processing, `faker` generates synthetic customer
   attributes, and `pandera` validates each staging table. Every staging Parquet file is validated
   with a strict `pandera` schema before it is written, and again before it is loaded.
2. **Labels stay off the graph:** IBM `Is Laundering` labels, pattern membership, and seeded-link
   answers are written only to eval ground-truth files:
   - `data/processed/ibm_pattern_ground_truth.parquet`
   - `data/processed/seed_ground_truth.parquet`

   Staging tables live in `data/processed/staging/`, the only directory graph loaders read. The
   strict staging schemas reject label columns. Tests check that staged transactions carry no label
   column and that no graph module refers to ground-truth paths. A Neo4j integration test asserting
   that no `SENT` edge has a label property will be added with the Step 3 loader.
3. **Manual download + `verify_data`:** The user downloads the raw data. `make verify-data` checks
   that the expected files exist, that their headers are correct, and that their SHA-256 checksums
   match `data/raw/*/MANIFEST.tsv`. It writes the IBM manifest on its first run. No code downloads data.
4. **IBM sampling keeps whole patterns:**
   - Every laundering-labelled transaction and every transaction in the patterns file is kept.
   - All accounts involved in those transactions are seed accounts, along with a deterministic random
     set of `ibm_benign_seed_accounts` other accounts.
   - For each seed account, up to `ibm_max_txns_per_account` of its other transactions are added,
     picked at random but deterministically. This cap stops hub accounts pulling in the whole graph.
   - Self-transfers are left out of that expansion.
   - If the sample comes out larger than `ibm_max_transactions`, staging stops with an error instead
     of truncating, because truncating would break patterns.
   - Bank IDs are normalized to integers, because the transactions file pads them with zeros and the
     accounts file does not. Account IDs take the form `bank_id:account_number`.
5. **OpenSanctions:** Use the `us_ofac_sdn`, `eu_fsf` and `un_sc_sanctions` lists.
   - Keep `Person`, `Organization`, `Company` and `LegalEntity` records, merged by their canonical ID.
   - Relationship records (Ownership, Directorship, and others) become edges when both endpoints are kept.
   - Cap the number of entities at `opensanctions_entity_sample_size`.
6. **ICIJ:**
   - Restrict to the leaks in `icij_source_ids` (default: Panama Papers).
   - Seed from `icij_seed_entity_count` offshore entities chosen at random but deterministically.
   - Add their 1-hop neighbours, then the other entities of those neighbouring officers (the second hop
     goes through officers only). The 2-hop additions are trimmed to `icij_node_sample_size`.
   - Keep relationships whose two endpoints are both selected.
   - ICIJ's own `same_as`, `similar` and `same_name_as` links are renamed with an `ICIJ_` prefix, so
     they can't be confused with our entity-resolution `SAME_AS` edges.
7. **Synthetic customers:**
   - Each IBM entity in the sample gets one `Customer`, with a Faker-generated name, date of birth or
     incorporation date, country and address, and `synthetic=true`.
   - Individuals and sole proprietorships become person customers; all other entity types become
     organizations.
8. **Seeded links:**
   - `seeded_link_rate` (3%) of customers get a name derived from an OpenSanctions or ICIJ record in
     the sample. The share from OpenSanctions is `seed_opensanctions_share` (50%).
   - The name gets 1 to `seed_max_perturbations` changes: typo, transliteration, token reorder, suffix
     removal, alias substitution, or initials.
   - `seed_laundering_share` (50%) of the seeded customers own an account that touches laundering; the
     rest are chosen at random.
   - Separately, `hard_negative_rate` of customers get similar-looking names that are *not* matches.
   - All of this is recorded only in `seed_ground_truth.parquet`. No gold `SAME_AS` edge is created.
9. **Docker:** Still deferred. Steps 1–2 (parse, sample, synthesize, validate, stage) need no
   services. Step 3, loading the graph, needs a running Neo4j; how that is provided will be decided
   before Step 3.
10. **Qdrant is deferred to Phase 2**, when embeddings are first needed for entity resolution.

All thresholds, caps, and shares above live in `src/tracegraph/config.py`.
