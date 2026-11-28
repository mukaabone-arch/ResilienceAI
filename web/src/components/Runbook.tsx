"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "@/lib/api";
import { relative } from "@/lib/format";
import { ErrorNotice, useDialog } from "./ui";

/**
 * An activity linked to an Ansible Automation Platform job template. ResilienceAI doesn't run commands: a person
 * approves each run, AAP runs it, and the job's progress, output and failures come back to the activity.
 */
export const RUN_ACTIVE = ["Launching", "Pending", "Running"];
const RUN_TEXT: Record<string, string> = {
  Launching: "starting", Pending: "queued in Ansible", Running: "running", Successful: "succeeded", Failed: "failed",
  Error: "couldn't start", Canceled: "cancelled", Lost: "launch not confirmed",
};
const LIVE_STAGES = ["Mobilising", "Readiness", "Ready", "Live"];

export function RunbookLine({ a }: { a: any }) {
  const rb = a.runbook;
  if (!rb) return null;
  const r = rb.last_run;
  const cls = !r ? "" : RUN_ACTIVE.includes(r.status) ? "running" : r.status === "Successful" ? "ok" : "bad";
  return (
    <div className="rb-line">
      <span className="rb-tag">Runbook</span>
      <span className="rb-name" title={`Job template ${rb.job_template_id} in ${rb.integration}`}>{rb.job_template}</span>
      {r && (
        <span className={`rb-run ${cls}`}>
          {cls === "running" && <span className="rb-pulse" aria-hidden="true" />}
          {r.external_job_id ? `Job ${r.external_job_id}` : "Run"} {RUN_TEXT[r.status] ?? r.status.toLowerCase()}
          {r.finished_at ? ` ${relative(r.finished_at)}` : ""}
          {r.external_url && <> · <a href={r.external_url} target="_blank" rel="noopener noreferrer">open in Ansible</a></>}
        </span>
      )}
      {r?.status === "Successful" && r.evidence_id && <span className="rb-soft">output attached as evidence</span>}
      {r && <span className="rb-soft">approved by {r.approved_by_name}{rb.runs > 1 ? `, run ${r.attempt}` : ""}</span>}
    </div>
  );
}

/** An agent's request to run this activity's runbook, for its accountable person or the test manager to decide. */
export function RunRequest({ a, ev, meId, run }: {
  a: any; ev: any; meId?: string; run: (fn: () => Promise<unknown>, ok?: string | ((r: any) => string)) => void;
}) {
  const { ask, node } = useDialog();
  const rq = a.run_request;
  if (!rq) return null;
  const canDecide = ev.can_manage || rq.accountable_user_id === meId;
  async function decide(approve: boolean) {
    const v = await ask(approve
      ? { title: `Approve ${rq.agent}'s request?`, confirm: "Approve and run",
          description: `This launches "${a.runbook?.job_template}" in ${a.runbook?.integration} now, on your approval. The agent gave this reason: ${rq.reason}` }
      : { title: `Decline ${rq.agent}'s request`, confirm: "Decline", danger: true,
          fields: [{ name: "note", label: "Why not, and what should happen instead?", kind: "textarea", required: true }] });
    if (v) run(() => api("POST", `/run-requests/${rq.id}/decision`, { approve, note: (v as any).note }),
      approve ? (r: any) => r?.run?.status === "Error" ? `Approved, but Ansible didn't start it: ${r.run.message ?? ""}` : `Approved: Ansible job ${r?.run?.external_job_id} launched`
              : "Request declined; the agent and its owner can see why");
  }
  return (
    <div className="rq">
      <div><strong>{rq.agent}</strong> asks to run this runbook: <span className="rq-reason">{rq.reason}</span>
        <span className="rq-when"> · {relative(rq.requested_at)}</span></div>
      {canDecide ? (
        <div className="rq-actions">
          <button className="btn primary" onClick={() => decide(true)}>Approve and run</button>
          <button className="btn" onClick={() => decide(false)}>Decline</button>
        </div>
      ) : <div className="xs muted">Waiting for its accountable person or the test manager.</div>}
      {node}
    </div>
  );
}

