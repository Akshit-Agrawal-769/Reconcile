import { useState, type FormEvent } from "react";
import { api, type Period, type User } from "../api";
import { useAction } from "../hooks";
import { ErrorBanner } from "./ErrorBanner";

/** The month's workflow buttons. The database decides whether each step is allowed;
 *  when it says no, its reason is shown here and the buttons stay usable. */
export function MonthActions({ period, user, onChanged }: {
  period: Period["period"]; user: User; onChanged: () => void;
}) {
  const { busy, error, run } = useAction();
  const [asking, setAsking] = useState<null | "override" | "reopen">(null);
  const [reason, setReason] = useState("");
  const s = period.state;
  if (user.role === "client") return null;

  const act = async (fn: () => Promise<unknown>) => {
    if (await run(fn)) { setAsking(null); setReason(""); onChanged(); }
  };

  function submitReason(e: FormEvent) {
    e.preventDefault();
    if (asking === "override") act(() => api.override(period.period_id, reason));
    if (asking === "reopen") act(() => api.reopen(period.period_id, reason));
  }

  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="actions">
        {s === "IN_REVIEW" && (
          <button className="primary" disabled={busy} onClick={() => act(() => api.submit(period.period_id))}>
            Submit for client approval
          </button>
        )}
        {user.role === "partner" && s === "AWAITING_APPROVAL" && !period.client_approved
          && !period.override_reason && (
          <button disabled={busy} onClick={() => setAsking("override")}>Override client approval</button>
        )}
        {user.role === "partner" && s === "AWAITING_APPROVAL" && (
          <button className="primary" disabled={busy} onClick={() => act(() => api.close(period.period_id))}>
            Close month
          </button>
        )}
        {user.role === "partner" && s === "CLOSED" && (
          <button disabled={busy} onClick={() => setAsking("reopen")}>Reopen month</button>
        )}
      </div>
      {period.override_reason && (
        <p className="muted" style={{ margin: 0 }}>Client approval overridden: {period.override_reason}</p>
      )}
      {asking && (
        <form className="actions" onSubmit={submitReason}>
          <label style={{ flex: "1 1 260px" }}>
            {asking === "override" ? "Why approve without the client?" : "Why reopen this month?"}
            <input value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
          </label>
          <button type="submit" className="primary" disabled={busy}>
            {asking === "override" ? "Override" : "Reopen"}
          </button>
          <button type="button" className="link" onClick={() => setAsking(null)}>Cancel</button>
        </form>
      )}
      <ErrorBanner message={error} />
    </div>
  );
}
