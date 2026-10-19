"""Fixtures for tests against the throwaway Neo4j container (`make test-integration`)."""

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase
from neo4j.exceptions import ServiceUnavailable
from raw_fixtures import fixture_settings, write_all

from tracegraph.config import Settings
from tracegraph.ingest import stage

DATABASE = "neo4j"


@pytest.fixture(scope="session")
def test_driver() -> Iterator[Driver]:
    uri = Settings(_env_file=None).neo4j_test_uri
    if urlparse(uri).port == 7687:
        pytest.fail("refusing to run integration tests against the main graph port 7687")
    driver = GraphDatabase.driver(uri, auth=None)
    try:
        driver.verify_connectivity()
    except (ServiceUnavailable, OSError):
        driver.close()
        pytest.skip(f"test Neo4j is not running at {uri}; use `make test-integration`")
    yield driver
    driver.close()


@pytest.fixture
def empty_graph(test_driver: Driver) -> Driver:
    test_driver.execute_query("MATCH (n) DETACH DELETE n", database_=DATABASE)
    return test_driver


@pytest.fixture(scope="session")
def staged(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    data_dir: Path = tmp_path_factory.mktemp("data")
    write_all(data_dir)
    config = fixture_settings(data_dir)
    stage.run(config)
    return config
