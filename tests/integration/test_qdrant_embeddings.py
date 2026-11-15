"""Qdrant indexing and nearest-neighbour search against the throwaway Qdrant (port 6335)."""

from urllib.parse import urlparse

import polars as pl
import pytest
from qdrant_client import QdrantClient
from test_er_pipeline import HashEmbedder

from tracegraph.config import Settings
from tracegraph.er.embeddings import index_sources, nearest_sources

pytestmark = pytest.mark.integration


@pytest.fixture
def qdrant() -> QdrantClient:
    url = Settings(_env_file=None).qdrant_test_url
    if urlparse(url).port == 6333:
        pytest.fail("refusing to run integration tests against the main Qdrant port 6333")
    client = QdrantClient(url=url)
    try:
        client.get_collections()
    except Exception:
        pytest.skip(f"test Qdrant is not running at {url}; use `make test-integration`")
    return client


def test_nearest_sources_respects_kind_and_aliases(qdrant: QdrantClient) -> None:
    sources = pl.DataFrame(
        {
            "unique_id": ["s1", "s2", "s3"],
            "kind": ["person", "person", "organization"],
            "names": [["sergei petrov", "sergey petrov"], ["anna schmidt"], ["sergei petrov llc"]],
        }
    )
    customers = pl.DataFrame(
        {"unique_id": ["c1"], "kind": ["person"], "name_norm": ["sergey petrof"]}
    )
    vectors = index_sources(qdrant, "it_names", sources, HashEmbedder())
    assert vectors.shape == (3, HashEmbedder.dimension)
    ids, _ = nearest_sources(qdrant, "it_names", customers, HashEmbedder(), top_k=2)
    assert ids[0][0] == "s1"
    assert "s3" not in ids[0]  # organizations are never candidates for a person
    qdrant.delete_collection("it_names")
