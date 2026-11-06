import { api, download } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { MetricCard } from "../components/MetricCard";
import { dateLabel, money, periodLabel } from "../format";
import { useAction, useLoad } from "../hooks";

/** What the business owner sees: four numbers, one decision, one download. */
export function ClientView({ periodId }: { periodId: number }) {
  const { data, error, reload } = useLoad(() => api.period(periodId), [periodId]);
  const act = useAction();
  if (!data) return error ? <ErrorBanner message={error} /> : <p className="muted">Loading…</p>;
  const p = data.period;
  const s = data.summary;
  const planReady = p.client_approved || Boolean(p.override_reason);

  return (
    <section className="client-summary">
      <div className="page-head">
        <h1>Your GST credit for {periodLabel(p.period)}</h1>
        <p className="muted">{p.client_name}, GSTIN <span className="mono">{p.gstin}</span></p>
      </div>
      <div className="totals">
        <MetricCard label="Claimable now" value={money(s?.claimable_now)}
                    hint="Input tax credit you can take in this month's GSTR-3B" />
        <MetricCard label="Deferred" value={money(s?.deferred)}
                    hint="Credit waiting for suppliers to file; claim it in a later month" />
        <MetricCard label="Lost" value={money(s?.lost)} atRisk={(s?.lost ?? 0) > 0}
                    hint="Credit that cannot be claimed" />
        <MetricCard label="Suppliers to hold payment from" value={s?.hold_payment_parties ?? 0}
                    hint="They have not filed your invoices yet" />
      </div>

      {p.state === "AWAITING_APPROVAL" && !planReady && (
        <div className="stack">
          <p>Your CA has finished this month's review. Approve it so they can file your return.</p>
          <div className="actions">
            <button className="primary" disabled={act.busy}
                    onClick={() => act.run(() => api.clientApprove(p.period_id)).then((ok) => ok && reload())}>
              Approve this month
            </button>
          </div>
        </div>
      )}
      {(p.state === "OPEN" || p.state === "IN_REVIEW") && (
        <p className="notice">Your CA is still reviewing this month. The numbers may change.</p>
      )}
      {planReady && (
        <div className="stack">
          <p>
            {p.client_approved_at ? `You approved this month on ${dateLabel(p.client_approved_at)}.`
                                  : "Your CA approved this month on your behalf."}
            {" "}The IMS action plan lists what to accept, reject or keep pending on the GST portal.
          </p>
          <div className="actions">
            <button onClick={() => act.run(() => download(`/api/periods/${p.period_id}/ims-plan.csv`,
                                                         `ims-plan-${p.period}.csv`))}>
              Download IMS action plan (CSV)
            </button>
          </div>
        </div>
      )}
      <ErrorBanner message={act.error} />
    </section>
  );
}
