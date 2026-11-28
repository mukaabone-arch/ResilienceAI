"use client";

import { useState } from "react";
import { createPortal } from "react-dom";
import { ErrorNotice, Loading, Panel, useAction, useDialog } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { fmtDate, fmtDateTime } from "@/lib/format";

/**
 * Autonomy policy: which Ansible job templates may run without a person approving each run. The model-risk committee
 * drafts and proposes; the CISO approves or rejects; either can revoke the policy in force at once.
 */
type Rule = {
  name: string; integration_id: string; job_template_ids: number[]; max_criticality: string; stages: string[];
  auto_launch: boolean; agents: string[]; max_runs_per_day: number; note?: string;
  integration?: string; agent_names?: string[]; runs?: number;
};
type Policy = {
  id: string; version: number; status: string; rules: Rule[]; rationale: string; review_by: string; lapsed?: boolean;
  drafted_by_name?: string; drafted_at: string; proposed_by_name?: string; proposed_at?: string;
  decided_by_name?: string; decided_at?: string; decision_note?: string;
  revoked_by_name?: string; revoked_at?: string; revoke_reason?: string;
};
type View = {
  in_force: Policy | null; policies: Policy[]; can_draft: boolean; can_decide: boolean; can_revoke: boolean;
  integrations: { id: string; name: string }[]; agents: { id: string; name: string }[]; stages: string[]; criticality: string[];
};

