"""Pydantic models — schema v2 (KnowledgeGraph_Schema reference)."""

from typing import Any
from pydantic import BaseModel, Field


class UserStoryCreate(BaseModel):
    story_id: str | None = None
    title: str
    content: str = ""
    flows: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)


class FeatureCreate(BaseModel):
    feature_id: str | None = None
    name: str
    description: str = ""
    apis_used: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    order: int = 0


class APIEndpointCreate(BaseModel):
    path: str
    method: str
    summary: str = ""
    request_schema: dict[str, Any] = Field(default_factory=dict)


class TestCaseCreate(BaseModel):
    tc_id: str | None = None
    title: str
    linked_to: str = ""
    type: str = "positive"
    test_layer: str = "api"
    steps: list[str] = Field(default_factory=list)
    expected_result: str = ""


class EdgeCreate(BaseModel):
    from_node_id: str
    rel_type: str
    to_node_id: str
    params: str | None = None
    coupling_type: str | None = None


class NodeMutationResponse(BaseModel):
    success: bool
    node_id: str | None = None
    base_id: str | None = None
    version: int | None = None
    is_new: bool | None = None
    flows: list[str] | None = None
    proposal_id: str | None = None
    flow_derivation: dict | None = None
    edges_created: list = Field(default_factory=list)
    graph: dict | None = None
    message: str | None = None


class DeleteResponse(BaseModel):
    deleted: bool
    message: str | None = None
    graph: dict | None = None
