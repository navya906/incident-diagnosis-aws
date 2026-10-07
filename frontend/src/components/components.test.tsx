import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { GraphData, Series, TimelineItem } from "../api/types";
import { event, incident } from "../test/fixtures";
import { DependencyGraph, layout, roleOf } from "./DependencyGraph";
import { EventSearch } from "./EventSearch";
import { LifecyclePanel } from "./LifecyclePanel";
import { chartData, MetricsPanel, resourcesOf } from "./MetricsPanel";
import { categoryOf, filterItems, Timeline } from "./Timeline";

const GRAPH: GraphData = {
  nodes: [
    { id: "alb/web-lb", node_type: "load_balancer", affected: true, upstream: false, downstream: false, can_cause: false },
    { id: "ecs/service/web", node_type: "ecs_service", affected: false, upstream: false, downstream: true, can_cause: true },
    { id: "rds/db-main", node_type: "rds_instance", affected: false, upstream: false, downstream: true, can_cause: true },
    { id: "ecs/service/batch", node_type: "ecs_service", affected: false, upstream: false, downstream: false, can_cause: false },
  ],
  edges: [
    { source: "alb/web-lb", target: "ecs/service/web", edge_type: "routes_to" },
    { source: "ecs/service/web", target: "rds/db-main", edge_type: "connects_to" },
  ],
};

describe("DependencyGraph", () => {
  it("classifies nodes for highlighting", () => {
    expect(GRAPH.nodes.map(roleOf)).toEqual(["affected", "can_cause", "can_cause", "other"]);
    expect(roleOf({ ...GRAPH.nodes[3]!, upstream: true })).toBe("upstream");
    expect(roleOf({ ...GRAPH.nodes[3]!, downstream: true })).toBe("downstream");
  });

  it("lays dependencies out left to right", () => {
    const pos = layout(GRAPH);
    expect(pos["alb/web-lb"]!.x).toBeLessThan(pos["ecs/service/web"]!.x);
    expect(pos["ecs/service/web"]!.x).toBeLessThan(pos["rds/db-main"]!.x);
  });

  it("renders a legend and the role of every node", () => {
    render(<DependencyGraph data={GRAPH} />);
    expect(screen.getByTestId("graph-legend")).toHaveTextContent("affected (alarmed)");
    const list = screen.getByTestId("graph-nodes");
    expect(list.querySelector('[data-node="alb/web-lb"]')).toHaveAttribute("data-role", "affected");
    expect(list.querySelector('[data-node="ecs/service/batch"]')).toHaveAttribute("data-role", "other");
  });
});

const ITEMS: TimelineItem[] = [
  { kind: "event", at: "2025-03-01T11:00:00Z", ...event("old", { source: "cloudwatch_log", event_type: "log_line", message: "boot" }) },
  { kind: "event", at: "2025-03-01T11:58:00Z", ...event("deploy") },
  { kind: "anomaly", at: "2025-03-01T12:00:00Z", resource_id: "alb/web-lb", metric: "5XX", score: 9, baseline: 1, observed: 300, method: "zscore" },
  { kind: "transition", at: "2026-10-07T12:00:00Z", from: null, to: "DETECTED", actor: "key", note: "" },
];

describe("Timeline", () => {
  it("categorises and filters by kind and window", () => {
    expect(ITEMS.map(categoryOf)).toEqual(["log", "cloudtrail", "anomaly", "lifecycle"]);
    const all = new Set(["log", "cloudtrail", "anomaly", "lifecycle", "alarm", "config"] as const);
    expect(filterItems(ITEMS, all, null, "2025-03-01T12:03:00Z")).toHaveLength(4);
    // 15-minute window drops the 11:00 log line but keeps lifecycle rows.
    expect(filterItems(ITEMS, all, 15, "2025-03-01T12:03:00Z")).toHaveLength(3);
    expect(filterItems(ITEMS, new Set(["anomaly"] as const), null, null)).toHaveLength(1);
  });

  it("lets the user toggle categories and expand an item", async () => {
    const user = userEvent.setup();
    render(<Timeline items={ITEMS} alarmAt="2025-03-01T12:03:00Z" />);
    expect(screen.getByTestId("timeline-count")).toHaveTextContent("2 items"); // ±30 min, no lifecycle
    await user.click(screen.getByLabelText("show lifecycle"));
    expect(screen.getByTestId("timeline-count")).toHaveTextContent("3 items");
    await user.selectOptions(screen.getByLabelText("time window"), "all");
    expect(screen.getByTestId("timeline-count")).toHaveTextContent("4 items");
    await user.click(screen.getByText(/UpdateService: web now uses/));
    expect(within(screen.getByTestId("timeline-list")).getByText(/"userIdentity": "deployer"/)).toBeInTheDocument();
  });
});

