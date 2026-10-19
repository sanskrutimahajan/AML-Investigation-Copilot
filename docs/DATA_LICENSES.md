# Data sources and licensing

Verified against each publisher's own page on 2026-10-19 (checked items are noted below). Terms can
change: re-check them for the exact snapshot you download, and record that snapshot's retrieval date in
the ingestion manifest. This file is not legal advice.

## IBM Transactions for Anti Money Laundering (AML) — HI-Small

- **Source:** Kaggle, `ealtman2019/ibm-transactions-for-anti-money-laundering-aml`
  (<https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml>).
  The dataset was last updated 2025-07-08 according to Kaggle's dataset metadata. Downloading requires
  a (free) Kaggle account; no download script is provided until the user chooses a method.
- **Files used:** `HI-Small_Trans.csv` (~476 MB), `HI-Small_Patterns.txt` (~324 KB), and
  `HI-Small_accounts.csv` (listed on Kaggle; size not yet recorded).
- **Publisher-stated scale (HI-Small):** about 5M transactions, 515K bank accounts, 5.1K laundering
  transactions, Sep 1–10 2022. The publisher notes that some laundering transactions fall after
  that date range.
- **License:** Community Data License Agreement – Sharing – Version 1.0 (CDLA-Sharing-1.0), as listed in
  Kaggle's dataset metadata.
- **Attribution:** The authors ask that published work cite Altman et al., "Realistic Synthetic
  Financial Transactions for Anti-Money Laundering Models" (<https://arxiv.org/abs/2306.16424>).
- **Note:** This is fully synthetic data. The GitHub repo `IBM/Multi-GNN` is Apache-2.0, but that
  license covers its code, not this data.
- **TODO:** Confirm the exact CSV headers, the format of the patterns file, and the columns of
  `HI-Small_accounts.csv` from the downloaded files. These have not been verified.

## OpenSanctions

- **Source:** <https://www.opensanctions.org/datasets/>; FollowTheMoney (FtM) entity bulk exports.
- **License:** Creative Commons Attribution-NonCommercial 4.0 (CC BY-NC 4.0)
  (<https://www.opensanctions.org/licensing/>).
- **Non-commercial use:** OpenSanctions' exemption page (<https://www.opensanctions.org/docs/commercial/exemption/>)
  gives student projects, hobby analysis and personal research as non-commercial examples. It says
  that use inside a for-profit business, including compliance screening, needs a commercial license.
- **TODO:** The page does not mention portfolio projects explicitly. Treat this one as non-commercial,
  and do not use it inside a company without checking with OpenSanctions first.
- **Snapshot in use (downloaded 2026-10-19):** `us_ofac_sdn` (20261019061005-mwr), `eu_fsf`
  (20261019065705-obm), `un_sc_sanctions` (20261019040111-fgv). Exact URLs and SHA-256 checksums are in
  `data/raw/opensanctions/MANIFEST.tsv`, which is not committed.
- **TODO:** Check the source attribution for each of the three lists.

## ICIJ Offshore Leaks Database

- **Source:** <https://offshoreleaks.icij.org/pages/database>; CSV archive
  <https://offshoreleaks-data.icij.org/offshoreleaks/csv/full-oldb.LATEST.zip>, which contains one CSV
  per node type plus one relationships CSV. It covers Offshore Leaks, Panama Papers, Bahamas Leaks,
  Paradise Papers, and Pandora Papers.
- **License:** The database is under the Open Database License (ODbL) and its contents under Creative
  Commons Attribution-ShareAlike, per ICIJ's download page.
- **Attribution:** "Always cite the International Consortium of Investigative Journalists when using
  this data."
- **Disclaimer:** Inclusion in the database does not imply wrongdoing.
- **Snapshot in use (downloaded 2026-10-19):** `full-oldb.LATEST.zip`, generated 2026-09-09. The
  SHA-256 checksum is in `data/raw/icij/MANIFEST.tsv`, which is not committed.
- **TODO:** Neither ICIJ's page nor the archive (which contains no license file) gives version
  numbers for ODbL or CC BY-SA. ODbL share-alike terms apply to any database derived from
  this data and then publicly used, so do not publish derived data before reviewing them.

## Handling rules

- All raw and processed data stays under `data/`, which is gitignored.
- Each ingestion run writes a manifest with the source URL, retrieval date, file checksums, and license.
- Links between IBM accounts and OpenSanctions/ICIJ names are **synthetic** and are not evidence about
  any real person or company.
