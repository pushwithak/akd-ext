"""
Repository search over NASA's Science Discovery Engine (SDE).

Finds scientific code repositories by querying the SDE ``/api/search`` endpoint
scoped to ``Software and Tools`` documents, then enriches each GitHub hit with
repository metadata and a computed reliability score.

The SDE request itself is delegated to :class:`SDESearchTool` so that the search
backend (endpoint, search mode, ``min_score``) is configured in exactly one place.
"""

import asyncio
import os
from typing import Literal
from urllib.parse import urlparse

from loguru import logger
from pydantic import Field, computed_field, model_validator

from akd.structures import SearchResultItem
from akd.tools.misc import HttpUrlAdapter
from akd.tools.search import (
    SearchTool,
    SearchToolConfig,
    SearchToolInputSchema,
    SearchToolOutputSchema,
)

from akd_ext.mcp import mcp_tool
from akd_ext.structures import SDEIndexedDocumentType
from ..sde_search import SDEDocument, SDESearchTool, SDESearchToolConfig, SDESearchToolInputSchema
from .utils import RepositoryMetadata, fetch_github_metadata, calculate_reliability_score


# Schemas (formerly inherited from akd.tools.search.code_search; ported locally
# after that module was removed upstream — see akd commit 771d7c3.)
class CodeSearchToolInputSchema(SearchToolInputSchema):
    """Input schema for code search; exposes ``top_k`` as an alias for ``max_results``."""

    @computed_field
    def top_k(self) -> int:
        return self.max_results


class CodeSearchToolOutputSchema(SearchToolOutputSchema):
    """Output schema for code search."""


class RepositorySearchResultItem(SearchResultItem):
    """
    Search result item with added github repository metadata and computed reliability score.
    """

    reliability_score: float | None = Field(
        default=None,
        description="Computed reliability score based on github repository metadata. If none, treat it neutrally as if there is no reliability score.",
    )
    repository_metadata: RepositoryMetadata = Field(
        default_factory=RepositoryMetadata,
        description="Github repository metadata. includes number of stars, forks, open issues, open pull requests, and closed pull requests.",
    )

    @model_validator(mode="before")
    @classmethod
    def convert_parent_instance(cls, data):
        """
        While we call super()._arun(params), the parent pydantic validation runs on the parents output schema.
        The data of the parent instance is SearchResultItem. However, the data of this cls is RepositorySearchResultItem.
        To avoid this pydantic validation inconsistency on results, we need to return the model dump of the parent instance.
        TODO: fix this issue in the core
        """
        if isinstance(data, SearchResultItem) and not isinstance(data, cls):
            return data.model_dump()
        return data


# Tool input and output schemas
class RepositorySearchToolInputSchema(CodeSearchToolInputSchema):
    """
    Input query for the repository search tool. Its a text based query that initializes the relevant code search tool.
    """


class RepositorySearchToolOutputSchema(CodeSearchToolOutputSchema):
    """
    Output schema for the repository search tool.
    """

    results: list[RepositorySearchResultItem] = Field(
        ...,
        description="List of search result items with added github repository metadata and computed reliability score.",
    )


# Tool config schema
class RepositorySearchToolConfig(SearchToolConfig):
    """
    Config schema for the repository search tool.
    """

    # SDE search backend. base_url is the API host; the tool always queries
    # /api/search filtered to "Software and Tools" documents.
    base_url: str = Field(
        default_factory=lambda: os.getenv("SDE_BASE_URL", "https://dyejsbdumgpqz.cloudfront.net"),
        description="SDE API host.",
    )
    page_size: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Number of documents to fetch from the SDE API per query, before trimming to max_results.",
    )
    search_mode: Literal["hybrid", "vector", "keyword"] = Field(default="hybrid", description="SDE search mode.")
    min_score: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Lower bound on the _score a document must reach to be returned. Sent on every request "
            "because the server applies its own default of 0.55 when the field is omitted, which is "
            "above the score most documents receive and silently drops them."
        ),
    )

    # URL is the only stable identity signal for code repositories, so RRF and
    # deduplication are restricted to it (vs the upstream default of doi/title/url).
    rrf_keys: list[str] = Field(default_factory=lambda: ["url"])
    deduplication_keys: list[str] = Field(default_factory=lambda: ["url"])
    # SDE results don't carry resolvable DOIs; skip the resolver pass.
    result_normalization: bool = Field(default=False)

    access_token: str | None = Field(
        default_factory=lambda: os.getenv("GITHUB_ACCESS_TOKEN", None),
        description="GitHub access token.",
    )


def _github_repo_name(url: str) -> str | None:
    """Return ``owner/repo`` for a GitHub URL, or None when the URL isn't one.

    SDE indexes web pages alongside repositories, so a result URL is not
    guaranteed to carry an owner and repo path segment.
    """
    parsed = urlparse(url)
    if parsed.netloc.lower().removeprefix("www.") != "github.com":
        return None
    parts = [segment for segment in parsed.path.split("/") if segment]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"


