"""Functional tests for Repository Search Tool."""

import pytest

from akd.structures import SearchResultItem
from akd.tools.misc import HttpUrlAdapter

from akd_ext.structures import SDEIndexedDocumentType
from akd_ext.tools.code_search.repository_search import (
    RepositorySearchTool,
    RepositorySearchToolConfig,
    RepositorySearchToolInputSchema,
    RepositorySearchToolOutputSchema,
    _github_repo_name,
)
from akd_ext.tools.sde_search import SDESearchToolOutputSchema


class TestRepositorySearchTool:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query",
        [
            "nasa python",
            "pds software",
        ],
    )
    async def test_repository_search_tool(self, query: str):
        """Test Repository Search Tool functionality.

        Args:
            query: Code search query to test
        """
        config = RepositorySearchToolConfig(page_size=2)
        tool = RepositorySearchTool(config=config)
        result = await tool.arun(RepositorySearchToolInputSchema(queries=[query]))

        assert isinstance(result, RepositorySearchToolOutputSchema)
        assert len(result.results) > 0

        # Verify each result has repository metadata and reliability score
        for item in result.results:
            assert hasattr(item, "repository_metadata")
            assert hasattr(item, "reliability_score")
            assert item.repository_metadata.stars >= 0
            assert item.repository_metadata.forks >= 0

    @pytest.mark.parametrize(
        "page_size,result_size",
        [
            (3, 3),
            (8, 8),
            (10, 10),
            (11, 10),  # Capped at max 10
            (55, 10),  # Capped at max 10
            (100, 10),  # Capped at max 10
        ],
    )
    @pytest.mark.asyncio
    async def test_repository_search_tool_results_number(self, page_size: int, result_size: int):
        """Test Repository Search Tool results number."""
        config = RepositorySearchToolConfig(page_size=page_size)
        tool = RepositorySearchTool(config=config)
        # "nasa python" has >100 results in the SDE code index so any page_size up to the 10 cap works
        result = await tool.arun(RepositorySearchToolInputSchema(queries=["nasa python"]))
        assert len(result.results) == result_size


class TestGithubRepoName:
    """The SDE index mixes web pages in with repositories, so URL parsing must not assume owner/repo."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://github.com/NASA-IMPACT/veda-config-ghg", "NASA-IMPACT/veda-config-ghg"),
            ("https://github.com/SnowEx/uavsar_snow/", "SnowEx/uavsar_snow"),
            ("https://www.github.com/owner/repo/tree/main", "owner/repo"),
            ("https://github.com/NASA-IMPACT", None),  # owner only, no repo
            ("https://github.com/", None),
            ("https://nuwrf.gsfc.nasa.gov/wrf", None),  # sde-web page, not a repo
        ],
    )
    def test_github_repo_name(self, url: str, expected: str | None):
        assert _github_repo_name(url) == expected


class TestRepositorySearchBackend:
    @pytest.mark.unit
    def test_defaults_target_unified_search_backend(self):
        """Repository search reads the same SDE host/min_score contract as SDESearchTool."""
        config = RepositorySearchToolConfig()
        assert config.min_score == 0.0
        assert RepositorySearchTool(config=config).sde_tool.config.min_score == 0.0

    @pytest.mark.unit
    async def test_query_is_scoped_to_software_and_tools(self, monkeypatch):
        """The SDE query must be filtered to code, since /api/search spans all document types."""
        tool = RepositorySearchTool(config=RepositorySearchToolConfig(page_size=4))
        captured: dict = {}

        async def _arun(params, **kwargs):
            captured["params"] = params
            return SDESearchToolOutputSchema(results=[], extra={})

        monkeypatch.setattr(tool.sde_tool, "arun", _arun)
        await tool._arun_single_query("radar reader", max_results=2)

        assert captured["params"].doc_type == SDEIndexedDocumentType.SOFTWARE_TOOLS
        assert captured["params"].limit == 4

    @pytest.mark.unit
    async def test_enrichment_skips_non_github_results(self):
        """A non-GitHub hit yields empty metadata rather than raising on URL parsing."""
        tool = RepositorySearchTool()
        item = SearchResultItem(
            query="q",
            title="NU-WRF",
            content="",
            url=HttpUrlAdapter.validate_python("https://nuwrf.gsfc.nasa.gov/"),
        )

        enriched = await tool._enrich_code_search_with_metadata(item)

        assert enriched.reliability_score is None
        assert enriched.repository_metadata.is_null_metadata