export default function Governance() {
  const { data, error, reload } = useApi<View>("/autonomy");
  const { run, feedback } = useAction(reload);
  const { ask, node } = useDialog();
  const [editing, setEditing] = useState<Policy | "new" | null>(null);

  if (error) return <><Head /><ErrorNotice error={error} /></>;
  if (!data) return <><Head /><Loading /></>;
  const proposed = data.policies.find((p) => p.status === "Proposed");
  const drafts = data.policies.filter((p) => p.status === "Draft");
  const past = data.policies.filter((p) => !["Draft", "Proposed"].includes(p.status) && p.id !== data.in_force?.id);

  async function decide(p: Policy, approve: boolean) {
    const v = await ask({ title: approve ? `Approve autonomy policy v${p.version}?` : `Reject autonomy policy v${p.version}?`,
      confirm: approve ? "Approve policy" : "Reject", danger: !approve,
      description: approve ? "It replaces any policy in force now. The runs its rules cover will start without a person approving each one, until it is revoked or its review date passes."
        : "The committee sees your reason and can propose a revised policy.",
      fields: [{ name: "note", label: approve ? "Basis for approval (recorded)" : "Reason (recorded)", kind: "textarea", required: true }] });
    if (v) run(() => api("POST", `/autonomy/policies/${p.id}/decision`, { approve, note: v.note }),
      approve ? `Policy v${p.version} is in force` : `Policy v${p.version} rejected`);
  }
  async function revoke(p: Policy) {
    const v = await ask({ title: `Revoke autonomy policy v${p.version}?`, confirm: "Revoke now", danger: true,
      description: "From this moment every runbook run needs a person's approval again. Jobs already running are not stopped; cancel them on their tests if needed.",
      fields: [{ name: "reason", label: "Reason (recorded)", kind: "textarea", required: true }] });
    if (v) run(() => api("POST", `/autonomy/policies/${p.id}/revoke`, { reason: v.reason }), `Policy v${p.version} revoked`);
  }

  return (
    <>
      <Head />
      {feedback}
      <div className="stack">
        <Panel title="In force" meta={data.in_force ? `Version ${data.in_force.version}` : undefined}
          action={data.in_force && data.can_revoke ? <button className="btn danger" onClick={() => revoke(data.in_force!)}>Revoke</button> : null}>
          {data.in_force ? <PolicyBody p={data.in_force} /> : (
            <p className="small"><strong>No policy is in force.</strong> Every runbook run needs the activity owner, the test manager or
              an agent&apos;s accountable person to approve it.
              {data.policies.some((p) => p.lapsed) && " The last approved policy has passed its review date."}</p>
          )}
        </Panel>
        {proposed && (
          <Panel title="Awaiting the CISO" meta={`Version ${proposed.version}, proposed by ${proposed.proposed_by_name} ${fmtDateTime(proposed.proposed_at)}`}
            action={<div className="btn-row">
              {data.can_decide && <><button className="btn primary" onClick={() => decide(proposed, true)}>Approve</button>
                <button className="btn" onClick={() => decide(proposed, false)}>Reject</button></>}
              {data.can_draft && <button className="btn quiet" onClick={() => run(() => api("POST", `/autonomy/policies/${proposed.id}/withdraw`), "Proposal withdrawn to drafts")}>Withdraw</button>}
            </div>}>
            <PolicyBody p={proposed} />
          </Panel>
        )}
        {data.can_draft && (
          <Panel title="Drafts" action={<button className="btn" onClick={() => setEditing("new")}>New draft</button>}>
            {!drafts.length ? <p className="small muted">No drafts. Start one from the policy in force, or from scratch.</p> : drafts.map((d) => (
              <div key={d.id} className="gov-draft">
                <div><strong>Version {d.version}</strong> <span className="xs muted">drafted by {d.drafted_by_name}, {fmtDateTime(d.drafted_at)} ·
                  {" "}{d.rules.length} rule{d.rules.length === 1 ? "" : "s"}, review by {fmtDate(d.review_by)}</span></div>
                <div className="btn-row">
                  <button className="btn quiet" onClick={() => setEditing(d)}>Edit</button>
                  <button className="btn primary" onClick={() => run(() => api("POST", `/autonomy/policies/${d.id}/propose`), `Version ${d.version} proposed to the CISO`)}>Propose to the CISO</button>
                </div>
              </div>
            ))}
          </Panel>
        )}
        {past.length > 0 && (
          <Panel title="History" flush>
            <table className="table">
              <thead><tr><th>Version</th><th>Outcome</th><th>By</th><th>Note</th></tr></thead>
              <tbody>{past.map((p) => (
                <tr key={p.id}>
                  <td>v{p.version}<div className="sub">{p.rules.length} rule{p.rules.length === 1 ? "" : "s"}</div></td>
                  <td className="small">{p.lapsed ? "Lapsed (review date passed)" : p.status}<div className="sub">{fmtDateTime(p.revoked_at ?? p.decided_at ?? p.proposed_at ?? p.drafted_at)}</div></td>
                  <td className="small">{p.revoked_by_name ?? p.decided_by_name ?? p.proposed_by_name ?? p.drafted_by_name}</td>
                  <td className="small">{p.revoke_reason ?? p.decision_note ?? ""}</td>
                </tr>
              ))}</tbody>
            </table>
          </Panel>
        )}
      </div>
      {editing && <PolicyEditor view={data} base={editing === "new" ? data.in_force : editing} draftId={editing === "new" ? null : editing.id}
        onDone={(msg) => { setEditing(null); if (msg) run(async () => undefined, msg); }} />}
      {node}
    </>
  );
}

function Head() {
  return (
    <div className="page-head"><div>
      <h1>Governance</h1>
      <p>Which runbooks may run without a person approving each run. The model-risk committee proposes; the CISO decides.
        Anything no rule covers needs a person, and every run records who or which rule allowed it.</p>
    </div></div>
  );
}