# Tool implementation
@mcp_tool
class RepositorySearchTool(SearchTool):
    """
    Search for relevant code and implementations within specialized science repositories.

    This tool performs a targeted search across curated scientific codebases to find
    relevant GitHub repositories with README. It enriches the search results with
    GitHub metadata such as stars, forks, and development activity, which are then
    used to compute a reliability score for each item.

    The reliability score (0-100) is a weighted average of repository maturity, activity, and community trust.

    The formula: Score = (Age * 0.20) + (Activity * 0.25) + (Stars * 0.25) + (Forks * 0.15) + (History * 0.15)

    How components are calculated:
      - Age (20%): Higher for older repos; reaches 100% after 4 years.
      - Activity (25%): Starts at 100% and drops to 0% if the repo hasn't been updated in a year.
      - Stars (25%): Logarithmic scale where ~1,000 stars = 100%.
      - Forks (15%): Logarithmic scale where ~500 forks = 100%.
      - History (15%): Based on the span between the first commit and now; reaches 100% after 4 years.
    """

    input_schema = RepositorySearchToolInputSchema
    output_schema = RepositorySearchToolOutputSchema
    config_schema = RepositorySearchToolConfig

    @property
    def sde_tool(self) -> SDESearchTool:
        """SDE search tool used as this tool's search backend, built once per instance."""
        if getattr(self, "_sde_tool", None) is None:
            self._sde_tool = SDESearchTool(
                config=SDESearchToolConfig(
                    base_url=self.config.base_url,
                    search_type=self.config.search_mode,
                    min_score=self.config.min_score,
                    timeout=float(self.config.timeout),
                ),
            )
        return self._sde_tool

    async def _arun_single_query(
        self,
        query: str,
        max_results: int,
        **kwargs,
    ) -> SearchToolOutputSchema:
        """Fetch a single query's worth of ``Software and Tools`` documents from the SDE API."""
        try:
            sde_result = await self.sde_tool.arun(
                SDESearchToolInputSchema(
                    query=query,
                    limit=self.config.page_size,
                    doc_type=SDEIndexedDocumentType.SOFTWARE_TOOLS,
                ),
            )
        except Exception as e:
            logger.error(f"Error during SDE search for '{query}': {e}")
            return SearchToolOutputSchema(results=[])

        formatted = [self._to_search_result_item(doc, query) for doc in sde_result.results if doc.url]
        return SearchToolOutputSchema(results=formatted[:max_results])

    @staticmethod
    def _to_search_result_item(doc: SDEDocument, query: str) -> SearchResultItem:
        """Map an SDE document onto a SearchResultItem, titling it by repository name."""
        return SearchResultItem(
            title=doc.url.rstrip("/").split("/")[-1] or doc.title,
            url=HttpUrlAdapter.validate_python(doc.url),
            content=doc.content,
            query=query,
            score=doc.score,
            extra={
                "division": doc.division,
                "doc_type": doc.doc_type,
                "source": doc.source,
            },
        )

    async def _arun(self, params: RepositorySearchToolInputSchema) -> RepositorySearchToolOutputSchema:
        search_result: SearchToolOutputSchema = await super()._arun(params)
        tasks: list[asyncio.Task] = [
            self._enrich_code_search_with_metadata(repository_item) for repository_item in search_result.results
        ]
        enriched_results: list[RepositorySearchResultItem] = await asyncio.gather(*tasks)
        repository_search_result: RepositorySearchToolOutputSchema = RepositorySearchToolOutputSchema(
            results=enriched_results, extra=search_result.extra
        )
        return repository_search_result

    async def _enrich_code_search_with_metadata(self, repository_item: SearchResultItem) -> RepositorySearchResultItem:
        repo_name: str | None = _github_repo_name(str(repository_item.url))
        if repo_name is None:
            # Non-GitHub results keep empty metadata; calculate_reliability_score
            # returns None for it, which consumers treat as "no signal".
            return RepositorySearchResultItem(**repository_item.model_dump())
        repository_metadata: RepositoryMetadata = await fetch_github_metadata(repo_name, self.config.access_token)
        reliability_score: float | None = calculate_reliability_score(repository_metadata)
        return RepositorySearchResultItem(
            **{
                **repository_item.model_dump(),
                "repository_metadata": repository_metadata,
                "reliability_score": reliability_score,
            }
        )


if __name__ == "__main__":
    import sys

    config = RepositorySearchToolConfig(page_size=2)
    query = "indus pipeline code"
    if len(sys.argv) > 1:
        query = sys.argv[1]
    tool = RepositorySearchTool(config=config)
    result = asyncio.run(tool.arun(RepositorySearchToolInputSchema(queries=[query])))
    logger.info(result.model_dump())
