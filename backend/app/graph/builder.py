"""Build the dependency graph from resource/relationship records.

Real inventory discovery (`app.collectors.inventory.Inventory.records()`) and the offline dataset
(`ReplayCollector.inventory()`) produce the same `ResourceRecord`/`RelationshipRecord` types, so
one builder serves both.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.graph.store import NetworkXGraphStore, node_type_for
from app.offline.models import RelationshipRecord, ResourceRecord


def build_graph(
    resources: Iterable[ResourceRecord], relationships: Iterable[RelationshipRecord]
) -> NetworkXGraphStore:
    g = NetworkXGraphStore()
    for r in sorted(resources, key=lambda r: r.resource_id):
        g.add_node(
            r.resource_id,
            node_type_for(r.resource_type, r.resource_id),
            resource_type=r.resource_type,
            service=r.service,
            role=r.role,
            **{f"attr_{k}": v for k, v in sorted(r.attributes.items())},
        )
    for e in sorted(relationships, key=lambda e: (e.source_id, e.target_id, e.relation_type)):
        g.add_edge(e.source_id, e.target_id, e.relation_type)
    return g


def graph_for_incident(collector, incident_id: str) -> NetworkXGraphStore:
    """Graph for an offline incident (`ReplayCollector`) — the dataset path."""
    resources, relationships = collector.inventory(incident_id)
    return build_graph(resources, relationships)