function PolicyBody({ p }: { p: Policy }) {
  return (
    <>
      <div className="gov-meta small">
        {p.decided_by_name && p.status === "Approved" && <span>Approved by <strong>{p.decided_by_name}</strong> (CISO) {fmtDateTime(p.decided_at)}: “{p.decision_note}”</span>}
        <span>Review by <strong className={p.lapsed ? "overdue" : ""}>{fmtDate(p.review_by)}</strong></span>
        {p.proposed_by_name && <span>Proposed by {p.proposed_by_name} (model-risk committee)</span>}
      </div>
      {p.rationale && <p className="gov-rationale">{p.rationale}</p>}
      <table className="table gov-rules">
        <thead><tr><th>Rule</th><th>Job templates</th><th>Tests</th><th>Without a person</th><th>Limit</th></tr></thead>
        <tbody>{p.rules.map((r) => (
          <tr key={r.name}>
            <td><strong>{r.name}</strong>{r.note && <div className="sub">{r.note}</div>}{r.runs ? <div className="sub">{r.runs} run{r.runs === 1 ? "" : "s"} so far</div> : null}</td>
            <td className="small">#{r.job_template_ids.join(", #")}<div className="sub">{r.integration}</div></td>
            <td className="small">Up to {r.max_criticality} criticality<div className="sub">while {r.stages.join(", ")}</div></td>
            <td className="small">{[r.auto_launch && "Starts when prerequisites are done",
              r.agent_names?.length && `Approves requests from ${r.agent_names.join(", ")}`].filter(Boolean).join("; ")}</td>
            <td className="small">{r.max_runs_per_day} a day</td>
          </tr>
        ))}</tbody>
      </table>
    </>
  );
}

const blank = (v: View): Rule => ({ name: "", integration_id: v.integrations[0]?.id ?? "", job_template_ids: [], max_criticality: "Medium",
  stages: ["Mobilising", "Readiness"], auto_launch: false, agents: [], max_runs_per_day: 10, note: "" });