/** Buttons for the actions column. */
export function RunbookButtons({ a, ev, meId, canManage, run }: {
  a: any; ev: any; meId?: string; canManage?: boolean; run: (fn: () => Promise<unknown>, ok?: string | ((r: any) => string)) => void;
}) {
  const { ask, node } = useDialog();
  const [linking, setLinking] = useState(false);
  const rb = a.runbook;
  const r = rb?.last_run;
  const isOwner = a.owner_user_id === meId;
  const active = r && RUN_ACTIVE.includes(r.status);
  const stageOk = LIVE_STAGES.includes(ev.lifecycle_status);
  const closed = ["Closure", "Closed", "Cancelled"].includes(ev.lifecycle_status);
  const out: React.ReactNode[] = [];

  async function launch() {
    const vars = Object.keys(rb.extra_vars ?? {}).length ? JSON.stringify(rb.extra_vars) : "none";
    const v = await ask({ title: `Run ${a.activity_code} in Ansible?`, confirm: "Approve and run",
      description: `This launches job template "${rb.job_template}" in ${rb.integration} with variables ${vars}. `
        + "Ansible runs it under its own access controls; ResilienceAI follows the job and updates this activity. "
        + "Your approval is recorded in the audit trail." });
    if (v) run(() => api("POST", `/test-activities/${a.id}/runbook/run`),
      (res: any) => res?.status === "Error" || res?.message ? `Ansible didn't start it: ${res.message}` : `${a.activity_code}: Ansible job ${res?.external_job_id} launched`);
  }
  async function stop() {
    const v = await ask({ title: `Cancel Ansible job ${r.external_job_id ?? ""}?`, confirm: "Cancel the job", danger: true,
      description: "Ansible stops the job where it is. Steps already done aren't undone; check the job output before rerunning." });
    if (v) run(() => api("POST", `/automation-runs/${r.id}/cancel`), "Cancellation sent to Ansible");
  }

  if (rb && (isOwner || canManage) && !active && stageOk && !["Submitted", "Completed"].includes(a.status)) {
    out.push(<button key="run" className="btn primary" onClick={launch}>{r ? "Run again" : "Run in Ansible"}</button>);
  }
  if (active && (isOwner || canManage)) out.push(<button key="stop" className="btn danger" onClick={stop}>Cancel run</button>);
  if (canManage && !closed && !active) {
    out.push(<button key="link" className="btn quiet" onClick={() => setLinking(true)}>{rb ? "Change runbook" : "Link runbook"}</button>);
  }
  return <>{out}{node}{linking && <LinkDialog a={a} onDone={(msg) => { setLinking(false); if (msg) run(async () => undefined, msg); }} />}</>;
}

