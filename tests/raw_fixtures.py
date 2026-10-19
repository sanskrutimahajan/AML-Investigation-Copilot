"""Tiny synthetic raw files that mimic the real source formats (no real data, no downloads)."""

import json
from pathlib import Path

from tracegraph.config import Settings

IBM_HEADER = (
    "Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,"
    "Amount Paid,Payment Currency,Payment Format,Is Laundering"
)
# Rows 1-2 form a FAN-OUT pattern, row 3 is laundering outside any pattern, row 4 is a
# self-transfer, rows 5-24 make H1 a hub, rows 25-26 are ordinary payments.
IBM_ROWS = [
    "2022/09/01 00:00,010,A1,020,B1,100.00,US Dollar,100.00,US Dollar,ACH,1",
    "2022/09/01 00:05,010,A1,030,C1,200.00,US Dollar,200.00,US Dollar,ACH,1",
    "2022/09/01 01:00,030,C1,010,D1,50.00,Euro,50.00,Euro,Wire,1",
    "2022/09/01 02:00,050,S1,050,S1,10.00,US Dollar,10.00,US Dollar,Reinvestment,0",
    *[
        f"2022/09/02 {i:02d}:00,040,H1,060,X{i},{i + 1}.00,US Dollar,{i + 1}.00,US Dollar,Cheque,0"
        for i in range(20)
    ],
    "2022/09/03 00:00,010,D1,040,H1,75.00,US Dollar,75.00,US Dollar,Credit Card,0",
    "2022/09/03 01:00,020,B1,010,D1,12.50,US Dollar,12.50,US Dollar,Cash,0",
]
IBM_PATTERNS = """BEGIN LAUNDERING ATTEMPT - FAN-OUT:  Max 2-degree Fan-Out
2022/09/01 00:00,010,A1,020,B1,100.00,US Dollar,100.00,US Dollar,ACH,1
2022/09/01 00:05,010,A1,030,C1,200.00,US Dollar,200.00,US Dollar,ACH,1
END LAUNDERING ATTEMPT - FAN-OUT

"""
IBM_ACCOUNTS = [
    "Bank Name,Bank ID,Account Number,Entity ID,Entity Name",
    "US Bank #1,10,A1,E1,Corporation #1",
    "US Bank #2,20,B1,E1,Corporation #1",
    "UK Bank #3,30,C1,E2,Individual #2",
    "US Bank #1,10,D1,E3,Sole Proprietorship #3",
    "DE Bank #4,40,H1,E4,Partnership #4",
    "FR Bank #5,50,S1,E5,Corporation #5",
    *[f"NL Bank #6,60,X{i},E{6 + i % 4},Partnership #{6 + i % 4}" for i in range(20)],
]


def _ftm(entity_id: str, schema: str, datasets: list[str], **props: list[str]) -> dict[str, object]:
    return {"id": entity_id, "schema": schema, "datasets": datasets, "properties": props}


OPENSANCTIONS_FILES = {
    "list_a": [
        _ftm(
            "NK-p1",
            "Person",
            ["list_a"],
            name=["Mohammed Yusuf Khan"],
            alias=["M. Y. Khan"],
            birthDate=["1970-02-03"],
            country=["pk"],
            topics=["sanction"],
        ),
        _ftm(
            "NK-o1", "Organization", ["list_a"], name=["Ocean Ventures Trading LLC"], country=["ae"]
        ),
        _ftm("NK-noname", "Person", ["list_a"]),
        _ftm("addr-1", "Address", ["list_a"], full=["1 Main St"]),
        _ftm("own-1", "Ownership", ["list_a"], owner=["NK-p1"], asset=["NK-o1"], role=["owner"]),
        _ftm("own-2", "Ownership", ["list_a"], owner=["NK-p1"], asset=["addr-1"]),
    ],
    "list_b": [
        _ftm(
            "NK-p1",
            "Person",
            ["list_b"],
            name=["Mohammed Yusuf Khan"],
            weakAlias=["Abu Yusuf"],
            nationality=["af"],
        ),
        _ftm("NK-p2", "Person", ["list_b"], name=["Sergei Ivanovich Petrov"]),
        _ftm("NK-c1", "Company", ["list_b"], name=["Petrov Holdings GmbH"]),
        _ftm("dir-1", "Directorship", ["list_b"], director=["NK-p2"], organization=["NK-c1"]),
    ],
}

