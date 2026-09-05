import type { Interpretation } from "../api/types";
import { humanizeKey } from "../lib/format";

/**
 * Shows the validated `FinancialQuery` the backend actually executed, so the
 * user can see how their words were interpreted before trusting the number.
 */
export function InterpretationPanel({ interpretation }: { interpretation: Interpretation }) {
  const { filters, date_range: dateRange } = interpretation;
  const activeFilters = Object.entries(filters).filter(([key, value]) => {
    if (value === null) return false;
    // Amount operators are meaningless without their bound, exactly as in the compiler.
    if (key === "min_amount_operator") return filters.min_amount !== null;
    if (key === "max_amount_operator") return filters.max_amount !== null;
    return true;
  });

  return (
    <details className="mt-2 rounded-lg border border-slate-700/70 bg-slate-900/50">
      <summary className="cursor-pointer list-none px-4 py-2.5 text-sm font-medium text-slate-300 hover:text-slate-100">
        Interpretation
      </summary>
      <div className="space-y-3 border-t border-slate-700/70 px-4 py-3 text-sm">
        <div className="flex flex-wrap gap-1.5">
          <Chip label="intent" value={interpretation.intent} />
          <Chip label="metric" value={interpretation.metric} />
          <Chip label="aggregation" value={interpretation.aggregation} />
          {interpretation.limit !== null && <Chip label="limit" value={String(interpretation.limit)} />}
          {interpretation.group_by.map((dimension) => (
            <Chip key={dimension} label="group by" value={dimension} />
          ))}
          {interpretation.comparison && <Chip label="compared with" value={interpretation.comparison.against} />}
        </div>

        <div>
          <span className="text-xs tracking-wide text-slate-500 uppercase">Period</span>
          <p className="font-mono text-xs text-slate-200">
            {dateRange.label ? `${dateRange.label} — ` : ""}
            {dateRange.start} to {dateRange.end} (end exclusive)
          </p>
        </div>

        <div>
          <span className="text-xs tracking-wide text-slate-500 uppercase">Filters</span>
          {activeFilters.length === 0 ? (
            <p className="text-sm text-slate-400">None.</p>
          ) : (
            <ul className="mt-1 flex flex-wrap gap-1.5">
              {activeFilters.map(([key, value]) => (
                <li key={key}>
                  <Chip label={humanizeKey(key)} value={String(value)} />
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </details>
  );
}

function Chip({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-baseline gap-1.5 rounded border border-slate-700 bg-slate-800/70 px-2 py-0.5 text-xs">
      <span className="text-slate-500">{label}</span>
      <span className="font-mono text-slate-200">{value}</span>
    </span>
  );
}
