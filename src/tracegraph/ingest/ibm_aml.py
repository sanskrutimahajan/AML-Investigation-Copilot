"""IBM AML HI-Small: parse transactions, accounts and patterns; take a pattern-preserving sample.

Source: https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml
License: CDLA-Sharing-1.0 (see docs/DATA_LICENSES.md). Data is synthetic.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

from tracegraph.config import Settings

TRANSACTIONS_FILE = "HI-Small_Trans.csv"
ACCOUNTS_FILE = "HI-Small_accounts.csv"
PATTERNS_FILE = "HI-Small_Patterns.txt"

# The source header repeats "Account" for sender and receiver, so columns are renamed by position.
TRANSACTIONS_HEADER = [
    "Timestamp",
    "From Bank",
    "Account",
    "To Bank",
    "Account",
    "Amount Received",
    "Receiving Currency",
    "Amount Paid",
    "Payment Currency",
    "Payment Format",
    "Is Laundering",
]
ACCOUNTS_HEADER = ["Bank Name", "Bank ID", "Account Number", "Entity ID", "Entity Name"]
RAW_COLUMNS = [
    "timestamp",
    "from_bank",
    "from_account",
    "to_bank",
    "to_account",
    "amount_received",
    "receiving_currency",
    "amount_paid",
    "payment_currency",
    "payment_format",
    "is_laundering",
]
# Fields that identify a transaction when matching pattern-file lines back to the transactions file.
MATCH_COLUMNS = [
    "timestamp",
    "from_account_id",
    "to_account_id",
    "amount_received",
    "receiving_currency",
    "amount_paid",
    "payment_currency",
    "payment_format",
]
_PATTERN_BEGIN = re.compile(r"^BEGIN LAUNDERING ATTEMPT - ([A-Z-]+):?\s*(.*)$")


@dataclass(frozen=True)
class IbmStage:
    """Staging tables plus label ground truth; labels never enter the staging tables."""

    transactions: pl.DataFrame
    accounts: pl.DataFrame
    banks: pl.DataFrame
    pattern_ground_truth: pl.DataFrame
    laundering_account_ids: frozenset[str]
    seed_accounts: pl.DataFrame
    stats: dict[str, int]


def _check_header(path: Path, expected: list[str]) -> None:
    with path.open(encoding="utf-8") as handle:
        header = handle.readline().rstrip("\r\n").split(",")
    if header != expected:
        raise ValueError(f"{path.name}: unexpected header {header}; expected {expected}")


def _bank_id(column: str) -> pl.Expr:
    # Transactions pad bank IDs with zeros ("010"); the accounts file does not ("10").
    return pl.col(column).cast(pl.Int64).cast(pl.Utf8)


def _normalize(raw: pl.LazyFrame) -> pl.LazyFrame:
    return raw.select(
        pl.col("timestamp").str.strptime(pl.Datetime("us"), "%Y/%m/%d %H:%M"),
        _bank_id("from_bank").alias("from_bank_id"),
        pl.format("{}:{}", _bank_id("from_bank"), "from_account").alias("from_account_id"),
        _bank_id("to_bank").alias("to_bank_id"),
        pl.format("{}:{}", _bank_id("to_bank"), "to_account").alias("to_account_id"),
        pl.col("amount_received").cast(pl.Float64),
        "receiving_currency",
        pl.col("amount_paid").cast(pl.Float64),
        "payment_currency",
        "payment_format",
        (pl.col("is_laundering") == "1").alias("is_laundering"),
        *(["source_row"] if "source_row" in raw.collect_schema() else []),
    )


def read_transactions(path: Path) -> pl.DataFrame:
    """Read and normalize all transactions; `source_row` is the 1-based data row in the file."""
    _check_header(path, TRANSACTIONS_HEADER)
    raw = pl.scan_csv(
        path,
        has_header=False,
        skip_rows=1,
        new_columns=RAW_COLUMNS,
        schema_overrides=dict.fromkeys(RAW_COLUMNS, pl.Utf8),
    ).with_row_index("source_row", offset=1)
    return (
        _normalize(raw)
        .with_columns(
            pl.format("ibm-{}", "source_row").alias("transaction_id"),
            pl.col("source_row").cast(pl.Int64),
        )
        .collect()
    )


def read_accounts(path: Path) -> pl.DataFrame:
    _check_header(path, ACCOUNTS_HEADER)
    raw = pl.read_csv(path, infer_schema=False)
    return raw.select(
        pl.format("{}:{}", _bank_id("Bank ID"), "Account Number").alias("account_id"),
        _bank_id("Bank ID").alias("bank_id"),
        pl.col("Account Number").alias("account_number"),
        pl.col("Bank Name").alias("bank_name"),
        pl.col("Entity ID").alias("entity_id"),
        pl.col("Entity Name").str.replace(r"\s*#\d+$", "").alias("entity_type"),
    ).unique("account_id", keep="first", maintain_order=True)


def read_patterns(path: Path) -> pl.DataFrame:
    """Parse BEGIN/END blocks into one row per pattern line with its pattern ID and typology."""
    rows: list[dict[str, str | int]] = []
    pattern_id: str | None = None
    typology = description = ""
    count = 0
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            if begin := _PATTERN_BEGIN.match(line):
                count += 1
                pattern_id = f"ibm-pattern-{count:04d}"
                typology, description = begin.group(1), begin.group(2).strip()
            elif line.startswith("END LAUNDERING ATTEMPT"):
                pattern_id = None
            else:
                if pattern_id is None:
                    raise ValueError(f"{path.name}:{line_no}: transaction outside a pattern block")
                fields = line.split(",")
                if len(fields) != len(RAW_COLUMNS):
                    raise ValueError(f"{path.name}:{line_no}: expected {len(RAW_COLUMNS)} fields")
                rows.append(
                    {
                        "pattern_id": pattern_id,
                        "typology": typology,
                        "pattern_description": description,
                        "pattern_line": line_no,
                        **dict(zip(RAW_COLUMNS, fields, strict=True)),
                    }
                )
    raw = pl.DataFrame(rows, schema={k: pl.Utf8 for k in rows[0]} if rows else None)
    meta = raw.select("pattern_id", "typology", "pattern_description", "pattern_line")
    return pl.concat([meta, _normalize(raw.lazy()).collect()], how="horizontal").with_columns(
        pl.col("pattern_line").cast(pl.Int64)
    )


def match_patterns(patterns: pl.DataFrame, transactions: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """Attach a transaction_id to each pattern line; returns (matches, ambiguous line count).

    A line can match several identical rows; the lowest source row is chosen deterministically.
    """
    joined = patterns.join(
        transactions.select([*MATCH_COLUMNS, "transaction_id", "source_row"]),
        on=MATCH_COLUMNS,
        how="left",
    )
    unmatched = joined.filter(pl.col("transaction_id").is_null())
    if unmatched.height:
        raise ValueError(f"{unmatched.height} pattern lines have no matching transaction")
    per_line = joined.group_by("pattern_line").agg(pl.len().alias("n"))
    ambiguous = per_line.filter(pl.col("n") > 1).height
    matched = (
        joined.sort("source_row")
        .unique("pattern_line", keep="first")
        .select("pattern_id", "typology", "pattern_description", "transaction_id")
        .unique(maintain_order=True)
        .sort("pattern_id", "transaction_id")
    )
    return matched, ambiguous


def sample_transactions(
    transactions: pl.DataFrame, core_ids: pl.Series, config: Settings
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Keep every core (laundering or pattern) transaction plus capped neighbourhoods of seeds.

    Returns (sampled transactions, seed accounts with `seed_type` "core" or "benign"). Seed type
    reveals which accounts touch laundering, so it is eval-only and never staged.
    """
    rng = np.random.default_rng(config.random_seed)
    core = transactions.filter(pl.col("transaction_id").is_in(core_ids.implode()))
    core_accounts = pl.concat([core["from_account_id"], core["to_account_id"]]).unique()

    candidates = transactions.filter(~pl.col("transaction_id").is_in(core_ids.implode()))
    if config.ibm_drop_self_transfers:
        candidates = candidates.filter(pl.col("from_account_id") != pl.col("to_account_id"))

    others = pl.concat([candidates["from_account_id"], candidates["to_account_id"]]).unique()
    others = others.filter(~others.is_in(core_accounts.implode())).sort()
    n_benign = min(config.ibm_benign_seed_accounts, others.len())
    benign = others.gather(np.sort(rng.choice(others.len(), size=n_benign, replace=False)))
    seeds = pl.concat([core_accounts, benign]).unique()

    # One random draw per candidate row gives each seed a deterministic, unbiased ordering.
    candidates = candidates.with_columns(pl.Series("_order", rng.random(candidates.height)))
    seed_list = seeds.implode()
    sides = pl.concat(
        [
            candidates.filter(pl.col(side).is_in(seed_list)).select(
                pl.col(side).alias("_seed"), "transaction_id", "_order"
            )
            for side in ("from_account_id", "to_account_id")
        ]
    )
    capped = (
        sides.with_columns(pl.col("_order").rank("ordinal").over("_seed").alias("_rank"))
        .filter(pl.col("_rank") <= config.ibm_max_txns_per_account)
        .get_column("transaction_id")
        .unique()
    )
    keep = pl.concat([core_ids, capped]).unique()
    if keep.len() > config.ibm_max_transactions:
        raise ValueError(
            f"IBM sample has {keep.len()} transactions, above ibm_max_transactions="
            f"{config.ibm_max_transactions}; lower ibm_benign_seed_accounts or "
            "ibm_max_txns_per_account instead of truncating (truncation breaks patterns)."
        )
    seed_accounts = pl.concat(
        [
            pl.DataFrame({"account_id": core_accounts.sort(), "seed_type": "core"}),
            pl.DataFrame({"account_id": benign.sort(), "seed_type": "benign"}),
        ]
    )
    sample = transactions.filter(pl.col("transaction_id").is_in(keep.implode()))
    return sample.sort("source_row"), seed_accounts


