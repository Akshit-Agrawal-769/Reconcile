import type { ReactNode } from "react";

/** One total in the totals strip; the value carries an accountant's double underline. */
export function MetricCard({ label, value, hint, testId, atRisk = false }: {
  label: string; value: ReactNode; hint?: string; testId?: string; atRisk?: boolean;
}) {
  return (
    <div className={`metric${atRisk ? " at-risk" : ""}`}>
      <p className="label">{label}</p>
      <p className="value" data-testid={testId}>{value}</p>
      {hint && <p className="hint">{hint}</p>}
    </div>
  );
}
