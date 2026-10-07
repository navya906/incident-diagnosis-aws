import { Background, Controls, MarkerType, Position, ReactFlow, type Edge, type Node } from "@xyflow/react";
import { useMemo } from "react";
import type { GraphData, GraphNode } from "../api/types";

export type Role = "affected" | "can_cause" | "upstream" | "downstream" | "other";

export function roleOf(n: GraphNode): Role {
  if (n.affected) return "affected";
  if (n.can_cause) return "can_cause";
  if (n.upstream) return "upstream";
  if (n.downstream) return "downstream";
  return "other";
}

const ROLE_STYLE: Record<Role, { bg: string; border: string; label: string }> = {
  affected: { bg: "#fee2e2", border: "#dc2626", label: "affected (alarmed)" },
  can_cause: { bg: "#fef3c7", border: "#d97706", label: "can cause the symptoms" },
  upstream: { bg: "#dbeafe", border: "#2563eb", label: "upstream (depends on affected)" },
  downstream: { bg: "#ede9fe", border: "#7c3aed", label: "downstream (affected depends on it)" },
  other: { bg: "#f1f5f9", border: "#94a3b8", label: "not connected to the alarm" },
};

/** Layers: depth from the entry points along "dependent -> dependency" edges. */
export function layout(data: GraphData): Record<string, { x: number; y: number }> {
  const incoming = new Map<string, number>(data.nodes.map((n) => [n.id, 0]));
  const next = new Map<string, string[]>(data.nodes.map((n) => [n.id, []]));
  for (const e of data.edges) {
    incoming.set(e.target, (incoming.get(e.target) ?? 0) + 1);
    next.get(e.source)?.push(e.target);
  }
  const depth = new Map<string, number>();
  const queue = data.nodes.filter((n) => (incoming.get(n.id) ?? 0) === 0).map((n) => n.id);
  queue.forEach((id) => depth.set(id, 0));
  while (queue.length) {
    const id = queue.shift()!;
    for (const t of next.get(id) ?? []) {
      const d = (depth.get(id) ?? 0) + 1;
      if (!depth.has(t) || depth.get(t)! < d) {
        if (d > data.nodes.length) continue; // cycle guard
        depth.set(t, d);
        queue.push(t);
      }
    }
  }
  const rows = new Map<number, number>();
  const out: Record<string, { x: number; y: number }> = {};
  for (const n of [...data.nodes].sort((a, b) => a.id.localeCompare(b.id))) {
    const d = depth.get(n.id) ?? 0;
    const row = rows.get(d) ?? 0;
    rows.set(d, row + 1);
    out[n.id] = { x: d * 260, y: row * 110 };
  }
  return out;
}

export function DependencyGraph({ data }: { data: GraphData }) {
  const { nodes, edges } = useMemo(() => {
    const pos = layout(data);
    const nodes: Node[] = data.nodes.map((n) => {
      const role = roleOf(n);
      const st = ROLE_STYLE[role];
      return {
        id: n.id,
        position: pos[n.id] ?? { x: 0, y: 0 },
        // Left-to-right layout: edges leave on the right and enter on the left.
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        data: { label: `${n.id}\n(${n.node_type})` },
        className: `graph-node role-${role}`,
        style: {
          background: st.bg,
          border: `2px solid ${st.border}`,
          borderRadius: 8,
          fontSize: 11,
          whiteSpace: "pre-line",
          width: 210,
        },
      };
    });
    const edges: Edge[] = data.edges.map((e) => ({
      id: `${e.source}->${e.target}`,
      source: e.source,
      target: e.target,
      label: e.edge_type,
      markerEnd: { type: MarkerType.ArrowClosed },
      style: { strokeWidth: 1.5 },
      labelStyle: { fontSize: 10 },
    }));
    return { nodes, edges };
  }, [data]);

  return (
    <div>
      <ul className="mb-2 flex flex-wrap gap-3 text-xs" data-testid="graph-legend">
        {(Object.keys(ROLE_STYLE) as Role[]).map((r) => (
          <li key={r} className="flex items-center gap-1">
            <span
              className="inline-block h-3 w-3 rounded"
              style={{ background: ROLE_STYLE[r].bg, border: `2px solid ${ROLE_STYLE[r].border}` }}
            />
            {ROLE_STYLE[r].label}
          </li>
        ))}
      </ul>
      {/* Accessible list of the same information (also what tests read). */}
      <ul className="sr-only" data-testid="graph-nodes">
        {data.nodes.map((n) => (
          <li key={n.id} data-node={n.id} data-role={roleOf(n)}>
            {n.id} {roleOf(n)}
          </li>
        ))}
      </ul>
      <div className="h-[520px] rounded border border-slate-200 bg-white">
        <ReactFlow nodes={nodes} edges={edges} fitView nodesDraggable proOptions={{ hideAttribution: true }}>
          <Background />
          <Controls />
        </ReactFlow>
      </div>
      <p className="mt-1 text-xs text-slate-500">Edges point from the dependent to its dependency.</p>
    </div>
  );
}
