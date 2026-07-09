# akd-ext

Misc extension to [akd-core](https://github.com/NASA-IMPACT/accelerated-discovery/).

## Documentation

- [Creating Agents](docs/development/creating_agents.md) — guide for building new agents on `OpenAIBaseAgent` or `PydanticAIBaseAgent`, including config, schemas, tools, capabilities, tests, and reference examples.

## Installation

### Using uv (recommended)

```bash
uv pip install git+https://github.com/NASA-IMPACT/akd-ext.git@develop
```

### For development

```bash
git clone https://github.com/NASA-IMPACT/akd-ext.git
cd akd-ext
git checkout develop
uv venv --python 3.12
uv sync  # preferred
source .venv/bin/activate
```

### Running scripts

The best way to execute scripts is with `uv run`:

```bash
uv run python your_script.py
```

## MCP server

`akd_ext/mcp/server.py` publishes every `@mcp_tool`-decorated tool over MCP. Two of them are
backed by the SDE `/api/search` endpoint:

| Tool | Purpose |
| --- | --- |
| `sde_search_tool` | Search NASA's Science Discovery Engine across all indexed document types. |
| `repository_search_tool` | Search `Software and Tools` documents and enrich GitHub hits with repository metadata and a reliability score. |

Run it locally:

```bash
uv run python -m akd_ext.mcp.server                  # stdio (default)
uv run python -m akd_ext.mcp.server --transport sse  # http/sse on :8000
```

To host it, point a FastMCP Cloud project at the repository with entrypoint
`akd_ext/mcp/server.py:mcp`.

### Environment

| Variable | Required | Notes |
| --- | --- | --- |
| `SDE_BASE_URL` | No | SDE API host. Defaults to `https://dyejsbdumgpqz.cloudfront.net`. |
| `GITHUB_ACCESS_TOKEN` | Recommended | Without it GitHub throttles at 60 req/hour and `reliability_score` comes back `null`. |

### `min_score` and the SDE backend

Both SDE tools send `min_score` on every request. It is a lower bound on the `_score` each document
is returned with, and the API applies a server-side default of `0.55` when the field is omitted.
Hybrid search scores most `Software and Tools` documents around `0.01`, far below that default, so
omitting the field silently returns nothing. The tools default to `min_score=0.0` and expose it as
a config field.

Measured against the current endpoint, for `"UF universal format weather radar .uf reader python
reflectivity"` scoped to `Software and Tools`:

| `min_score` | results |
| --- | --- |
| omitted (server default `0.55`) | 0 |
| `0.0` | 385 |

Because `/api/search` spans every document type, a `Software and Tools` hit is not guaranteed to be
a GitHub repository (the index also contains project web pages). `repository_search_tool` returns
those results with empty metadata and a `null` reliability score rather than failing.

### Tool registration

Tools reach the MCP server through the `@mcp_tool` decorator, which registers a class at import
time. `akd_ext/tools/__init__.py` therefore determines what a given deployment exposes: importing a
tool there publishes it, and leaving it out keeps it off the server without touching its source.
