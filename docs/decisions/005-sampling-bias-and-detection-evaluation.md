# ADR 005: Sampling bias and how Phase 4 detection must be evaluated

- **Status:** Accepted (2026-10-19)
- **Context:** The IBM sample (ADR 003 §4) is built around seed accounts:
  - **Core seeds** are every account that touches a laundering or pattern transaction (6,357).
  - **Benign seeds** are 8,000 accounts chosen at random but deterministically.
  - **Non-seeds** are the 39,243 counterparties that enter only through seeds' transactions.

  This keeps every pattern intact, but it biases the sample. Measured against the 2026-10-19 stage:

  | Measure | Full HI-Small | Sample |
  | --- | --- | --- |
  | Laundering share of transactions | 0.1019% (1 in 981) | 1.9137% (1 in 52), about 19× |
  | Share of accounts touching laundering | 1.23% | 11.9% |

  | Account group | n | Median degree in sample | Median degree in full data |
  | --- | --- | --- | --- |
  | Core seeds | 6,357 | 19 | 22 |
  | Benign seeds | 8,000 | 11 | 12 |
  | Non-seeds | 39,243 | 2 | 25 |

  By construction, no non-seed account touches a laundering transaction. Non-seed neighbourhoods are
  truncated (2 in the sample against 25 in the full data), because only their transactions with seeds
  are kept. Seed membership is recorded only in the eval file `data/processed/ibm_sample_seeds.parquet`.

- **Decision:** Phase 4 typology detection (and any account-level metric) must be evaluated as follows:
  1. **Seed-only comparisons.** The evaluation population is seed accounts only: core seeds as
     positives, benign seeds as negatives. Both kinds of seed have their neighbourhoods sampled the
     same way (up to `ibm_max_txns_per_account`). Non-seed accounts are excluded from precision and
     recall, because their structure is truncated and their label is known by construction.
  2. **Base rates reported with every metric.** Report the positive rate of the evaluation population
     (core / all seeds, currently 44.3%) next to the full-data rates above. Never present sample
     precision as real-world precision. Where useful, add precision re-weighted to the full-data
     prevalence, stating the assumptions.
  3. **Degree baseline.** Core seeds have a higher median degree than benign seeds in the full data
     (22 against 12), so a detector must be compared with a degree-only baseline. A typology detector
     that does not beat it adds nothing.
  4. **Transaction-level metrics** must state the 19× enrichment, and are computed only over
     transactions that have at least one seed endpoint.
- **Consequences:** `make eval` (Phase 5) reads `ibm_sample_seeds.parquet` and
  `ibm_pattern_ground_truth.parquet`, which are eval-only. Graph code never reads them.
  Changing the sampling settings changes these numbers, so the tables above must be regenerated
  from a fresh stage rather than copied.
