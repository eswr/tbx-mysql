const RUPEES = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const INTEGER = new Intl.NumberFormat("en-IN");

/**
 * Format a Pydantic-serialised decimal string. The string is only handed to the
 * formatter for display; an unparseable value is shown verbatim rather than as NaN.
 */
export function formatAmount(value: string): string {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? RUPEES.format(parsed) : value;
}

export function formatCount(value: number): string {
  return INTEGER.format(value);
}

/** Render an ISO date or datetime as a stable calendar date. */
export function formatDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString("en-IN", { year: "numeric", month: "short", day: "2-digit" });
}

export function formatMilliseconds(value: number | null | undefined): string | null {
  return typeof value === "number" ? `${value.toFixed(1)} ms` : null;
}

/** Turn `min_amount_operator` into `min amount operator` for display. */
export function humanizeKey(key: string): string {
  return key.replaceAll("_", " ");
}
