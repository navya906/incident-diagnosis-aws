import { useState, type FormEvent, type ReactNode } from "react";
import { apiKey } from "../api/client";
import { Button, Card } from "./ui";

/** Asks for the API key once per browser session (sessionStorage, never localStorage). */
export function ApiKeyGate({ children }: { children: ReactNode }) {
  const [key, setKey] = useState<string | null>(apiKey.get());
  const [draft, setDraft] = useState("");

  if (key) return <>{children}</>;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const k = draft.trim();
    if (!k) return;
    apiKey.set(k);
    setKey(k);
  };

  return (
    <div className="mx-auto mt-24 max-w-md">
      <Card title="Connect to the diagnosis API">
        <form onSubmit={submit} className="space-y-3">
          <label className="block text-sm text-slate-600" htmlFor="api-key">
            API key (kept for this browser tab only)
          </label>
          <input
            id="api-key"
            type="password"
            autoComplete="off"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
          />
          <Button type="submit" disabled={!draft.trim()}>
            Continue
          </Button>
        </form>
      </Card>
    </div>
  );
}

export function signOut() {
  apiKey.clear();
  window.location.assign("/");
}
