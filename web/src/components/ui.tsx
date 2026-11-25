"use client";

import { ReactNode, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";

const STATE_CLASS: Record<string, string> = {
  // lifecycle
  Draft: "muted", Planned: "", Mobilising: "waiting", Readiness: "waiting", Ready: "ready",
  Live: "live", Paused: "blocked", Closure: "", Closed: "done", Cancelled: "muted",
  // activities / readiness
  "Not Started": "", "In Progress": "waiting", Blocked: "blocked", Submitted: "waiting", Completed: "done",
  Pending: "", Failed: "blocked",
  // dependency state
  actionable: "ready", waiting: "", blocked: "blocked", at_risk: "waiting", done: "done",
  // assignments
  Invited: "waiting", Accepted: "ready", Declined: "blocked", Reassigned: "muted",
  // decisions
  Approved: "ready", Rejected: "blocked", Conditional: "waiting",
};

const DEP_LABEL: Record<string, string> = {
  actionable: "Can start", waiting: "Waiting on prerequisite", blocked: "Blocked upstream", at_risk: "At risk", done: "Done",
};

export function Pill({ value, dep = false }: { value: string; dep?: boolean }) {
  return <span className={`pill ${STATE_CLASS[value] ?? ""}`}>{dep ? DEP_LABEL[value] ?? value : value}</span>;
}

export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error as ApiError;
  return (
    <div className="notice error" role="alert">
      {e.message}
      {e.details?.length ? <ul>{e.details.map((d) => <li key={d}>{d}</li>)}</ul> : null}
    </div>
  );
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <p className="muted small" aria-live="polite">{what}…</p>;
}

export type DialogField = { name: string; label: string; kind?: "text" | "textarea" | "select"; required?: boolean;
  options?: { value: string; label: string }[]; hint?: string; initial?: string };

/**
 * A small modal for actions that need input (reason, comment, choice).
 * Resolves with the field values, or null when cancelled.
 */
export function useDialog() {
  const [state, setState] = useState<null | {
    title: string; description?: string; confirm: string; danger?: boolean; fields: DialogField[];
    resolve: (v: Record<string, string> | null) => void;
  }>(null);

  function ask(opts: { title: string; description?: string; confirm: string; danger?: boolean; fields?: DialogField[] }) {
    return new Promise<Record<string, string> | null>((resolve) => setState({ fields: [], ...opts, resolve }));
  }

  const node = state ? <Dialog {...state} onDone={(v) => { state.resolve(v); setState(null); }} /> : null;
  return { ask, node };
}

function Dialog({ title, description, confirm, danger, fields, onDone }: {
  title: string; description?: string; confirm: string; danger?: boolean; fields: DialogField[];
  onDone: (v: Record<string, string> | null) => void;
}) {
  const [vals, setVals] = useState<Record<string, string>>(
    Object.fromEntries(fields.map((f) => [f.name, f.initial ?? (f.kind === "select" ? f.options?.[0]?.value ?? "" : "")])));
  const first = useRef<HTMLElement | null>(null);
  useEffect(() => {
    first.current?.focus();
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onDone(null);
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onDone]);
  const missing = fields.some((f) => f.required && !vals[f.name]?.trim());
  return (
    <div className="dialog-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onDone(null)}>
      <form className="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title"
        onSubmit={(e) => { e.preventDefault(); if (!missing) onDone(vals); }}>
        <header>
          <h2 id="dlg-title">{title}</h2>
          {description && <p>{description}</p>}
        </header>
        {fields.length > 0 && (
          <div className="body">
            {fields.map((f, i) => (
              <div className="field" key={f.name}>
                <label htmlFor={`f-${f.name}`}>{f.label}{f.required ? "" : " (optional)"}</label>
                {f.kind === "textarea" ? (
                  <textarea id={`f-${f.name}`} className="textarea" value={vals[f.name]}
                    ref={(el) => { if (i === 0) first.current = el; }}
                    onChange={(e) => setVals({ ...vals, [f.name]: e.target.value })} />
                ) : f.kind === "select" ? (
                  <select id={`f-${f.name}`} className="select" value={vals[f.name]}
                    ref={(el) => { if (i === 0) first.current = el; }}
                    onChange={(e) => setVals({ ...vals, [f.name]: e.target.value })}>
                    {f.options?.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                ) : (
                  <input id={`f-${f.name}`} className="input" value={vals[f.name]}
                    ref={(el) => { if (i === 0) first.current = el; }}
                    onChange={(e) => setVals({ ...vals, [f.name]: e.target.value })} />
                )}
                {f.hint && <span className="hint">{f.hint}</span>}
              </div>
            ))}
          </div>
        )}
        <footer>
          <button type="button" className="btn" onClick={() => onDone(null)}>Cancel</button>
          <button type="submit" className={`btn ${danger ? "danger" : "primary"}`} disabled={missing}
            ref={(el) => { if (!fields.length) first.current = el; }}>{confirm}</button>
        </footer>
      </form>
    </div>
  );
}

/** Run a mutation, show its error, report success, then reload. */
export function useAction(onDone?: () => void) {
  const [error, setError] = useState<unknown>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function run(fn: () => Promise<unknown>, success?: string) {
    setBusy(true); setError(null); setOk(null);
    try {
      await fn();
      if (success) setOk(success);
      onDone?.();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  const feedback = (
    <>
      <ErrorNotice error={error} />
      {ok && <div className="notice ok" role="status">{ok}</div>}
    </>
  );
  return { run, busy, feedback, clear: () => { setError(null); setOk(null); } };
}

export function Panel({ title, meta, action, children, flush }: {
  title: ReactNode; meta?: ReactNode; action?: ReactNode; children: ReactNode; flush?: boolean;
}) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>{title}</h2>
        <div className="btn-row" style={{ alignItems: "baseline" }}>
          {meta && <span className="meta">{meta}</span>}
          {action}
        </div>
      </div>
      {flush ? children : <div className="panel-body">{children}</div>}
    </section>
  );
}
