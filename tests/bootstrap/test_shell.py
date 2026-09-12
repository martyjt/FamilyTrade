from uuid import UUID

from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import StreamableHTTPTransport, streamable_http_client
from mcp.server import MCPServer
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from mcp.types import LATEST_PROTOCOL_VERSION, DiscoverRequest, DiscoverResult
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

from familytrade.api import app


def test_health_returns_the_bootstrap_envelope() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json()["schema_version"] == "v1"
    assert response.json()["result"] == {"status": "ok"}
    request_id = response.json()["request_id"]
    assert request_id == str(UUID(request_id))
    assert UUID(request_id).version == 7


def test_mcp_v2_imports_and_models_are_available() -> None:
    """FT-01 checks the v2 SDK surface; FT-15 owns authenticated interoperability."""
    assert Client.__module__ == "mcp.client.client"
    assert MCPServer.__module__ == "mcp.server.mcpserver.server"
    assert LATEST_PROTOCOL_VERSION == "2026-07-28"
    assert MODERN_PROTOCOL_VERSIONS == ("2026-07-28",)
    assert DiscoverRequest.model_json_schema()["type"] == "object"
    assert DiscoverResult.model_json_schema()["type"] == "object"
    assert StreamableHTTPTransport.__module__ == "mcp.client.streamable_http"
    assert streamable_http_client.__module__ == "mcp.client.streamable_http"
    assert StreamableHTTPServerTransport.__module__ == "mcp.server.streamable_http"
    assert ProtectedResourceMetadata.model_json_schema()["type"] == "object"
    assert OAuthMetadata.model_json_schema()["type"] == "object"