def build_ibm_stage(raw_dir: Path, config: Settings) -> IbmStage:
    transactions = read_transactions(raw_dir / TRANSACTIONS_FILE)
    accounts = read_accounts(raw_dir / ACCOUNTS_FILE)
    patterns, ambiguous = match_patterns(read_patterns(raw_dir / PATTERNS_FILE), transactions)

    laundering_ids = transactions.filter(pl.col("is_laundering")).get_column("transaction_id")
    core_ids = pl.concat([laundering_ids, patterns["transaction_id"]]).unique()
    sample, seed_accounts = sample_transactions(transactions, core_ids, config)

    labels = sample.filter(pl.col("is_laundering")).select("transaction_id")
    pattern_ground_truth = (
        pl.concat([labels, patterns.select("transaction_id")])
        .unique()
        .join(patterns, on="transaction_id", how="left")
        .join(sample.select("transaction_id", "is_laundering"), on="transaction_id", how="left")
        .select(
            "transaction_id",
            "is_laundering",
            "pattern_id",
            "typology",
            "pattern_description",
        )
        .sort("transaction_id", "pattern_id", nulls_last=True)
    )
    laundering_accounts = frozenset(
        pl.concat(
            [
                sample.filter(pl.col("is_laundering"))["from_account_id"],
                sample.filter(pl.col("is_laundering"))["to_account_id"],
            ]
        ).to_list()
    )

    endpoints = pl.concat(
        [
            sample.select(account_id="from_account_id", bank_id="from_bank_id"),
            sample.select(account_id="to_account_id", bank_id="to_bank_id"),
        ]
    ).unique("account_id")
    sampled_accounts = (
        endpoints.join(accounts.drop("bank_id"), on="account_id", how="left")
        .with_columns(
            pl.col("account_number").fill_null(pl.col("account_id").str.split(":").list.last())
        )
        .select("account_id", "bank_id", "account_number", "bank_name", "entity_id", "entity_type")
        .sort("account_id")
    )
    banks = (
        sampled_accounts.select("bank_id", "bank_name")
        .sort("bank_id", "bank_name", nulls_last=True)
        .unique("bank_id", keep="first")
        .sort("bank_id")
    )
    staged_transactions = sample.select(
        "transaction_id",
        "timestamp",
        "from_account_id",
        "to_account_id",
        "amount_paid",
        "payment_currency",
        "amount_received",
        "receiving_currency",
        "payment_format",
        "source_row",
    )
    stats = {
        "source_transactions": transactions.height,
        "source_laundering_transactions": laundering_ids.len(),
        "source_pattern_blocks": patterns["pattern_id"].n_unique(),
        "pattern_lines_ambiguous": ambiguous,
        "sampled_transactions": staged_transactions.height,
        "sampled_laundering_transactions": labels.height,
        "sampled_accounts": sampled_accounts.height,
        "sampled_accounts_without_entity": sampled_accounts["entity_id"].null_count(),
        "sampled_banks": banks.height,
    }
    return IbmStage(
        transactions=staged_transactions,
        accounts=sampled_accounts,
        banks=banks,
        pattern_ground_truth=pattern_ground_truth,
        laundering_account_ids=laundering_accounts,
        seed_accounts=seed_accounts,
        stats=stats,
    )
