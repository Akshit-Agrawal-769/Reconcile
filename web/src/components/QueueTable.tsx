import type { QueueRow } from "../api";
import { STATUS_CODES, decisionLabel, money, periodLabel, reviewStateLabel, statusLabel } from "../format";
import { StatusChip } from "./StatusChip";

export function QueueTable({ rows, status, q, onStatus, onSearch, onOpen }: {
  rows: QueueRow[]; status: string; q: string;
  onStatus: (s: string) => void; onSearch: (q: string) => void; onOpen: (r: QueueRow) => void;
}) {
  const open = (r: QueueRow) => { if (r.result_id) onOpen(r); };
  return (
    <>
      <div className="toolbar">
        <label>
          Show
          <select value={status} onChange={(e) => onStatus(e.target.value)}>
            <option value="">Everything</option>
            {STATUS_CODES.map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}
          </select>
        </label>
        <label>
          Search invoice or GSTIN
          <input type="search" value={q} onChange={(e) => onSearch(e.target.value)} placeholder="INV-104" />
        </label>
        <p className="muted" style={{ margin: 0 }}>{rows.length} {rows.length === 1 ? "item" : "items"}</p>
      </div>
      <div className="ledger-wrap">
        <table className="ledger" data-testid="queue">
          <thead>
            <tr>
              <th scope="col">Match</th>
              <th scope="col">Supplier GSTIN</th>
              <th scope="col">Invoice</th>
              <th scope="col" className="num">Tax difference</th>
              <th scope="col">Decision</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.result_id ?? `c${i}`} className={r.result_id ? "clickable" : undefined}
                  tabIndex={r.result_id ? 0 : undefined}
                  onClick={() => open(r)} onKeyDown={(e) => { if (e.key === "Enter") open(r); }}>
                <td><StatusChip status={r.status} /></td>
                <td className="mono">{r.party_gstin}</td>
                <td>{r.invoice_no ?? "—"}</td>
                <td className="num">{money(r.diff_tax)}</td>
                <td>
                  {r.decision ? decisionLabel(r.decision) : <span className="muted">Not reviewed</span>}
                  {r.review_state === "PROPOSED" && <span className="badge">{reviewStateLabel("PROPOSED")}</span>}
                  {r.changed_since_review && <span className="badge changed">Changed since review</span>}
                  {r.carried_from_period && (
                    <span className="badge">Carried from {periodLabel(r.carried_from_period)}</span>
                  )}
                </td>
              </tr>
            ))}
            {!rows.length && (
              <tr><td colSpan={5} className="muted">Nothing here. Upload both files to see the matches.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
