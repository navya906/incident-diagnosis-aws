import { Link, Navigate, Route, Routes } from "react-router-dom";
import { ApiKeyGate, signOut } from "./components/ApiKeyGate";
import { IncidentPage } from "./pages/IncidentPage";
import { IncidentsPage } from "./pages/IncidentsPage";

export function App() {
  return (
    <ApiKeyGate>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-2">
          <Link to="/incidents" className="font-semibold">
            Cloud Incident Diagnosis
          </Link>
          <button className="text-xs text-slate-500 hover:text-slate-800" onClick={signOut}>
            Forget API key
          </button>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-4">
        <Routes>
          <Route path="/" element={<Navigate to="/incidents" replace />} />
          <Route path="/incidents" element={<IncidentsPage />} />
          <Route path="/incidents/:id" element={<Navigate to="overview" replace />} />
          <Route path="/incidents/:id/:tab" element={<IncidentPage />} />
          <Route path="*" element={<p className="text-sm">Page not found.</p>} />
        </Routes>
      </main>
    </ApiKeyGate>
  );
}
