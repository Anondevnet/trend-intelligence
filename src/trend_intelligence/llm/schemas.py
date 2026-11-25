"""Structured outputs for LLM tasks. Every schema forbids extra fields so the JSON schema
sent to the API has ``additionalProperties: false`` and validation is strict on our side."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Claim(Strict):
    text: str
    source_refs: list[int] = Field(description="raw_items ids that support this claim")


class PostDraftOut(Strict):
    text: str = Field(description="post text, no links, no @mentions, no $tickers")
    claim_status: Literal["verified", "speculative", "rumor"]
    claims: list[Claim]


class ClaimClassificationOut(Strict):
    status: Literal["verified", "speculative", "rumor"]
    reason: str


class EntityExtractionOut(Strict):
    entities: list[str]


class TopicTaggingOut(Strict):
    topics: list[str]


TASK_SCHEMAS: dict[str, type[Strict]] = {
    "post_draft": PostDraftOut,
    "claim_classification": ClaimClassificationOut,
    "entity_extraction": EntityExtractionOut,
    "topic_tagging": TopicTaggingOut,
}


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for ``output_config.format`` (pydantic, inlined, closed objects)."""
    schema = model.model_json_schema()
    _close(schema)
    return schema


def _close(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
        for value in node.values():
            _close(value)
    elif isinstance(node, list):
        for value in node:
            _close(value)