function PolicyEditor({ view, base, draftId, onDone }: { view: View; base: Policy | null; draftId: string | null; onDone: (msg?: string) => void }) {
  const [rules, setRules] = useState<Rule[]>(base?.rules.map((r) => ({ ...r })) ?? [blank(view)]);
  const [rationale, setRationale] = useState(base?.rationale ?? "");
  const [review, setReview] = useState(base && draftId ? base.review_by : new Date(Date.now() + 180 * 864e5).toISOString().slice(0, 10));
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const set = (i: number, patch: Partial<Rule>) => setRules((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const toggle = <T,>(list: T[], x: T) => (list.includes(x) ? list.filter((y) => y !== x) : [...list, x]);

  async function save() {
    setBusy(true); setError(null);
    const body = { rules: rules.map(({ integration: _i, agent_names: _a, runs: _r, ...r }) => r), rationale, review_by: review };
    try {
      await api(draftId ? "PUT" : "POST", draftId ? `/autonomy/policies/${draftId}` : "/autonomy/policies", body);
      onDone("Draft saved. Propose it to the CISO when the committee agrees.");
    } catch (e) { setError(e); setBusy(false); }
  }

  return createPortal(
    <div className="dialog-backdrop" onMouseDown={(e) => e.target === e.currentTarget && !busy && onDone()}>
      <div className="dialog wide gov-editor" role="dialog" aria-modal="true" aria-labelledby="pol-title">
        <header>
          <h2 id="pol-title">{draftId ? "Edit draft" : "New draft policy"}</h2>
          <p>Keep rules narrow: name each job template, the highest test criticality and the stages. Anything not covered still needs a person.</p>
        </header>
        <div className="body">
          <ErrorNotice error={error} />
          {rules.map((r, i) => (
            <fieldset key={i} className="gov-rule">
              <legend>Rule {i + 1}{rules.length > 1 && <button type="button" className="linkish" onClick={() => setRules(rules.filter((_, j) => j !== i))}> remove</button>}</legend>
              <div className="form-row">
                <div className="field" style={{ flex: 2 }}><label htmlFor={`rn${i}`}>Name</label>
                  <input id={`rn${i}`} className="input" value={r.name} onChange={(e) => set(i, { name: e.target.value })} placeholder="e.g. Read-only standby checks" /></div>
                <div className="field"><label htmlFor={`ri${i}`}>Automation platform</label>
                  <select id={`ri${i}`} className="select" value={r.integration_id} onChange={(e) => set(i, { integration_id: e.target.value })}>
                    {view.integrations.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select></div>
              </div>
              <TemplatePicker integration={r.integration_id} value={r.job_template_ids} onChange={(ids) => set(i, { job_template_ids: ids })} />
              <div className="form-row">
                <div className="field"><label htmlFor={`rc${i}`}>Tests up to criticality</label>
                  <select id={`rc${i}`} className="select" value={r.max_criticality} onChange={(e) => set(i, { max_criticality: e.target.value })}>
                    {view.criticality.map((c) => <option key={c}>{c}</option>)}</select></div>
                <div className="field"><label htmlFor={`rm${i}`}>Most runs in 24 hours</label>
                  <input id={`rm${i}`} className="input" type="number" min={1} max={100} value={r.max_runs_per_day} onChange={(e) => set(i, { max_runs_per_day: Number(e.target.value) })} /></div>
              </div>
              <div className="gov-checks"><span className="gov-label">While the test is</span>
                {view.stages.map((s) => <label key={s}><input type="checkbox" checked={r.stages.includes(s)} onChange={() => set(i, { stages: toggle(r.stages, s) })} /> {s}</label>)}</div>
              <div className="gov-checks"><span className="gov-label">Without a person</span>
                <label><input type="checkbox" checked={r.auto_launch} onChange={() => set(i, { auto_launch: !r.auto_launch })} /> Start when the activity&apos;s prerequisites are done</label>
                {view.agents.map((a) => <label key={a.id}><input type="checkbox" checked={r.agents.includes(a.id)} onChange={() => set(i, { agents: toggle(r.agents, a.id) })} /> Approve requests from {a.name}</label>)}</div>
              <div className="field"><label htmlFor={`rno${i}`}>Note (optional)</label>
                <input id={`rno${i}`} className="input" value={r.note ?? ""} onChange={(e) => set(i, { note: e.target.value })} placeholder="e.g. Reads Data Guard status only" /></div>
            </fieldset>
          ))}
          <button type="button" className="btn quiet" onClick={() => setRules([...rules, blank(view)])}>Add a rule</button>
          <div className="field" style={{ marginTop: 14 }}><label htmlFor="rat">The committee&apos;s rationale</label>
            <textarea id="rat" className="textarea" rows={4} value={rationale} onChange={(e) => setRationale(e.target.value)}
              placeholder="Why these runs are safe without a person approving each one: what they do and don't change, their track record under supervision, how failures are caught." /></div>
          <div className="field"><label htmlFor="rev">Review by</label>
            <input id="rev" className="input" type="date" value={review} onChange={(e) => setReview(e.target.value)} style={{ maxWidth: 200 }} />
            <span className="hint">The policy lapses after this date (at most 12 months ahead).</span></div>
        </div>
        <footer>
          <button type="button" className="btn" disabled={busy} onClick={() => onDone()}>Cancel</button>
          <button type="button" className="btn primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save draft"}</button>
        </footer>
      </div>
    </div>, document.body);
}

function TemplatePicker({ integration, value, onChange }: { integration: string; value: number[]; onChange: (ids: number[]) => void }) {
  const { data, error } = useApi<any[]>(integration ? `/automation/integrations/${integration}/job-templates` : null);
  return (
    <div className="field"><span className="gov-label">Job templates it covers</span>
      {error ? <span className="hint overdue">Couldn&apos;t list templates: {(error as Error).message}</span>
        : !data ? <span className="hint">Loading…</span>
        : <div className="gov-tpls">{data.map((t) => (
          <label key={t.id} className={value.includes(t.id) ? "on" : ""}>
            <input type="checkbox" checked={value.includes(t.id)} onChange={() => onChange(value.includes(t.id) ? value.filter((x) => x !== t.id) : [...value, t.id])} />
            <span><strong>{t.name}</strong> <span className="xs muted">#{t.id}</span>{t.description && <span className="xs muted block">{t.description}</span>}</span>
          </label>))}</div>}
    </div>
  );
}
