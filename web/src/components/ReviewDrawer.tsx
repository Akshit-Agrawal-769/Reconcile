import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, type ResultDetail, type User } from "../api";
import { DECISION_CODES, IMS_CODES, dateLabel, decisionLabel, imsLabel, money,
         reviewStateLabel } from "../format";
import { useAction, useLoad } from "../hooks";
import { ErrorBanner } from "./ErrorBanner";
import { StatusChip } from "./StatusChip";

/** The suggested IMS action for a fresh decision, from what the match found. */
function suggestedIms(status: string) {
  if (status === "ONLY_IN_2B" || status === "DUPLICATE") return "REJECT";
  if (status === "EXACT" || status === "ROUNDING") return "ACCEPT";
  return "PENDING";
}

const today = () => new Date().toISOString().slice(0, 10);

export function ReviewDrawer({ resultId, user, locked, onClose, onChanged }: {
  resultId: number; user: User; locked: boolean; onClose: () => void; onChanged: () => void;
}) {
  const { data: r, error, reload } = useLoad(() => api.result(resultId), [resultId]);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    panel.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const title = r ? (r.books_invoice_no ?? r.portal_invoice_no ?? "Invoice") : "Loading…";
  const changed = () => { reload(); onChanged(); };

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title"
           tabIndex={-1} ref={panel}>
        <header>
          <h2 id="drawer-title">{title}</h2>
          <button onClick={onClose}>Close</button>
        </header>
        <ErrorBanner message={error} />
        {r && (
          <>
            <p className="muted" style={{ margin: 0 }}>
              <StatusChip status={r.status} /> {r.party_name ?? "Supplier"},{" "}
              <span className="mono">{r.party_gstin}</span>
              {r.matched_by && <>. Matched by the {r.matched_by.toLowerCase().replace("_", " ")} rule.</>}
            </p>
            <Compare r={r} />
            {r.review && <ReviewStatus r={r} user={user} locked={locked} onChanged={changed} />}
            {locked
              ? <p className="notice">This month is closed. Reopen it to change decisions.</p>
              : <DecisionForm r={r} onSaved={changed} />}
          </>
        )}
      </div>
    </>
  );
}

function Compare({ r }: { r: ResultDetail }) {
  return (
    <table className="compare">
      <thead>
        <tr><th scope="col"></th><th scope="col">Your books</th><th scope="col">GSTR-2B</th></tr>
      </thead>
      <tbody>
        <tr><th scope="row">Invoice</th><td>{r.books_invoice_no ?? "Not in books"}</td><td>{r.portal_invoice_no ?? "Not in 2B"}</td></tr>
        <tr><th scope="row">Date</th><td>{dateLabel(r.books_date)}</td><td>{dateLabel(r.portal_date)}</td></tr>
        <tr><th scope="row">Taxable value</th><td className="num">{money(r.books_taxable)}</td><td className="num">{money(r.portal_taxable)}</td></tr>
        <tr className="diff"><th scope="row">Tax</th><td className="num">{money(r.books_tax)}</td><td className="num">{money(r.portal_tax)}</td></tr>
      </tbody>
      <tfoot>
        <tr><th scope="row">Difference</th><td colSpan={2} className="num"><strong>{money(r.diff_tax)}</strong></td></tr>
      </tfoot>
    </table>
  );
}

function ReviewStatus({ r, user, locked, onChanged }: {
  r: ResultDetail; user: User; locked: boolean; onChanged: () => void;
}) {
  const rv = r.review!;
  const { busy, error, run } = useAction();
  const [doneOn, setDoneOn] = useState(today());
  return (
    <div className="stack">
      <p style={{ margin: 0 }}>
        Decided: <strong>{decisionLabel(rv.decision)}</strong>, IMS: {imsLabel(rv.ims_action)}
        {rv.claim_pct !== null && rv.decision === "PARTIAL" && <>, claim {rv.claim_pct}%</>}.{" "}
        <span className={`pill ${rv.state === "PROPOSED" ? "state-in_review" : "state-closed"}`}>
          {reviewStateLabel(rv.state)}
        </span>
        {rv.changed_since_review && <span className="badge changed">Changed since review</span>}
      </p>
      {rv.remark && <p className="muted" style={{ margin: 0 }}>“{rv.remark}”</p>}
      {!locked && rv.state === "PROPOSED" && user.role === "partner" && (
        <div className="actions">
          <button className="primary" disabled={busy}
                  onClick={() => run(() => api.approve(rv.review_id)).then((ok) => ok && onChanged())}>
            Approve
          </button>
        </div>
      )}
      {rv.state === "APPROVED" && user.role !== "client" && (
        <form className="actions" onSubmit={(e) => {
          e.preventDefault();
          run(() => api.markDone(rv.review_id, doneOn)).then((ok) => ok && onChanged());
        }}>
          <label>
            Done on
            <input type="date" value={doneOn} onChange={(e) => setDoneOn(e.target.value)} required />
          </label>
          <button type="submit" disabled={busy}>Mark done</button>
        </form>
      )}
      {rv.state === "DONE" && rv.executed_on && <p className="muted">Done on {dateLabel(rv.executed_on)}.</p>}
      <ErrorBanner message={error} />
    </div>
  );
}

function DecisionForm({ r, onSaved }: { r: ResultDetail; onSaved: () => void }) {
  const rv = r.review;
  const [decision, setDecision] = useState(rv?.decision ?? "");
  const [ims, setIms] = useState(rv?.ims_action ?? suggestedIms(r.status));
  const [claim, setClaim] = useState(rv?.claim_pct?.toString() ?? "");
  const [remark, setRemark] = useState(rv?.remark ?? "");
  const [claimError, setClaimError] = useState<string | null>(null);
  const { busy, error, run } = useAction();

  async function save(e: FormEvent) {
    e.preventDefault();
    let pct: number | null = null;
    if (decision === "PARTIAL") {
      pct = Number(claim);
      if (claim.trim() === "" || !Number.isFinite(pct) || pct < 0 || pct > 100) {
        setClaimError("Enter a percentage from 0 to 100");
        return;
      }
    }
    setClaimError(null);
    const ok = await run(() => api.propose(r.result_id, {
      decision, ims_action: ims, claim_pct: pct, remark: remark.trim() || null }));
    if (ok) onSaved();
  }

  return (
    <form className="stack" onSubmit={save}>
      <h3 style={{ margin: 0 }}>{rv ? "Change the decision" : "Decide"}</h3>
      <label>
        Decision
        <select required value={decision} onChange={(e) => setDecision(e.target.value)}>
          <option value="">Choose a decision</option>
          {DECISION_CODES.map((d) => <option key={d} value={d}>{decisionLabel(d)}</option>)}
        </select>
      </label>
      {decision === "PARTIAL" && (
        <label>
          Claim %
          <input inputMode="decimal" value={claim} onChange={(e) => setClaim(e.target.value)}
                 aria-invalid={Boolean(claimError)} aria-describedby="claim-error" />
          {claimError && <span id="claim-error" className="field-error">{claimError}</span>}
        </label>
      )}
      <label>
        IMS action
        <select value={ims} onChange={(e) => setIms(e.target.value)}>
          {IMS_CODES.map((c) => <option key={c} value={c}>{imsLabel(c)}</option>)}
        </select>
      </label>
      <label>
        Remark
        <textarea value={remark} maxLength={500} onChange={(e) => setRemark(e.target.value)} />
      </label>
      <ErrorBanner message={error} />
      <div className="actions">
        <button className="primary" type="submit" disabled={busy}>Save decision</button>
      </div>
    </form>
  );
}
