import { statusLabel, statusTone, stateLabel } from "../format";

export function StatusChip({ status }: { status: string }) {
  return <span className={`chip tone-${statusTone(status)}`}>{statusLabel(status)}</span>;
}

export function StatePill({ state }: { state: string | null }) {
  if (!state) return <span className="pill">No month open</span>;
  return <span className={`pill state-${state.toLowerCase()}`}>{stateLabel(state)}</span>;
}
