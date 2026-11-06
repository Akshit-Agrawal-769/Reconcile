import { useState, type FormEvent } from "react";
import { api } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { dateLabel, money, periodLabel } from "../format";
import { useAction, useLoad } from "../hooks";

const ALERT_KIND: Record<string, string> = {
  FILING_DUE: "Due soon",
  FILING_OVERDUE: "Overdue",
  S16_4_NEAR: "s.16(4) deadline near",
  FILED: "Filed",
};

const share = (x: number | null | undefined) =>
  x === null || x === undefined ? "—" : `${(Number(x) * 100).toLocaleString("en-IN", { maximumFractionDigits: 1 })}%`;

export function Reports({ clientId }: { clientId: number }) {
  const clients = useLoad(() => api.clients(), []);
  const name = clients.data?.find((c) => c.client_id === clientId)?.client_name ?? "client";
  return (
    <section>
      <div className="page-head">
        <p className="crumbs"><a href="#/board">Clients</a></p>
        <h1>Reports for {name}</h1>
        <p className="muted">Supplier reliability, unpaid invoices and the filing calendar.</p>
      </div>
      <PartyScores clientId={clientId} />
      <Rule37 clientId={clientId} />
      <Calendar clientId={clientId} />
    </section>
  );
}

function PartyScores({ clientId }: { clientId: number }) {
  const { data, error } = useLoad(() => api.partyScores(clientId), [clientId]);
  return (
    <div className="section">
      <h2>Supplier scores</h2>
      <p className="muted">Lowest first: these suppliers most often leave your credit at risk.</p>
      <details className="explain">
        <summary>How is the score calculated?</summary>
        <p>
          Score = 100 − 50 × share only in books − 30 × share with a value mismatch − 20 × average
          filing delay in days (capped at 30) ÷ 30. A supplier who uploads every invoice
          correctly and on time scores 100.
        </p>
      </details>
      <ErrorBanner message={error} />
      <div className="ledger-wrap">
        <table className="ledger" data-testid="party-scores">
          <thead>
            <tr>
              <th scope="col">Supplier GSTIN</th>
              <th scope="col" className="num">Invoices</th>
              <th scope="col" className="num">Only in books</th>
              <th scope="col" className="num">Value mismatch</th>
              <th scope="col" className="num">Avg filing delay</th>
              <th scope="col" className="num">Score</th>
            </tr>
          </thead>
          <tbody>
            {(data ?? []).map((s) => (
              <tr key={s.party_gstin}>
                <td className="mono">{s.party_gstin}</td>
                <td className="num">{s.docs}</td>
                <td className="num">{share(s.pct_only_in_books)}</td>
                <td className="num">{share(s.pct_mismatch)}</td>
                <td className="num">{s.avg_filing_delay_days === null ? "—" : `${s.avg_filing_delay_days} days`}</td>
                <td className="num"><strong>{Number(s.score).toFixed(1)}</strong></td>
              </tr>
            ))}
            {data && !data.length && (
              <tr><td colSpan={6} className="muted">Scores appear after the first month is matched.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Rule37({ clientId }: { clientId: number }) {
  const { data, error } = useLoad(() => api.rule37(clientId), [clientId]);
  const rows = data ?? [];
  const buckets = ["0-150", "150-180", ">180"].map((b) => ({
    b, n: rows.filter((r) => r.bucket === b).length,
    unpaid: rows.filter((r) => r.bucket === b).reduce((t, r) => t + Number(r.unpaid), 0),
  }));
  const label = (b: string) => (b === ">180" ? "Over 180 days" : `${b.replace("-", " to ")} days`);
  return (
    <div className="section">
      <h2>Unpaid invoices (Rule 37)</h2>
      <p className="muted">
        Credit on a purchase not paid within 180 days must be reversed, with interest.
      </p>
      <ErrorBanner message={error} />
      <div className="totals">
        {buckets.map(({ b, n, unpaid }) => (
          <div key={b} className={`metric${b === ">180" && n ? " at-risk" : ""}`}>
            <p className="label">{label(b)}</p>
            <p className="value">{money(unpaid)}</p>
            <p className="hint">{n} {n === 1 ? "invoice" : "invoices"}</p>
          </div>
        ))}
      </div>
      {rows.length > 0 && (
        <div className="ledger-wrap">
          <table className="ledger">
            <thead>
              <tr>
                <th scope="col">Supplier GSTIN</th><th scope="col">Invoice</th><th scope="col">Date</th>
                <th scope="col" className="num">Unpaid</th><th scope="col" className="num">Days</th>
                <th scope="col" className="num">Interest (est.)</th>
                <th scope="col" className="num">Reverse now</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.invoice_id}>
                  <td className="mono">{r.party_gstin}</td><td>{r.invoice_no}</td>
                  <td>{dateLabel(r.invoice_date)}</td><td className="num">{money(r.unpaid)}</td>
                  <td className="num">{r.days_outstanding}</td>
                  <td className="num">{money(r.interest_estimate)}</td>
                  <td className="num">{money(r.proposed_reversal)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Calendar({ clientId }: { clientId: number }) {
  const filings = useLoad(() => api.filings(clientId), [clientId]);
  const alerts = useLoad(() => api.alerts(clientId), [clientId]);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [added, setAdded] = useState<number | null>(null);
  const { busy, error, run } = useAction();

  async function generate(e: FormEvent) {
    e.preventDefault();
    let n = 0;
    if (await run(async () => { n = (await api.generateFilings(clientId, from, to)).inserted; })) {
      setAdded(n);
      filings.reload();
      alerts.reload();
    }
  }

  return (
    <div className="section">
      <h2>Filing calendar</h2>
      <form className="toolbar" onSubmit={generate}>
        <label>From<input type="date" required value={from} onChange={(e) => setFrom(e.target.value)} /></label>
        <label>To<input type="date" required value={to} onChange={(e) => setTo(e.target.value)} /></label>
        <button type="submit" disabled={busy}>Generate filings</button>
      </form>
      {added !== null && (
        <p className="notice" role="status">
          Added {added} {added === 1 ? "filing" : "filings"}. Months already on the calendar are kept as they are.
        </p>
      )}
      <ErrorBanner message={error ?? filings.error} />
      <div className="ledger-wrap">
        <table className="ledger" data-testid="filings">
          <thead>
            <tr>
              <th scope="col">Return</th><th scope="col">GSTIN</th><th scope="col">Period</th>
              <th scope="col">Due</th><th scope="col">Status</th><th scope="col">Filed on</th>
              <th scope="col" className="num">Late fee</th>
            </tr>
          </thead>
          <tbody>
            {(filings.data ?? []).map((f) => (
              <tr key={f.filing_id}>
                <td>{f.return_type}</td><td className="mono">{f.gstin}</td>
                <td>{periodLabel(f.period)}</td><td>{dateLabel(f.due_date)}</td>
                <td>{f.status[0] + f.status.slice(1).toLowerCase()}</td>
                <td>{dateLabel(f.filed_on)}</td><td className="num">{money(f.late_fee)}</td>
              </tr>
            ))}
            {filings.data && !filings.data.length && (
              <tr><td colSpan={7} className="muted">No filings yet. Pick a date range and generate them.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <h3 className="section">Alerts</h3>
      <ul className="jobs" data-testid="alerts">
        {(alerts.data ?? []).map((a, i) => (
          <li key={i} className={a.kind === "FILING_OVERDUE" ? "failed" : a.kind === "FILED" ? "done" : "running"}>
            <strong>{ALERT_KIND[a.kind] ?? a.kind}:</strong> {a.ref} for <span className="mono">{a.gstin}</span>
            {a.due_by && <>, due {dateLabel(a.due_by)}</>}
          </li>
        ))}
        {alerts.data && !alerts.data.length && <li className="muted">No alerts. Nothing is due soon.</li>}
      </ul>
    </div>
  );
}
