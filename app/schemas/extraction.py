import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from app.core.exceptions import GraphFailureReason


ALLOWED_RELATIONSHIP_TYPES = frozenset(
    {
        "PREREQUISITE_OF",
        "PART_OF",
        "EXPLAINS",
        "RELATED_TO",
        "CAUSES",
        "APPLIES_TO",
    }
)


def normalize_concept_name(value: str) -> str:
    return re.sub(r"\s+", "", value.casefold())


def relationship_key(source: str, target: str, relation_type: str) -> tuple[str, str, str]:
    """RELATED_TO is symmetric; all other relation directions carry meaning."""
    if relation_type == "RELATED_TO":
        source, target = sorted((source, target))
    return source, target, relation_type


class ConceptNode(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: Annotated[StrictStr, Field(min_length=1)]
    name: Annotated[StrictStr, Field(min_length=1)]
    type: Annotated[StrictStr, Field(min_length=1)]
    description: StrictStr = ""
    source_chunk_id: StrictStr = ""
    normalized_name: StrictStr = ""
    document_name: StrictStr = ""
    page_number: int | None = Field(default=None, ge=1)
    section_heading: StrictStr = ""
    upload_id: StrictStr = ""
    source_chunk_ids: list[StrictStr] = Field(default_factory=list)
    page_numbers: list[int] = Field(default_factory=list)
    section_headings: list[StrictStr] = Field(default_factory=list)


class ConceptRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_node_id: Annotated[StrictStr, Field(min_length=1)]
    target_node_id: Annotated[StrictStr, Field(min_length=1)]
    relation_type: Annotated[StrictStr, Field(min_length=1)]


class GraphBatchDiagnostic(BaseModel):
    """Authenticated upload metadata; never contains provider output or error text."""

    section_label: str
    kind: Literal["section", "global"] = "section"
    outcome: Literal["failed", "empty", "skipped", "filtered"]
    category: GraphFailureReason | Literal[
        "no_concepts", "quota", "provider_unavailable", "configuration",
        "provider_request", "budget", "no_new_relationships", "application_budget",
    ]
    discarded_nodes: int = Field(default=0, ge=0)
    discarded_relationships: int = Field(default=0, ge=0)


class GraphExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[ConceptNode] = Field(default_factory=list)
    relationships: list[ConceptRelationship] = Field(default_factory=list)
    sections_total: int = Field(default=0, ge=0)
    sections_succeeded: int = Field(default=0, ge=0)
    sections_failed: int = Field(default=0, ge=0)
    sections_processed: int = Field(default=0, ge=0)
    sections_empty: int = Field(default=0, ge=0)
    batches_empty: int = Field(default=0, ge=0)
    empty_extraction: bool = False
    batches_total: int = Field(default=0, ge=0)
    batches_succeeded: int = Field(default=0, ge=0)
    batches_failed: int = Field(default=0, ge=0)
    batches_skipped: int = Field(default=0, ge=0)
    provider_limited: bool = False
    extraction_budget_applied: bool = False
    global_linking_attempted: bool = False
    global_linking_succeeded: bool = False
    global_linking_processed: bool = False
    failed_section_labels: list[StrictStr] = Field(default_factory=list)
    batch_diagnostics: list[GraphBatchDiagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph_integrity(self) -> "GraphExtractionResponse":
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Graph extraction contains duplicate concept IDs.")

        known_ids = set(node_ids)
        missing_endpoints = {
            endpoint
            for relationship in self.relationships
            for endpoint in (relationship.source_node_id, relationship.target_node_id)
            if endpoint not in known_ids
        }
        if missing_endpoints:
            raise ValueError("Graph relationships reference concepts that were not extracted.")

        canonical_id_by_name: dict[str, str] = {}
        canonical_id_by_original_id: dict[str, str] = {}
        unique_nodes: list[ConceptNode] = []
        for node in self.nodes:
            normalized_name = normalize_concept_name(node.name)
            if not normalized_name:
                continue
            canonical_id = canonical_id_by_name.setdefault(normalized_name, node.id)
            canonical_id_by_original_id[node.id] = canonical_id
            if canonical_id == node.id:
                node.normalized_name = normalized_name
                unique_nodes.append(node)

        unique_relationships: list[ConceptRelationship] = []
        seen: set[tuple[str, str, str]] = set()
        for relationship in self.relationships:
            relation_type = re.sub(
                r"[^A-Z0-9]+",
                "_",
                relationship.relation_type.strip().upper(),
            ).strip("_")
            if relation_type not in ALLOWED_RELATIONSHIP_TYPES:
                continue
            source_id = canonical_id_by_original_id[relationship.source_node_id]
            target_id = canonical_id_by_original_id[relationship.target_node_id]
            if source_id == target_id:
                continue
            key = relationship_key(source_id, target_id, relation_type)
            if key in seen:
                continue
            seen.add(key)
            unique_relationships.append(
                relationship.model_copy(
                    update={
                        "source_node_id": source_id,
                        "target_node_id": target_id,
                        "relation_type": relation_type,
                    }
                )
            )
        self.nodes = unique_nodes
        self.relationships = unique_relationships
        return self