function LinkDialog({ a, onDone }: { a: any; onDone: (msg?: string) => void }) {
  const [integrations, setIntegrations] = useState<any[] | null>(null);
  const [integ, setInteg] = useState("");
  const [search, setSearch] = useState("");
  const [templates, setTemplates] = useState<any[] | null>(null);
  const [tpl, setTpl] = useState<number | null>(a.runbook?.job_template_id ?? null);
  const [vars, setVars] = useState(a.runbook ? JSON.stringify(a.runbook.extra_vars ?? {}, null, 2) : "{}");
  const [timeout, setTimeoutMin] = useState(String(a.runbook?.timeout_minutes ?? 120));
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api("GET", "/automation/integrations").then((r) => {
      setIntegrations(r);
      setInteg(a.runbook?.integration_id ?? r[0]?.id ?? "");
    }).catch(setError);
  }, [a.runbook?.integration_id]);
  useEffect(() => {
    if (!integ) return;
    setTemplates(null);
    const t = setTimeout(() => {
      api("GET", `/automation/integrations/${integ}/job-templates?search=${encodeURIComponent(search)}`)
        .then((r) => { setTemplates(r); setError(null); }).catch((e) => { setTemplates([]); setError(e); });
    }, 250);
    return () => clearTimeout(t);
  }, [integ, search]);

  let parsed: any = null;
  try { parsed = JSON.parse(vars || "{}"); } catch { parsed = null; }
  const varsOk = parsed && typeof parsed === "object" && !Array.isArray(parsed);
  const chosen = templates?.find((t) => t.id === tpl);
  const ok = integ && tpl && varsOk && Number(timeout) >= 1;

  async function save() {
    setBusy(true); setError(null);
    try {
      await api("PUT", `/test-activities/${a.id}/runbook`, { integration_id: integ, job_template_id: tpl, extra_vars: parsed,
        timeout_minutes: Number(timeout) });
      onDone(`${a.activity_code} linked to "${chosen?.name ?? "the job template"}"`);
    } catch (e) { setError(e); setBusy(false); }
  }

  return createPortal(
    <div className="dialog-backdrop" onMouseDown={(e) => e.target === e.currentTarget && !busy && onDone()}>
      <div className="dialog wide" role="dialog" aria-modal="true" aria-labelledby="rb-title">
        <header>
          <h2 id="rb-title">Link {a.activity_code} to an Ansible job template</h2>
          <p>Each run still needs the owner or test manager to approve it. Ansible runs the job under its own access
            controls; ResilienceAI follows it and updates this activity, attaching the job output as evidence.</p>
        </header>
        <div className="body">
          <ErrorNotice error={error} />
          {integrations && !integrations.length ? (
            <p className="small muted">No Ansible Automation Platform is connected yet. Operations connects one with
              <code> python -m scripts.automation add-aap</code>.</p>
          ) : (
            <>
              <div className="form-row">
                <div className="field"><label htmlFor="rb-int">Automation platform</label>
                  <select id="rb-int" className="select" value={integ} onChange={(e) => { setInteg(e.target.value); setTpl(null); }}>
                    {(integrations ?? []).map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
                  </select></div>
                <div className="field"><label htmlFor="rb-search">Find a job template</label>
                  <input id="rb-search" className="input" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="e.g. standby" /></div>
              </div>
              <div className="rb-templates" role="radiogroup" aria-label="Job template">
                {!templates ? <p className="small muted">Loading job templates…</p>
                  : !templates.length ? <p className="small muted">No job templates match.</p>
                  : templates.map((t) => (
                    <label key={t.id} className={`rb-tpl ${tpl === t.id ? "on" : ""}`}>
                      <input type="radio" name="tpl" checked={tpl === t.id} onChange={() => setTpl(t.id)} />
                      <span><strong>{t.name}</strong><span className="xs muted"> #{t.id} · {t.playbook}</span>
                        {t.description && <span className="xs muted block">{t.description}</span>}
                        {!t.ask_variables_on_launch && <span className="xs muted block">Doesn&apos;t take variables at launch</span>}</span>
                    </label>
                  ))}
              </div>
              <div className="form-row">
                <div className="field" style={{ flex: 2 }}><label htmlFor="rb-vars">Variables (fixed for every run)</label>
                  <textarea id="rb-vars" className="textarea mono" rows={4} value={vars} onChange={(e) => setVars(e.target.value)} />
                  <span className={`hint ${varsOk ? "" : "overdue"}`}>{varsOk ? "JSON. ResilienceAI adds resilienceai_run, _event and _activity." : "Not valid JSON"}</span></div>
                <div className="field"><label htmlFor="rb-to">Alert if running longer than (minutes)</label>
                  <input id="rb-to" className="input" type="number" min={1} max={1440} value={timeout} onChange={(e) => setTimeoutMin(e.target.value)} /></div>
              </div>
            </>
          )}
        </div>
        <footer>
          <button type="button" className="btn" disabled={busy} onClick={() => onDone()}>Cancel</button>
          <button type="button" className="btn primary" disabled={!ok || busy} onClick={save}>{busy ? "Linking…" : "Link runbook"}</button>
        </footer>
      </div>
    </div>, document.body);
}
