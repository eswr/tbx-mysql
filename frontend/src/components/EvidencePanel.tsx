import type { Evidence } from "../api/types";
import { formatAmount, formatCount, formatDate, humanizeKey } from "../lib/format";

/**
 * Renders the proof behind a number: how it was calculated, which filters the
 * compiler actually applied, and the masked sample rows the engine returned.
 */
export function EvidencePanel({ evidence, engine }: { evidence: Evidence; engine?: string }) {
  const { how_calculated: how, records, breakdown } = evidence;
  const filters = Object.entries(how.filters_applied);
  const matchedLabel = breakdown && breakdown.length > 0 ? "Groups matched" : "Records matched";

  return (
    <details className="group mt-3 rounded-lg border border-hairline bg-canvas/60">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-2.5 text-sm font-medium text-evidence hover:brightness-110">
        <span aria-hidden className="inline-block transition-transform group-open:rotate-90">
          ›
        </span>
        <span>{evidence.grounded ? "✓ Grounded" : "Not grounded"} — view how this was calculated</span>
      </summary>

      <div className="space-y-4 border-t border-hairline px-4 py-3">
        <dl className="flex flex-wrap gap-x-8 gap-y-2 text-sm">
          <InlineTerm label="Date" value={how.date_range} />
          <InlineTerm label={matchedLabel} value={formatCount(how.records_matched)} />
          <InlineTerm label="Operation" value={how.operation} mono />
        </dl>

        <p className="text-sm">
          <span className="text-slate-500">Filters: </span>
          {filters.length === 0 ? (
            <span className="text-slate-400">none beyond the date range</span>
          ) : (
            <span className="font-mono text-xs text-slate-200">
              {filters.map(([key, value]) => `${humanizeKey(key)}=${String(value)}`).join("  ")}
            </span>
          )}
        </p>

        <p className="text-sm text-slate-500">
          Source: {engine ? engine.toUpperCase() : "Database"} — {evidence.source} table, deterministic
          allowlisted query engine
        </p>

        {breakdown && breakdown.length > 0 && (
          <section>
            <h4 className="mb-1.5 text-xs font-semibold tracking-wide text-slate-400 uppercase">Breakdown</h4>
            <div className="overflow-x-auto rounded border border-hairline">
              <table className="w-full border-collapse text-left text-xs">
                <thead className="bg-evidence-surface text-slate-300">
                  <tr>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Key
                    </th>
                    {breakdown.some((entry) => entry.label) && (
                      <th scope="col" className="px-3 py-2 font-medium">
                        Name
                      </th>
                    )}
                    <th scope="col" className="px-3 py-2 text-right font-medium">
                      Value
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-hairline">
                  {breakdown.map((entry) => (
                    <tr key={entry.key} className="text-slate-300">
                      <td className="px-3 py-1.5 font-mono whitespace-nowrap">{entry.key}</td>
                      {breakdown.some((other) => other.label) && (
                        <td className="px-3 py-1.5">{entry.label ?? "—"}</td>
                      )}
                      <td className="px-3 py-1.5 text-right font-mono whitespace-nowrap">{entry.value}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {evidence.comparison_of && (
          <section>
            <h4 className="mb-1.5 text-xs font-semibold tracking-wide text-slate-400 uppercase">Comparison</h4>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
              {Object.entries(evidence.comparison_of).map(([key, value]) => (
                <Term key={key} label={humanizeKey(key)} value={value === null ? "—" : formatAmount(value)} mono />
              ))}
            </dl>
          </section>
        )}

        {records && records.length > 0 && (
          <section>
            <h4 className="mb-1.5 text-xs font-semibold tracking-wide text-slate-400 uppercase">
              Sample records{evidence.records_truncated ? " (truncated)" : ""}
            </h4>
            <div className="overflow-x-auto rounded border border-hairline">
              <table className="w-full border-collapse text-left text-xs">
                <thead className="bg-evidence-surface text-slate-300">
                  <tr>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Date
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Description
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Type
                    </th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">
                      Amount
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Reference
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-hairline">
                  {records.map((record) => (
                    <tr key={record.transaction_id} className="text-slate-300">
                      <td className="px-3 py-1.5 whitespace-nowrap">{formatDate(record.transaction_date)}</td>
                      <td className="max-w-[22rem] truncate px-3 py-1.5" title={record.description}>
                        {record.description}
                      </td>
                      <td className="px-3 py-1.5">
                        <span
                          className={
                            record.transaction_type === "credit" ? "text-emerald-400" : "text-amber-400"
                          }
                        >
                          {record.transaction_type}
                        </span>
                      </td>
                      <td className="px-3 py-1.5 text-right font-mono whitespace-nowrap">
                        {formatAmount(record.transaction_amount)}
                      </td>
                      <td className="px-3 py-1.5 font-mono text-slate-400">
                        {record.transaction_reference_id ?? record.utr_number ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {evidence.records_truncated && (
              <p className="mt-1.5 text-xs text-slate-500">
                Showing {records.length} of {formatCount(how.records_matched)} matched records. Account numbers and
                UTRs are masked before leaving the engine.
              </p>
            )}
          </section>
        )}
      </div>
    </details>
  );
}

function Term({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-xs tracking-wide text-slate-500 uppercase">{label}</dt>
      <dd className={`text-slate-200 ${mono ? "font-mono text-xs" : "text-sm"}`}>{value}</dd>
    </div>
  );
}

/** Label and value on one line, matching the compact evidence header row. */
function InlineTerm({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="text-slate-500">{label}:</dt>
      <dd className={mono ? "font-mono text-xs text-slate-100" : "text-slate-100"}>{value}</dd>
    </div>
  );
}
