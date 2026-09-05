const INTEGER = new Intl.NumberFormat("en-IN");

/**
 * Format a Pydantic-serialised decimal string without ever converting it to a
 * JavaScript number. Invalid values are displayed verbatim.
 */
export function formatAmount(value: string): string {
  const match = /^([+-]?)(\d+)(?:\.(\d*))?$/.exec(value);
  if (!match) return value;

  const [, sign, rawInteger, rawFraction = ""] = match;
  const integer = rawInteger.replace(/^0+(?=\d)/, "");
  const tail = integer.slice(-3);
  const head = integer.slice(0, -3);
  const groupedHead = head.replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  const groupedInteger = head ? `${groupedHead},${tail}` : tail;
  const fraction = rawFraction.padEnd(2, "0");
  return `${sign}₹${groupedInteger}.${fraction}`;
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
