import { useState, type FormEvent } from "react";
import { api } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { dateLabel, periodLabel } from "../format";
import { useAction, useLoad } from "../hooks";

const CHANNELS: [string, string][] = [["EMAIL", "Email"], ["WHATSAPP", "WhatsApp"], ["CALL", "Call"], ["LETTER", "Letter"]];
const STATUSES: [string, string][] = [["OPEN", "Open"], ["SENT", "Sent"], ["RESPONDED", "Replied"], ["RESOLVED", "Resolved"]];
const name = (list: [string, string][], code: string) => list.find(([c]) => c === code)?.[1] ?? code;

export function Followups({ periodId }: { periodId: number }) {
  const period = useLoad(() => api.period(periodId), [periodId]);
  const queue = useLoad(() => api.queue(periodId), [periodId]);
  const list = useLoad(() => api.followups(periodId), [periodId]);
  const empty = { party_gstin: "", channel: "EMAIL", sent_on: "", response: "", next_chase_on: "", status: "SENT" };
  const [form, setForm] = useState(empty);
  const { busy, error, run } = useAction();

  // Suppliers worth chasing: those with an exception this month.
  const parties = [...new Set((queue.data ?? [])
    .filter((r) => r.status !== "EXACT" && r.status !== "ROUNDING")
    .map((r) => r.party_gstin))].sort();
  const set = (k: keyof typeof empty) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  async function submit(e: FormEvent) {
    e.preventDefault();
    const body: Record<string, string | null> = {};
    for (const [k, v] of Object.entries(form)) body[k] = v.trim() === "" ? null : v.trim();
    if (await run(() => api.addFollowup(periodId, body))) {
      setForm(empty);
      list.reload();
    }
  }

  const p = period.data?.period;
  return (
    <section>
      <div className="page-head">
        <p className="crumbs">
          <a href="#/board">Clients</a>
          {p && <> / <a href={`#/periods/${p.period_id}`}>{p.client_name}, {periodLabel(p.period)}</a></>}
        </p>
        <h1>Supplier follow-ups{p ? ` for ${periodLabel(p.period)}` : ""}</h1>
        <p className="muted">Who was chased, how, and what they said. Use this before deciding to defer credit.</p>
      </div>
      <ErrorBanner message={period.error ?? list.error} />
      <div className="ledger-wrap">
        <table className="ledger" data-testid="followups">
          <thead>
            <tr>
              <th scope="col">Supplier GSTIN</th><th scope="col">How</th><th scope="col">Sent on</th>
              <th scope="col">Their reply</th><th scope="col">Chase again on</th><th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {(list.data ?? []).map((f) => (
              <tr key={f.followup_id}>
                <td className="mono">{f.party_gstin}</td><td>{name(CHANNELS, f.channel)}</td>
                <td>{dateLabel(f.sent_on)}</td><td>{f.response ?? "—"}</td>
                <td>{dateLabel(f.next_chase_on)}</td><td>{name(STATUSES, f.status)}</td>
              </tr>
            ))}
            {list.data && !list.data.length && (
              <tr><td colSpan={6} className="muted">No follow-ups logged for this month yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <form className="inline-form" onSubmit={submit}>
        <h2>Log a follow-up</h2>
        <label>
          Supplier
          <select required value={form.party_gstin} onChange={set("party_gstin")}>
            <option value="">Choose a supplier</option>
            {parties.map((g) => <option key={g} value={g}>{g}</option>)}
          </select>
        </label>
        <label>
          How
          <select value={form.channel} onChange={set("channel")}>
            {CHANNELS.map(([c, t]) => <option key={c} value={c}>{t}</option>)}
          </select>
        </label>
        <label>Sent on<input type="date" value={form.sent_on} onChange={set("sent_on")} /></label>
        <label>Chase again on<input type="date" value={form.next_chase_on} onChange={set("next_chase_on")} /></label>
        <label>
          Status
          <select value={form.status} onChange={set("status")}>
            {STATUSES.map(([c, t]) => <option key={c} value={c}>{t}</option>)}
          </select>
        </label>
        <label style={{ flexBasis: "100%" }}>
          Their reply
          <textarea value={form.response} onChange={set("response")} maxLength={1000} />
        </label>
        <button type="submit" className="primary" disabled={busy}>Log follow-up</button>
        <ErrorBanner message={error} />
      </form>
    </section>
  );
}
