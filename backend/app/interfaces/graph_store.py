from abc import ABC, abstractmethod
from typing import Any


class GraphStore(ABC):
    """Dependency graph over AWS resources. Edges point from caller/dependent to dependency."""

    @abstractmethod
    def add_node(self, resource_id: str, node_type: str, **attrs: Any) -> None: ...

    @abstractmethod
    def add_edge(self, source_id: str, target_id: str, edge_type: str, **attrs: Any) -> None: ...

    @abstractmethod
    def has_node(self, resource_id: str) -> bool: ...

    @abstractmethod
    def node_attrs(self, resource_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def upstream(self, resource_id: str, max_depth: int | None = None) -> set[str]:
        """Resources that depend on `resource_id` (transitively)."""

    @abstractmethod
    def downstream(self, resource_id: str, max_depth: int | None = None) -> set[str]:
        """Resources that `resource_id` depends on (transitively)."""

    @abstractmethod
    def blast_radius(self, resource_id: str) -> set[str]:
        """Resources potentially impacted if `resource_id` fails."""

    @abstractmethod
    def path(self, source_id: str, target_id: str) -> list[str] | None:
        """Shortest dependency path, or None if unconnected."""
