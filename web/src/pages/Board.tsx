import { useState, type FormEvent } from "react";
import { api, type BoardRow, type User } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { StatePill } from "../components/StatusChip";
import { dateLabel, money, periodLabel } from "../format";
import { useAction, useLoad } from "../hooks";
import { navigate } from "../router";

export function Board({ user }: { user: User }) {
  const { data, error, reload } = useLoad(() => api.clients(), []);
  const rows = data ?? [];

  async function openLatest(r: BoardRow) {
    const periods = await api.clientPeriods(r.client_id);
    const p = periods.find((x) => x.gstin === r.gstin) ?? periods[0];
    if (p) navigate(`#/periods/${p.period_id}`);
  }

  return (
    <section>
      <div className="page-head">
        <h1>Clients</h1>
        <p className="muted">
          {user.role === "partner" ? "Every client of the firm." : "Clients assigned to you."}
          {" "}Highest tax at risk first.
        </p>
      </div>
      <ErrorBanner message={error} />
      <div className="ledger-wrap">
        <table className="ledger">
          <thead>
            <tr>
              <th scope="col">Client</th>
              <th scope="col">GSTIN</th>
              <th scope="col">Month</th>
              <th scope="col">State</th>
              <th scope="col" className="num">Tax at risk</th>
              <th scope="col">Last 2B import</th>
              <th scope="col">Assigned to</th>
            </tr>
          </thead>
          <tbody>
            {[...rows].sort((a, b) => (b.itc_at_risk ?? 0) - (a.itc_at_risk ?? 0)).map((r) => (
              <tr key={r.gstin} className={r.period ? "clickable" : undefined}
                  tabIndex={r.period ? 0 : undefined}
                  onClick={() => r.period && openLatest(r)}
                  onKeyDown={(e) => { if (e.key === "Enter" && r.period) openLatest(r); }}>
                <td><strong>{r.client_name}</strong></td>
                <td className="mono">{r.gstin}</td>
                <td>{periodLabel(r.period)}</td>
                <td><StatePill state={r.state} /></td>
                <td className="num">{money(r.itc_at_risk)}</td>
                <td>{dateLabel(r.last_import_at)}</td>
                <td>{r.assignee_names || "—"}</td>
              </tr>
            ))}
            {!rows.length && !error && (
              <tr><td colSpan={7} className="muted">No clients yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <OpenMonth rows={rows} onOpened={reload} />
    </section>
  );
}

function OpenMonth({ rows, onOpened }: { rows: BoardRow[]; onOpened: () => void }) {
  const [gstin, setGstin] = useState("");
  const [period, setPeriod] = useState("");
  const { busy, error, run } = useAction();

  async function submit(e: FormEvent) {
    e.preventDefault();
    let id = 0;
    const ok = await run(async () => { id = (await api.openPeriod(gstin, period)).period_id; });
    if (ok) { onOpened(); navigate(`#/periods/${id}`); }
  }

  return (
    <form className="inline-form" onSubmit={submit} aria-label="Open a month">
      <h2>Open a month</h2>
      <label>
        GSTIN
        <select required value={gstin} onChange={(e) => setGstin(e.target.value)}>
          <option value="">Choose a GSTIN</option>
          {rows.map((r) => <option key={r.gstin} value={r.gstin}>{r.client_name} ({r.gstin})</option>)}
        </select>
      </label>
      <label>
        Return period (MMYYYY)
        <input required pattern="(0[1-9]|1[0-2])[0-9]{4}" placeholder="102026" value={period}
               inputMode="numeric" onChange={(e) => setPeriod(e.target.value.trim())} />
      </label>
      <button type="submit" disabled={busy}>Open month</button>
      <ErrorBanner message={error} />
    </form>
  );
}
