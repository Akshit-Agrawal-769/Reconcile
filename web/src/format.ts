// How numbers, dates and codes from the API read on screen.

const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" });
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function money(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  return inr.format(Number(n));
}

export function pct(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${Number(n).toLocaleString("en-IN", { maximumFractionDigits: 1 })}%`;
}

export type Tone = "success" | "accent" | "warning" | "danger" | "neutral";

const STATUS: Record<string, [string, Tone]> = {
  EXACT: ["Exact", "success"],
  ROUNDING: ["Rounding", "success"],
  PROBABLE: ["Probable", "accent"],
  MISMATCH: ["Value mismatch", "warning"],
  PERIOD_MISMATCH: ["Period mismatch", "warning"],
  ONLY_IN_2B: ["Only in 2B", "warning"],
  ONLY_IN_BOOKS: ["Only in books", "danger"],
  DUPLICATE: ["Duplicate", "danger"],
};

export const STATUS_CODES = Object.keys(STATUS);

export function statusLabel(code: string): string {
  return STATUS[code]?.[0] ?? code;
}

export function statusTone(code: string): Tone {
  return STATUS[code]?.[1] ?? "neutral";
}

const DECISIONS: Record<string, string> = {
  ACCEPT: "Accept",
  CHASE: "Chase supplier",
  DEFER: "Defer to next month",
  PARTIAL: "Claim part",
  INELIGIBLE: "Ineligible",
};
export const DECISION_CODES = Object.keys(DECISIONS);
export function decisionLabel(code: string | null | undefined): string {
  return code ? DECISIONS[code] ?? code : "—";
}

const IMS: Record<string, string> = {
  ACCEPT: "Accept", REJECT: "Reject", PENDING: "Keep pending", NONE: "No IMS action",
};
export const IMS_CODES = Object.keys(IMS);
export function imsLabel(code: string): string {
  return IMS[code] ?? code;
}

const STATES: Record<string, string> = {
  OPEN: "Open",
  IN_REVIEW: "In review",
  AWAITING_APPROVAL: "Awaiting client approval",
  CLOSED: "Closed",
};
export function stateLabel(code: string): string {
  return STATES[code] ?? code;
}

const REVIEW_STATES: Record<string, string> = {
  PROPOSED: "Awaiting partner", APPROVED: "Approved", DONE: "Done",
};
export function reviewStateLabel(code: string | null | undefined): string {
  return code ? REVIEW_STATES[code] ?? code : "";
}

export function periodLabel(mmyyyy: string | null | undefined): string {
  if (!mmyyyy || mmyyyy.length !== 6) return mmyyyy ?? "—";
  return `${MONTHS[Number(mmyyyy.slice(0, 2)) - 1]} ${mmyyyy.slice(2)}`;
}

export function dateLabel(iso: string | null | undefined): string {
  if (!iso) return "—";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return iso;
  return `${d} ${MONTHS[m - 1]} ${y}`;
}

export function roleLabel(role: string): string {
  return role === "partner" ? "Partner" : role === "staff" ? "Staff" : "Client";
}

/** Server messages are lower-case fragments ("client approval missing"); show them as a
 *  sentence without otherwise changing the wording. */
export function sentence(msg: string): string {
  return msg ? msg[0].toUpperCase() + msg.slice(1) : msg;
}
