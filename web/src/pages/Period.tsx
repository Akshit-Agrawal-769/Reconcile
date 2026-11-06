import { useEffect, useState } from "react";
import { api, type QueueRow, type User } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { MetricCard } from "../components/MetricCard";
import { MonthActions } from "../components/MonthActions";
import { QueueTable } from "../components/QueueTable";
import { ReviewDrawer } from "../components/ReviewDrawer";
import { StatePill } from "../components/StatusChip";
import { UploadButton } from "../components/UploadButton";
import { money, pct, periodLabel } from "../format";
import { useLoad } from "../hooks";
import { navigate } from "../router";

export function Period({ periodId, user }: { periodId: number; user: User }) {
  const detail = useLoad(() => api.period(periodId), [periodId]);
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const queue = useLoad(() => api.queue(periodId, status || undefined, query || undefined),
                        [periodId, status, query]);
  const [selected, setSelected] = useState<QueueRow | null>(null);

  useEffect(() => {  // search as you type, without a request per keystroke
    const t = window.setTimeout(() => setQuery(q.trim()), 250);
    return () => window.clearTimeout(t);
  }, [q]);

  const d = detail.data;
  if (!d) return detail.error ? <ErrorBanner message={detail.error} /> : <p className="muted">Loading…</p>;
  const p = d.period;
  const t = d.totals;
  const closed = p.state === "CLOSED";
  const refresh = () => { detail.reload(); queue.reload(); };

  return (
    <section>
      <div className="page-head">
        <p className="crumbs"><a href="#/board">Clients</a></p>
        <div className="head-row">
          <h1>{p.client_name}</h1>
          <StatePill state={p.state} />
        </div>
        <p className="muted">
          Return for {periodLabel(p.period)}, GSTIN <span className="mono">{p.gstin}</span>.{" "}
          <PeriodSwitch clientId={p.client_id} periodId={p.period_id} />
        </p>
        <p>
          <a href={`#/clients/${p.client_id}/reports`}>Reports and filing calendar</a>
          {" "}or{" "}
          <a href={`#/clients/${p.client_id}/followups/${p.period_id}`}>supplier follow-ups</a>
        </p>
        <MonthActions period={p} user={user} onChanged={refresh} />
      </div>

      <div className="totals" aria-label="Totals for the month">
        <MetricCard label="ITC in your books" value={money(t?.itc_books)} testId="metric-itc-books" />
        <MetricCard label="ITC in GSTR-2B" value={money(t?.itc_2b)} testId="metric-itc-2b" />
        <MetricCard label="ITC at risk" value={money(t?.itc_at_risk)} testId="metric-at-risk" atRisk
                    hint="Tax on invoices that do not match yet" />
        <MetricCard label="Match rate" value={pct(t?.match_rate_pct)} testId="metric-match-rate" />
      </div>

      {user.role !== "client" && (
        <div className="section">
          <h2>Upload this month's files</h2>
          {closed && <p className="notice">This month is closed. Reopen it to upload again.</p>}
          <div className="uploads">
            <UploadButton kind="books" periodId={p.period_id} disabled={closed} onFinished={refresh} />
            <UploadButton kind="2b" periodId={p.period_id} disabled={closed} onFinished={refresh} />
          </div>
        </div>
      )}

      <div className="section">
        <h2>Exceptions to review</h2>
        <p className="muted">Mismatches first, largest tax difference at the top. Open a row to decide.</p>
        <ErrorBanner message={queue.error} />
        <QueueTable rows={queue.data ?? []} status={status} q={q}
                    onStatus={setStatus} onSearch={setQ} onOpen={setSelected} />
      </div>
      {selected?.result_id && (
        <ReviewDrawer resultId={selected.result_id} user={user} locked={closed}
                      onClose={() => setSelected(null)} onChanged={queue.reload} />
      )}
    </section>
  );
}

function PeriodSwitch({ clientId, periodId }: { clientId: number; periodId: number }) {
  const { data } = useLoad(() => api.clientPeriods(clientId), [clientId]);
  if (!data || data.length < 2) return null;
  return (
    <label className="sr-inline">
      Month{" "}
      <select value={periodId} onChange={(e) => navigate(`#/periods/${e.target.value}`)}
              style={{ width: "auto", minHeight: 32, padding: "2px 8px" }}>
        {data.map((x) => <option key={x.period_id} value={x.period_id}>{periodLabel(x.period)}</option>)}
      </select>
    </label>
  );
}
