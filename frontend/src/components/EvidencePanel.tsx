import type { Evidence } from "../api/types";
import { formatAmount, formatCount, formatDate, humanizeKey } from "../lib/format";

/**
 * Renders the proof behind a number: how it was calculated, which filters the
 * compiler actually applied, and the masked sample rows the engine returned.
 */
export function EvidencePanel({ evidence }: { evidence: Evidence }) {
  const { how_calculated: how, records } = evidence;
  const filters = Object.entries(how.filters_applied);

  return (
    <details className="group mt-3 rounded-lg border border-slate-700/70 bg-slate-900/50">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-2.5 text-sm text-slate-300 hover:text-slate-100">
        <span className="font-medium">Evidence</span>
        <span className="flex items-center gap-2 text-xs text-slate-400">
          <span>
            {formatCount(how.records_matched)} record{how.records_matched === 1 ? "" : "s"} matched
          </span>
          <span aria-hidden className="transition-transform group-open:rotate-90">
            ›
          </span>
        </span>
      </summary>

      <div className="space-y-4 border-t border-slate-700/70 px-4 py-3">
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          <Term label="Operation" value={how.operation} mono />
          <Term label="Date range" value={how.date_range} mono />
          <Term label="Source table" value={evidence.source} mono />
          <Term label="Grounded" value={evidence.grounded ? "yes" : "no"} />
        </dl>

        <section>
          <h4 className="mb-1.5 text-xs font-semibold tracking-wide text-slate-400 uppercase">Filters applied</h4>
          {filters.length === 0 ? (
            <p className="text-sm text-slate-400">No filters beyond the date range.</p>
          ) : (
            <ul className="flex flex-wrap gap-1.5">
              {filters.map(([key, value]) => (
                <li
                  key={key}
                  className="rounded border border-slate-700 bg-slate-800/70 px-2 py-0.5 font-mono text-xs text-slate-200"
                >
                  {humanizeKey(key)}: {String(value)}
                </li>
              ))}
            </ul>
          )}
        </section>

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
            <div className="overflow-x-auto rounded border border-slate-700">
              <table className="w-full border-collapse text-left text-xs">
                <thead className="bg-slate-800/80 text-slate-300">
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
                <tbody className="divide-y divide-slate-800">
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
