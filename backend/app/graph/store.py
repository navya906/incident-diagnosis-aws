"""NetworkX implementation of `GraphStore`.

Structural edges point from the dependent to its dependency (``alb -> ecs service -> rds``), as
the interface defines. `upstream`/`downstream`/`path` follow those structural edges.

Failure impact usually flows the other way (a failing RDS instance hurts the service that calls
it), so `blast_radius` and `impact_path` use a derived *impact graph*: every structural edge
``s -> t`` becomes ``t -> s``. Edge types in `BIDIRECTIONAL_IMPACT` also keep ``s -> t``: a
consumer that `polls` a queue depends on it, but when the consumer fails the queue backs up
(DECISIONS D49).
"""

from __future__ import annotations

from typing import Any

import networkx as nx

from app.interfaces.graph_store import GraphStore

#: CloudFormation resource type -> node type.
_NODE_TYPE_BY_RESOURCE_TYPE: dict[str, str] = {
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "load_balancer",
    "AWS::ElasticLoadBalancingV2::TargetGroup": "target_group",
    "AWS::ECS::Service": "ecs_service",
    "AWS::Lambda::Function": "lambda_function",
    "AWS::EC2::Instance": "ec2_instance",
    "AWS::RDS::DBInstance": "rds_instance",
    "AWS::SQS::Queue": "sqs_queue",
    "AWS::EC2::SecurityGroup": "security_group",
    "AWS::IAM::Role": "iam_role",
    "AWS::S3::Bucket": "s3_bucket",
}
NODE_TYPES: frozenset[str] = frozenset(_NODE_TYPE_BY_RESOURCE_TYPE.values()) | {"unknown"}

EDGE_TYPES: frozenset[str] = frozenset(
    {"routes_to", "connects_to", "secured_by", "assumes_role", "sends_to", "polls"}
)
BIDIRECTIONAL_IMPACT: frozenset[str] = frozenset({"polls"})

_PREFIX_NODE_TYPE = {
    "alb": "load_balancer",
    "tg": "target_group",
    "ecs": "ecs_service",
    "lambda": "lambda_function",
    "ec2": "ec2_instance",
    "rds": "rds_instance",
    "sqs": "sqs_queue",
    "sg": "security_group",
    "iam": "iam_role",
    "s3": "s3_bucket",
}


def node_type_for(resource_type: str | None, resource_id: str = "") -> str:
    """Node type from the CloudFormation type, falling back to the canonical id prefix."""
    if resource_type and resource_type in _NODE_TYPE_BY_RESOURCE_TYPE:
        return _NODE_TYPE_BY_RESOURCE_TYPE[resource_type]
    return _PREFIX_NODE_TYPE.get(resource_id.split("/", 1)[0], "unknown")


class NetworkXGraphStore(GraphStore):
    def __init__(self) -> None:
        self._g = nx.DiGraph()
        self._impact: nx.DiGraph | None = None

    # ------------------------------------------------------------------ construction
    def add_node(self, resource_id: str, node_type: str, **attrs: Any) -> None:
        if node_type not in NODE_TYPES:
            raise ValueError(f"unknown node type {node_type!r}")
        self._g.add_node(resource_id, node_type=node_type, **attrs)
        self._impact = None

    def add_edge(self, source_id: str, target_id: str, edge_type: str, **attrs: Any) -> None:
        if edge_type not in EDGE_TYPES:
            raise ValueError(f"unknown edge type {edge_type!r}")
        if source_id == target_id:
            raise ValueError("self-loops are not allowed")
        for rid in (source_id, target_id):
            if rid not in self._g:
                self.add_node(rid, node_type_for(None, rid))
        self._g.add_edge(source_id, target_id, edge_type=edge_type, **attrs)
        self._impact = None

    # ------------------------------------------------------------------ lookups
    def has_node(self, resource_id: str) -> bool:
        return resource_id in self._g

    def node_attrs(self, resource_id: str) -> dict[str, Any]:
        return dict(self._g.nodes[resource_id])

    def edge_type(self, source_id: str, target_id: str) -> str | None:
        data = self._g.get_edge_data(source_id, target_id)
        return data["edge_type"] if data else None

    def nodes(self) -> list[str]:
        return sorted(self._g.nodes)

    def edges(self) -> list[tuple[str, str, str]]:
        return sorted((s, t, d["edge_type"]) for s, t, d in self._g.edges(data=True))

    # ------------------------------------------------------------------ structural queries
    def _reach(self, g: nx.DiGraph, resource_id: str, max_depth: int | None) -> set[str]:
        if resource_id not in g:
            return set()
        lengths = nx.single_source_shortest_path_length(g, resource_id, cutoff=max_depth)
        return set(lengths) - {resource_id}

    def upstream(self, resource_id: str, max_depth: int | None = None) -> set[str]:
        return self._reach(self._g.reverse(copy=False), resource_id, max_depth)

    def downstream(self, resource_id: str, max_depth: int | None = None) -> set[str]:
        return self._reach(self._g, resource_id, max_depth)

    def path(self, source_id: str, target_id: str) -> list[str] | None:
        try:
            return nx.shortest_path(self._g, source_id, target_id)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    # ------------------------------------------------------------------ impact queries
    @property
    def impact_graph(self) -> nx.DiGraph:
        if self._impact is None:
            g = nx.DiGraph()
            g.add_nodes_from(self._g.nodes)
            for s, t, d in self._g.edges(data=True):
                g.add_edge(t, s, edge_type=d["edge_type"])
                if d["edge_type"] in BIDIRECTIONAL_IMPACT:
                    g.add_edge(s, t, edge_type=d["edge_type"])
            self._impact = g
        return self._impact

    def blast_radius(self, resource_id: str) -> set[str]:
        return self._reach(self.impact_graph, resource_id, None)

    def impact_path(self, cause_id: str, effect_id: str) -> list[str] | None:
        """Shortest path along which a failure of `cause_id` can reach `effect_id`."""
        if cause_id == effect_id:
            return [cause_id] if cause_id in self._g else None
        try:
            return nx.shortest_path(self.impact_graph, cause_id, effect_id)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def impact_sources(self, resource_ids: set[str] | list[str]) -> set[str]:
        """The given resources plus every resource whose failure can reach one of them."""
        out = {r for r in resource_ids if r in self._g}
        reverse = self.impact_graph.reverse(copy=False)
        for r in list(out):
            out |= self._reach(reverse, r, None)
        return out

    def impact_distance(self, cause_id: str, effect_id: str) -> int | None:
        p = self.impact_path(cause_id, effect_id)
        return None if p is None else len(p) - 1

    # ------------------------------------------------------------------ export
    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form (nodes with attributes, typed edges), sorted for stable output."""
        return {
            "nodes": [{"id": n, **self._g.nodes[n]} for n in self.nodes()],
            "edges": [{"source": s, "target": t, "edge_type": k} for s, t, k in self.edges()],
        }
