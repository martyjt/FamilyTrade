"""HTTP shell established by FT-01; application operations arrive in later issues."""

from typing import Literal, TypedDict
from uuid import uuid7

from fastapi import FastAPI


class HealthResult(TypedDict):
    status: Literal["ok"]


class HealthResponse(TypedDict):
    schema_version: Literal["v1"]
    request_id: str
    result: HealthResult


app = FastAPI(title="FamilyTrade", version="0.1.0")


@app.get("/health")
def health() -> HealthResponse:
    """Return bootstrap liveness without exposing operations or credentials."""
    return {
        "schema_version": "v1",
        "request_id": str(uuid7()),
        "result": {"status": "ok"},
    }