describe("MetricsPanel", () => {
  const series: Series[] = [
    {
      resource_id: "alb/web-lb",
      metric: "HTTPCode_Target_5XX_Count",
      points: [
        { t: "2025-03-01T12:00:00Z", v: 2 },
        { t: "2025-03-01T12:01:00Z", v: 300 },
      ],
      anomalies: [{ t: "2025-03-01T12:01:00Z", score: 9, baseline: 2 }],
    },
    { resource_id: "rds/db-main", metric: "CPUUtilization", points: [{ t: "2025-03-01T12:00:00Z", v: 20 }], anomalies: [] },
  ];

  it("merges anomaly flags into chart points", () => {
    expect(chartData(series[0]!).map((p) => p.anomaly)).toEqual([null, 300]);
    expect(resourcesOf(series)).toEqual(["alb/web-lb", "rds/db-main"]);
  });

  it("starts on the alarmed resource and switches resources", async () => {
    const user = userEvent.setup();
    render(<MetricsPanel series={series} alarmAt="2025-03-01T12:03:00Z" affected={["alb/web-lb"]} />);
    expect(screen.getByLabelText("Resource")).toHaveValue("alb/web-lb");
    expect(screen.getByText("alb/web-lb (alarmed)")).toBeInTheDocument();
    expect(screen.getAllByTestId("metric-chart")).toHaveLength(1);
    expect(screen.getByText(/1 anomalous points/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Resource"), "rds/db-main");
    expect(screen.getByText(/CPUUtilization/)).toBeInTheDocument();
  });
});

describe("EventSearch", () => {
  it("searches, filters by severity and pages", async () => {
    const user = userEvent.setup();
    const fetchPage = vi.fn().mockImplementation(async (p: { offset: number }) => ({
      items: [event(`x${p.offset}`, { source: "cloudwatch_log", severity: "ERROR", message: "timeout talking to db" })],
      total: 150,
    }));
    render(<EventSearch kind="logs" fetchPage={fetchPage} />);
    await screen.findByText("timeout talking to db");
    await user.type(screen.getByLabelText("search logs"), "timeout");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(fetchPage).toHaveBeenLastCalledWith(expect.objectContaining({ q: "timeout", offset: 0 })));
    await user.selectOptions(screen.getByLabelText("severity"), "ERROR");
    await waitFor(() => expect(fetchPage).toHaveBeenLastCalledWith(expect.objectContaining({ severity: "ERROR" })));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(fetchPage).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 100 })));
    expect(screen.getByTestId("result-count")).toHaveTextContent("150 results");
  });

  it("shows CloudTrail callers and error codes", async () => {
    const fetchPage = vi.fn().mockResolvedValue({
      items: [event("ct", { metadata: { userIdentity: "arn:aws:iam::1:user/deployer", errorCode: "AccessDenied" } })],
      total: 1,
    });
    render(<EventSearch kind="cloudtrail" fetchPage={fetchPage} />);
    expect(await screen.findByText("AccessDenied")).toBeInTheDocument();
    expect(screen.getByText("arn:aws:iam::1:user/deployer")).toBeInTheDocument();
  });
});

describe("LifecyclePanel", () => {
  it("offers only allowed transitions and passes the note", async () => {
    const user = userEvent.setup();
    const onTransition = vi.fn().mockResolvedValue(undefined);
    render(<LifecyclePanel incident={incident()} onTransition={onTransition} />);
    const buttons = screen.getAllByRole("button").map((b) => b.textContent);
    expect(buttons).toEqual(["Move to INVESTIGATING", "Move to CLOSED"]);
    await user.type(screen.getByLabelText("transition note"), "false alarm");
    await user.click(screen.getByRole("button", { name: "Move to CLOSED" }));
    expect(onTransition).toHaveBeenCalledWith("CLOSED", "false alarm");
    expect(screen.getByTestId("ttd")).toHaveTextContent("4.0 min");
  });

  it("shows the server's refusal", async () => {
    const user = userEvent.setup();
    const onTransition = vi.fn().mockRejectedValue(new Error("closing an unresolved incident needs a note"));
    render(<LifecyclePanel incident={incident()} onTransition={onTransition} />);
    await user.click(screen.getByRole("button", { name: "Move to CLOSED" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("needs a note");
  });
});
