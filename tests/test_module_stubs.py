"""Import checks for the planned module stubs."""

from importlib import import_module

import pytest

STUB_MODULES = [
    "tracegraph.ingest.ibm_aml",
    "tracegraph.ingest.opensanctions",
    "tracegraph.ingest.icij",
    "tracegraph.ingest.synthetic_profiles",
    "tracegraph.ingest.seed_links",
    "tracegraph.graph.schema",
    "tracegraph.graph.loaders",
    "tracegraph.graph.cypher_templates",
    "tracegraph.er.normalize",
    "tracegraph.er.blocking",
    "tracegraph.er.splink_model",
    "tracegraph.er.adjudicator",
    "tracegraph.retrieval.router",
    "tracegraph.retrieval.entity_linking",
    "tracegraph.retrieval.graph_retriever",
    "tracegraph.retrieval.text2cypher",
    "tracegraph.retrieval.vector_retriever",
    "tracegraph.retrieval.fusion",
    "tracegraph.generation.answer",
    "tracegraph.generation.grounding_check",
    "tracegraph.generation.sar_drafter",
    "tracegraph.detection.patterns",
    "tracegraph.detection.alerts",
]


@pytest.mark.parametrize("module_name", STUB_MODULES)
def test_module_stub_imports(module_name: str) -> None:
    assert import_module(module_name).__doc__
