import "@testing-library/jest-dom/vitest";

// jsdom lacks these; React Flow and Recharts' ResponsiveContainer need them.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
if (!("DOMMatrixReadOnly" in globalThis)) {
  (globalThis as Record<string, unknown>).DOMMatrixReadOnly = class {
    m22 = 1;
    constructor() {}
  };
}
