// The API client. The session token lives in memory, with a sessionStorage copy so a
// page reload keeps you logged in (closing the tab ends the session).

export class ApiFailure extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const KEY = "reconcile.token";
let token: string | null = null;
try {
  token = window.sessionStorage.getItem(KEY);
} catch {
  token = null;
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export function getToken() {
  return token;
}

export function setToken(t: string | null) {
  token = t;
  try {
    if (t) window.sessionStorage.setItem(KEY, t);
    else window.sessionStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: memory only */
  }
}

async function send(method: string, path: string, body?: unknown): Promise<Response> {
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let r: Response;
  try {
    r = await fetch(path, { method, headers, body: payload });
  } catch {
    throw new ApiFailure(0, "the server could not be reached");
  }
  if (!r.ok) {
    let message = `request failed (${r.status})`;
    try {
      const j = await r.json();
      if (j && typeof j.error === "string") message = j.error;
    } catch {
      /* not JSON */
    }
    if (r.status === 401 && path !== "/api/login") {
      setToken(null);
      onUnauthorized();
    }
    throw new ApiFailure(r.status, message);
  }
  return r;
}

export async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await send(method, path, body);
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}

export async function download(path: string, filename: string) {
  const r = await send("GET", path);
  const blob = await r.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// --- types --------------------------------------------------------------------------
export type Role = "partner" | "staff" | "client";
export interface User { user_id: number; role: Role; client_id: number | null }
export interface BoardRow {
  client_id: number; client_name: string; gstin: string; period: string | null;
  state: string | null; itc_at_risk: number | null; last_import_at: string | null;
  assignee_names: string | null;
}
export interface PeriodRow { period_id: number; gstin: string; period: string; state: string; fin_year: string }
export interface Period {
  period: PeriodRow & { client_id: number; client_name: string; client_approved: boolean;
    client_approved_at: string | null; override_reason: string | null; closed_at: string | null };
  totals: { itc_books: number; itc_2b: number; itc_at_risk: number; match_rate_pct: number } | null;
  summary: { claimable_now: number; deferred: number; lost: number; hold_payment_parties: number } | null;
}
export interface QueueRow {
  result_id: number | null; import_id: number | null; status: string; matched_by: string | null;
  party_gstin: string; invoice_no: string | null; diff_tax: number | null; tax_at_risk: number | null;
  review_id: number | null; decision: string | null; review_state: string | null;
  changed_since_review: boolean | null; carried_from_period: string | null;
}
export interface Review {
  review_id: number; decision: string; ims_action: string; claim_pct: number | null;
  remark: string | null; state: string; proposed_by: number; approved_by: number | null;
  executed_on: string | null; changed_since_review: boolean | null;
}
export interface ResultDetail {
  result_id: number; status: string; matched_by: string | null; diff_tax: number | null;
  books_invoice_no: string | null; books_date: string | null; books_taxable: number | null;
  books_tax: number | null; portal_invoice_no: string | null; portal_date: string | null;
  portal_taxable: number | null; portal_tax: number | null; party_gstin: string;
  party_name: string | null; review: Review | null;
}
export interface Job {
  job_id: number; job_type: string; status: "QUEUED" | "RUNNING" | "DONE" | "FAILED";
  attempts: number; last_error: string | null; result: Record<string, unknown> | null;
  created_at: string; finished_at: string | null;
}
export interface Rule37Row {
  invoice_id: number; party_gstin: string; invoice_no: string; invoice_date: string;
  unpaid: number; days_outstanding: number; bucket: string; interest_estimate: number;
  proposed_reversal: number;
}
export interface PartyScore {
  party_gstin: string; docs: number; pct_only_in_books: number; pct_mismatch: number;
  avg_filing_delay_days: number | null; score: number;
}
export interface Filing {
  filing_id: number; gstin: string; return_type: string; period: string; due_date: string;
  filed_on: string | null; status: string; late_fee: number | null;
}
export interface Alert { gstin: string; kind: string; ref: string; due_by: string | null }
export interface Followup {
  followup_id: number; party_gstin: string; channel: string; sent_by: number | null;
  sent_on: string | null; response: string | null; next_chase_on: string | null; status: string;
}

// --- calls --------------------------------------------------------------------------
export const api = {
  login: (email: string, password: string) =>
    request<{ token: string; user: User }>("POST", "/api/login", { email, password }),
  me: () => request<User>("GET", "/api/me"),
  clients: () => request<BoardRow[]>("GET", "/api/clients"),
  clientPeriods: (clientId: number) => request<PeriodRow[]>("GET", `/api/clients/${clientId}/periods`),
  openPeriod: (gstin: string, period: string) =>
    request<{ period_id: number }>("POST", "/api/periods", { gstin, period }),
  period: (id: number) => request<Period>("GET", `/api/periods/${id}`),
  queue: (id: number, status?: string, q?: string) => {
    const p = new URLSearchParams();
    if (status) p.set("status", status);
    if (q) p.set("q", q);
    const qs = p.toString();
    return request<QueueRow[]>("GET", `/api/periods/${id}/queue${qs ? "?" + qs : ""}`);
  },
  result: (id: number) => request<ResultDetail>("GET", `/api/results/${id}`),
  job: (id: number) => request<Job>("GET", `/api/jobs/${id}`),
  periodJobs: (id: number) => request<Job[]>("GET", `/api/periods/${id}/jobs`),
  upload2b: (id: number, file: File, source: string) => {
    const fd = new FormData();
    fd.set("file", file);
    fd.set("source", source);
    return request<{ upload_id: number; job_id: number }>("POST", `/api/periods/${id}/uploads/2b`, fd);
  },
  uploadBooks: (id: number, file: File) => {
    const fd = new FormData();
    fd.set("file", file);
    return request<{ upload_id: number; job_id: number; rows: number }>(
      "POST", `/api/periods/${id}/uploads/books`, fd);
  },
  propose: (resultId: number, body: { decision: string; ims_action: string; claim_pct?: number | null; remark?: string | null }) =>
    request<{ review_id: number }>("POST", `/api/results/${resultId}/propose`, body),
  approve: (reviewId: number) => request<void>("POST", `/api/reviews/${reviewId}/approve`),
  markDone: (reviewId: number, executed_on: string) =>
    request<void>("POST", `/api/reviews/${reviewId}/done`, { executed_on }),
  submit: (id: number) => request<void>("POST", `/api/periods/${id}/submit`),
  clientApprove: (id: number) => request<void>("POST", `/api/periods/${id}/client-approve`),
  override: (id: number, reason: string) => request<void>("POST", `/api/periods/${id}/override`, { reason }),
  close: (id: number) => request<void>("POST", `/api/periods/${id}/close`),
  reopen: (id: number, reason: string) => request<void>("POST", `/api/periods/${id}/reopen`, { reason }),
  rule37: (clientId: number) => request<Rule37Row[]>("GET", `/api/clients/${clientId}/rule37`),
  partyScores: (clientId: number) => request<PartyScore[]>("GET", `/api/clients/${clientId}/party-scores`),
  filings: (clientId: number) => request<Filing[]>("GET", `/api/clients/${clientId}/filings`),
  alerts: (clientId: number) => request<Alert[]>("GET", `/api/clients/${clientId}/alerts`),
  generateFilings: (clientId: number, from: string, to: string) =>
    request<{ inserted: number }>("POST", `/api/clients/${clientId}/filings/generate`, { from, to }),
  followups: (periodId: number) => request<Followup[]>("GET", `/api/periods/${periodId}/followups`),
  addFollowup: (periodId: number, body: Record<string, string | null>) =>
    request<{ followup_id: number }>("POST", `/api/periods/${periodId}/followups`, body),
};