ICIJ_ENTITY_HEADER = (
    "node_id,name,original_name,former_name,jurisdiction,jurisdiction_description,company_type,"
    "address,internal_id,incorporation_date,inactivation_date,struck_off_date,dorm_date,status,"
    "service_provider,ibcRUC,country_codes,countries,sourceID,valid_until,note"
)
ICIJ_FILES = {
    "nodes-entities.csv": [ICIJ_ENTITY_HEADER]
    + [
        f"{i},ALPHA {i} HOLDINGS LIMITED,,,BVI,British Virgin Islands,,,,01-JAN-2000,,,,Active,"
        f",,VGB,British Virgin Islands,Panama Papers,,"
        for i in range(1, 7)
    ]
    + ["7,OTHER LEAK LTD,,,BVI,British Virgin Islands,,,,,,,,,,,VGB,BVI,Offshore Leaks,,"],
    "nodes-officers.csv": [
        "node_id,name,countries,country_codes,sourceID,valid_until,note",
        "10,Anna Maria Schmidt,Germany,DEU,Panama Papers,,",
        "11,THE BEARER,,,Panama Papers,,",
        "12,MAYO NOMINEES LIMITED,Ireland,IRL,Panama Papers,,",
    ],
    "nodes-intermediaries.csv": [
        "node_id,name,status,internal_id,address,countries,country_codes,sourceID,valid_until,note",
        "20,Law Firm SA,ACTIVE,,,Panama,PAN,Panama Papers,,",
    ],
    "nodes-addresses.csv": [
        "node_id,address,name,countries,country_codes,sourceID,valid_until,note",
        "30,1 Harbour Rd Hong Kong,,Hong Kong,HKG,Panama Papers,,",
    ],
    "nodes-others.csv": [
        "node_id,name,type,incorporation_date,struck_off_date,closed_date,jurisdiction,"
        "jurisdiction_description,countries,country_codes,sourceID,valid_until,note",
    ],
    "relationships.csv": [
        "node_id_start,node_id_end,rel_type,link,status,start_date,end_date,sourceID",
        "10,1,officer_of,director of,,,,Panama Papers",
        "10,2,officer_of,director of,,,,Panama Papers",
        "11,3,officer_of,shareholder of,,,,Panama Papers",
        "12,4,officer_of,nominee director of,,,,Panama Papers",
        "12,5,officer_of,nominee director of,,,,Panama Papers",
        "20,1,intermediary_of,intermediary of,,,,Panama Papers",
        "1,30,registered_address,registered address,,,,Panama Papers",
        "1,7,same_as,same as,,,,",
        "2,3,similar,similar name and address as,,,,",
    ],
}


def write_ibm(raw_dir: Path) -> Path:
    directory = raw_dir / "ibm_aml"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "HI-Small_Trans.csv").write_text("\n".join([IBM_HEADER, *IBM_ROWS]) + "\n")
    (directory / "HI-Small_Patterns.txt").write_text(IBM_PATTERNS)
    (directory / "HI-Small_accounts.csv").write_text("\n".join(IBM_ACCOUNTS) + "\n")
    return directory


def write_opensanctions(raw_dir: Path) -> Path:
    directory = raw_dir / "opensanctions"
    directory.mkdir(parents=True, exist_ok=True)
    for name, records in OPENSANCTIONS_FILES.items():
        lines = [json.dumps(record) for record in records]
        (directory / f"{name}.entities.ftm.json").write_text("\n".join(lines) + "\n")
    return directory


def write_icij(raw_dir: Path) -> Path:
    directory = raw_dir / "icij"
    (directory / "csv").mkdir(parents=True, exist_ok=True)
    for name, lines in ICIJ_FILES.items():
        (directory / "csv" / name).write_text("\n".join(lines) + "\n")
    return directory


def write_all(data_dir: Path) -> None:
    raw = data_dir / "raw"
    write_ibm(raw)
    write_opensanctions(raw)
    write_icij(raw)


def fixture_settings(data_dir: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": data_dir,
        "ibm_benign_seed_accounts": 2,
        "ibm_max_txns_per_account": 5,
        "ibm_max_transactions": 1_000,
        "opensanctions_datasets": list(OPENSANCTIONS_FILES),
        "icij_seed_entity_count": 2,
        "icij_node_sample_size": 50,
        "seeded_link_rate": 0.5,
        "hard_negative_rate": 0.2,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]
