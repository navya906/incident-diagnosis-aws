import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { api, RATE_LIMIT_RETRIES } from "./api/client";
import { DiagnosisPanel } from "./components/DiagnosisPanel";
import { diagnosisRecord, incident } from "./test/fixtures";

type Handler = (url: string, init?: RequestInit) => unknown;

function mockFetch(handler: Handler) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const body = handler(url, init);
      if (body instanceof Response) return body;
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}

beforeEach(() => sessionStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("App", () => {
  it("asks for the API key, keeps it per session and sends it as a header", async () => {
    const user = userEvent.setup();
    const calls = mockFetch((url) => {
      if (url.startsWith("/api/incidents")) return { items: [incident()] };
      if (url.startsWith("/api/metrics/lifecycle"))
        return { incidents: 1, by_status: {}, mean_time_to_detect_min: 4, mean_time_to_diagnose_min: null, mean_time_to_resolve_min: null };
      if (url.startsWith("/api/offline/incidents"))
        return { items: [{ incident_id: "inc-aaaaaaaaaa", split: "dev", title: "ALARM x", alarm_time: "" }] };
      return {};
    });
    render(
      <MemoryRouter initialEntries={["/incidents"]}>
        <App />
      </MemoryRouter>,
    );
    await user.type(screen.getByLabelText(/API key/), "secret-key-123");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("ALARM: 5XX on alb/web-lb")).toBeInTheDocument();
    expect(sessionStorage.getItem("clouddiag.apiKey")).toBe("secret-key-123");
    expect(localStorage.length).toBe(0);
    const headers = new Headers(calls[0]!.init?.headers);
    expect(headers.get("X-API-Key")).toBe("secret-key-123");
    expect(calls.every((c) => !c.url.includes("secret-key-123"))).toBe(true);
    expect(screen.getByTestId("lifecycle-metrics")).toHaveTextContent("4.0 min");
  });

  it("shows API errors without crashing", async () => {
    sessionStorage.setItem("clouddiag.apiKey", "k");
    mockFetch(() => new Response(JSON.stringify({ detail: "missing or invalid API key" }), { status: 401 }));
    render(
      <MemoryRouter initialEntries={["/incidents"]}>
        <App />
      </MemoryRouter>,
    );
    expect((await screen.findAllByRole("alert"))[0]).toHaveTextContent("missing or invalid API key");
  });
});

describe("API client", () => {
  it("waits for Retry-After and retries when rate limited, then gives up with the error", async () => {
    vi.useFakeTimers();
    try {
      sessionStorage.setItem("clouddiag.apiKey", "k");
      let n = 0;
      const limited = () =>
        new Response(JSON.stringify({ detail: "rate limit exceeded" }), { status: 429, headers: { "Retry-After": "2" } });
      const calls = mockFetch(() => (++n <= 2 ? limited() : { items: [], total: 0 }));
      const pending = api.incidents();
      await vi.advanceTimersByTimeAsync(1999);
      expect(calls).toHaveLength(1); // still waiting for Retry-After
      await vi.advanceTimersByTimeAsync(2001 + 2000);
      await expect(pending).resolves.toEqual({ items: [], total: 0 });
      expect(calls).toHaveLength(3);

      const always = mockFetch(() => limited());
      const failing = api.incidents().catch((e: Error) => e);
      await vi.advanceTimersByTimeAsync(2000 * (RATE_LIMIT_RETRIES + 1));
      expect(((await failing) as Error).message).toBe("rate limit exceeded");
      expect(always).toHaveLength(RATE_LIMIT_RETRIES + 1);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("DiagnosisPanel", () => {
  it("starts a job, polls it and then shows the diagnosis", async () => {
    const user = userEvent.setup();
    sessionStorage.setItem("clouddiag.apiKey", "k");
    let diagnosed = false;
    let polls = 0;
    mockFetch((url, init) => {
      if (url.endsWith("/diagnose") && init?.method === "POST") return { job_id: "job-1", status: "PENDING" };
      if (url.startsWith("/api/jobs/")) {
        polls += 1;
        diagnosed = polls >= 2;
        return { job_id: "job-1", status: diagnosed ? "SUCCEEDED" : "RUNNING", diagnosis_id: 7 };
      }
      if (url.endsWith("/diagnosis"))
        return diagnosed ? diagnosisRecord() : new Response(JSON.stringify({ detail: "no diagnosis yet" }), { status: 404 });
      return {};
    });
    const onChanged = vi.fn();
    render(<DiagnosisPanel incident={incident()} onChanged={onChanged} pollMs={5} />);
    expect(await screen.findByText(/No diagnosis yet/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run diagnosis" }));
    await waitFor(() => expect(screen.getByTestId("root-cause")).toBeInTheDocument());
    expect(screen.getByTestId("job-status")).toHaveTextContent("SUCCEEDED");
    expect(onChanged).toHaveBeenCalled();
  });
});
