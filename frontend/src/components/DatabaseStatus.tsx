import { formatCount, formatDate } from "../lib/format";
import type { DatabaseStatusState } from "../hooks/useDatabaseStatus";
import type { ReactNode } from "react";

interface DatabaseStatusProps {
  status: DatabaseStatusState;
}

const DOT_STYLES = {
  loading: "bg-slate-500",
  healthy: "bg-emerald-400",
  degraded: "bg-amber-400",
  unreachable: "bg-rose-400",
};

export function DatabaseStatus({ status }: DatabaseStatusProps) {
  const { phase, health, capabilities, capabilitiesError } = status;
  if (phase === "loading") return <StatusLine phase={phase}>Checking database…</StatusLine>;
  if (phase !== "healthy") return <StatusLine phase={phase}>{phase}</StatusLine>;

  const range = capabilities
    ? `${capabilities.date_range_start ? formatDate(capabilities.date_range_start) : "—"} – ${
        capabilities.date_range_end ? formatDate(capabilities.date_range_end) : "—"
      }`
    : null;

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
      <StatusLine phase="healthy">
        healthy · {health?.backend ?? "database"}
        {capabilities
          ? ` · ${formatCount(capabilities.transaction_count)} transactions · ${formatCount(capabilities.account_count)} accounts · ${range}`
          : ""}
      </StatusLine>
      {capabilitiesError && <span>Capabilities unavailable</span>}
      {capabilities && capabilities.warnings.length > 0 && (
        <details className="relative">
          <summary className="cursor-pointer list-none rounded-full border border-amber-700/60 px-2 py-0.5 text-amber-300">
            ⚠ {capabilities.warnings.length}
          </summary>
          <ul className="absolute left-0 z-10 mt-1 w-72 space-y-1 rounded-lg border border-hairline bg-surface p-3 shadow-xl">
            {capabilities.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export function DatabaseStatusAlert({
  status,
  onRetry,
}: DatabaseStatusProps & { onRetry: () => void }) {
  if (status.phase !== "degraded" && status.phase !== "unreachable") return null;
  return (
    <div role="alert" className="mb-3 rounded-lg border border-rose-800/70 bg-rose-950/40 p-3 text-sm text-rose-100">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="font-medium">Database unreachable — chat is disabled.</p>
          {status.error && <p className="mt-1 text-xs text-rose-300">{status.error}</p>}
        </div>
        <button
          type="button"
          onClick={onRetry}
          className="rounded-lg border border-rose-700 px-3 py-1.5 text-xs font-medium hover:bg-rose-900/60"
        >
          Retry
        </button>
      </div>
    </div>
  );
}

function StatusLine({ phase, children }: { phase: keyof typeof DOT_STYLES; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-400">
      <span aria-hidden className={`h-2 w-2 rounded-full ${DOT_STYLES[phase]}`} />
      <span>{children}</span>
    </span>
  );
}
