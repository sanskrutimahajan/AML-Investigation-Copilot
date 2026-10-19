# ADR 006: Customer attributes independent of seed status; ICIJ officer addresses

- **Status:** Accepted (2026-10-19)

## Context
Before Phase 2, checks against the real stage showed that customer-only fields revealed seed status.
- **Country:** the country was copied from the source, so 517 seeds had a country outside the 8 base
  Faker countries, and no plain customer did.
- **Name style:** ICIJ upper-case names gave 636 all-caps seeds against 64 plain customers.
- **Edit footprints:** the edits ran only on seeds, leaving commas, initials, dropped suffixes and
  doubled letters behind.

A first leak test after fixing countries and casing still scored **ROC-AUC 0.787**, driven by those
edit footprints. Separately, the ICIJ sample had only 71 `Address` nodes: Panama Papers addresses
hang off officers (61%) rather than companies (2.5%), and the sampler never followed officer
addresses.

## Decisions
1. **One attribute procedure for every role** (plain, look-alike, seed), in `customer_attributes.py`.
   Each customer has a *template* record from the same OpenSanctions/ICIJ pools. For seeds and
   look-alikes it is the source record; for plain customers it is a random record.
   - **Country:** the template's country with probability `customer_source_country_rate` (0.5),
     otherwise a base country.
   - **Date of birth:** copied from the template when it has a full date, otherwise Faker. The
     copies are listed in `copied_attributes`.
   - **Address:** one format for everyone, using real city names of the final country and real
     street names (street-type words only, never buildings or companies). The 8 base countries use
     native Faker.
2. **Plain names follow the watchlist origin mix.**
   - **People:** native Faker locales where Faker really provides them. Fallback locales are
     detected by provider module and excluded (for example ar_JO, el_CY, es_CA, fr_QC). Otherwise,
     tokens are recombined from same-origin watchlist names.
   - **Companies:** recombined watchlist vocabulary.
   - Either way, a full watchlist name is never reproduced.
3. **Same data-entry noise for every role** (`perturb()`):
   - The number of edits comes from one distribution, `name_edit_count_weights` = P(0,1,2) =
     (0.30, 0.45, 0.25), drawn from a random stream that does not depend on role. Seeds with 0 edits
     are exact copies.
   - Source conventions are equalized: people start from given-name order, then "Last, First"
     formatting (`person_comma_format_rate` 0.1) and upper case (`customer_uppercase_rate` 0.3) are
     applied at the same rate to everyone.
   - Choosing a real alias for a seed (`seed_alias_rate` 0.15) is a choice of base name, not an edit.
   - Tests check that drawn and applied edit counts, per-edit rates and casing rates match across
     roles. The edit log `customer_name_edits.parquet` is eval-only.
4. **Leak test** (`make leak-test`): logistic regression and gradient boosting on customer-only fields
   (name and address character n-grams, suffix type, token count, dates, country, capitalization,
   types), with 5-fold CV. The target is ROC-AUC ≤ 0.60.
5. **ICIJ officer addresses:** the sampled officers' `REGISTERED_ADDRESS` targets are added, with
   **no expansion from addresses**, and the node cap is raised to 22,000. That gives 1,763 Address
   nodes and 2,498 `REGISTERED_ADDRESS` edges.

## Result (`eval/results/leak_test_20261019T093339Z.json`)
| Comparison | Logistic regression | Gradient boosting |
| --- | --- | --- |
| Seeds vs plain | 0.527 ± 0.011 | **0.590 ± 0.008** |
| Seeds vs look-alikes | 0.526 ± 0.025 | 0.547 ± 0.035 |
| Seeds + look-alikes vs plain | 0.514 ± 0.011 | 0.569 ± 0.011 |

**Max 0.590: pass, but close to the target.** Residual signal is spread thinly: no single feature
group scores above 0.54 on its own, and the top coefficients are rare suffixes such as "Co. Ltd" and
"FZE", rare countries, and scattered character n-grams. Phase 2 entity resolution must beat this
**0.590 leakage baseline** on the same customer-level task (see the Phase 2 plan).

## Note for Phase 2
Addresses shared by many records (registered agents, nominee offices) are weak identity evidence.
If addresses are ever used for ER, for example SourceEntity ↔ SourceEntity, they must be
down-weighted by frequency, for instance with term-frequency adjustment. Customer addresses are
synthetic and share no source addresses, so they are excluded from Customer ↔ SourceEntity ER.
