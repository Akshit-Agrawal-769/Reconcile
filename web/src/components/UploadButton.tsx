import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiFailure, type Job } from "../api";
import { ErrorBanner } from "./ErrorBanner";
import { sentence } from "../format";

const STEP: Record<string, string> = {
  IMPORT_2B: "Importing GSTR-2B",
  IMPORT_BOOKS: "Importing purchase register",
  RECONCILE: "Matching invoices",
  REFRESH_SCORE: "Updating supplier scores",
};

const SOURCES = [
  ["DRAFT_2B", "GSTR-2B (monthly statement)"],
  ["IMS", "IMS download (live)"],
  ["RECOMPUTED_2B", "Recomputed 2B (after IMS actions)"],
];

type Kind = "2b" | "books";

/** Upload one file, then follow its job chain (import → match → scores) until it ends. */
export function UploadButton({ kind, periodId, disabled, onFinished }: {
  kind: Kind; periodId: number; disabled?: boolean; onFinished: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState("DRAFT_2B");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [outcome, setOutcome] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const inputRef = useRef<HTMLInputElement>(null);
  const label = kind === "2b" ? "GSTR-2B (JSON)" : "Purchase register (CSV)";

  useEffect(() => () => window.clearTimeout(timer.current), []);

  function follow(jobId: number) {
    const tick = async () => {
      let job: Job;
      try {
        job = await api.job(jobId);
      } catch (e) {
        setError(e instanceof ApiFailure ? e.message : String(e));
        return;
      }
      setJobs((prev) => {
        const i = prev.findIndex((j) => j.job_id === job.job_id);
        return i < 0 ? [...prev, job] : prev.map((j) => (j.job_id === job.job_id ? job : j));
      });
      if (job.status === "DONE") {
        const next = job.result?.next_job_id as number | undefined;
        if (next) { follow(next); return; }
        if (job.result?.reconcile === "waiting for 2B") {
          setOutcome(`Imported ${job.result.invoices} invoices. Waiting for GSTR-2B to match against.`);
        } else {
          setOutcome("Done. The queue below is up to date.");
        }
        onFinished();
        return;
      }
      if (job.status === "FAILED") {
        onFinished();
        return;
      }
      timer.current = window.setTimeout(tick, 1000);
    };
    tick();
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file) { setError("choose a file to upload"); return; }
    setBusy(true);
    setError(null);
    setJobs([]);
    setOutcome(null);
    try {
      const r = kind === "2b" ? await api.upload2b(periodId, file, source)
                              : await api.uploadBooks(periodId, file);
      follow(r.job_id);
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
    } catch (err) {
      setError(err instanceof ApiFailure ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="upload" onSubmit={submit}>
      <h3>{kind === "2b" ? "GSTR-2B from the portal" : "Purchase register"}</h3>
      <label>
        {label}
        <input ref={inputRef} type="file" accept={kind === "2b" ? ".json,application/json" : ".csv,text/csv"}
               onChange={(e) => setFile(e.target.files?.[0] ?? null)} disabled={disabled} />
      </label>
      {kind === "2b" && (
        <label>
          Source
          <select value={source} onChange={(e) => setSource(e.target.value)} disabled={disabled}>
            {SOURCES.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
          </select>
        </label>
      )}
      <div className="actions">
        <button type="submit" className="primary" disabled={busy || disabled}>
          {kind === "2b" ? "Upload GSTR-2B" : "Upload purchase register"}
        </button>
      </div>
      <ErrorBanner message={error} />
      <ul className="jobs" data-testid={`jobs-${kind}`} aria-live="polite">
        {jobs.map((j) => (
          <li key={j.job_id} className={j.status.toLowerCase()}>
            {STEP[j.job_type] ?? j.job_type}: {jobStatus(j)}
            {j.status === "FAILED" && j.last_error && (
              <span role="alert"> {sentence(j.last_error)}</span>
            )}
          </li>
        ))}
        {outcome && <li className="done"><strong>{outcome}</strong></li>}
      </ul>
    </form>
  );
}

function jobStatus(j: Job): string {
  if (j.status === "QUEUED") return j.attempts > 0 ? `retrying (attempt ${j.attempts + 1} of 3)` : "queued";
  if (j.status === "RUNNING") return "running…";
  if (j.status === "FAILED") return "failed.";
  return "finished";
}
