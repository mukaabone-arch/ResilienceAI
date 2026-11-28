"use client";

import Link from "next/link";
import { Fragment, useEffect, useState } from "react";
import { ActivityActions } from "@/components/ActivityActions";
import { AssistantCards, MessageDrafts } from "@/components/Assistant";
import { GoDecision, GoDecisionDialog } from "@/components/Decisions";
import { DependencyMap } from "@/components/DependencyMap";
import { useEvidenceUpload } from "@/components/EvidenceUpload";
import { LessonsPanel } from "@/components/Lessons";
import { RUN_ACTIVE, RunbookButtons, RunbookLine } from "@/components/Runbook";
import { PrecheckDetails, PrecheckLine, openWarnings, type Finding } from "@/components/Precheck";
import { ReadinessGate } from "@/components/ReadinessGate";
import { ErrorNotice, Loading, Panel, Pill, useAction, useDialog } from "@/components/ui";
import { api, downloadFile, useApi } from "@/lib/api";
import { fmtDate, fmtDateTime, fmtTime, relative } from "@/lib/format";

type Ctx = { ev: any; me: any; isPM: boolean; reloadEvent: () => void };

const has = (me: any, ...roles: string[]) => roles.some((r) => me.roles.includes(r));
function canApproveGo({ ev, me, isPM }: Ctx) {
  if (me.is_external || me.user_id === ev.test_manager_user_id) return false;
  return isPM || (me.user_id === ev.owner_user_id && ev.criticality !== "Critical");
}

/* ------------------------------------------------------------------ Overview */
export function OverviewTab(ctx: Ctx) {
  const { ev, me } = ctx;
  const { data: rd } = useApi<any>(`/test-events/${ev.id}/readiness`);
  const { data: tl } = useApi<any[]>(`/test-events/${ev.id}/timeline`);
  const counts = ev.activity_counts as Record<string, number>;
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const order = ["Completed", "Submitted", "In Progress", "Blocked", "Not Started"];
  const recent = (tl ?? []).filter((x) => x.kind === "log").slice(-6).reverse();

  return (
    <div className="grid-2">
      <div className="stack">
        {rd ? <Link href={`/tests/${ev.id}?tab=readiness`} style={{ textDecoration: "none" }}>
          <ReadinessGate summary={rd} lifecycle={ev.lifecycle_status} compact />
        </Link> : <Loading />}
        {!me.is_external && <LessonsPanel eventId={ev.id} onChanged={ctx.reloadEvent} />}
        <Panel title="Recent activity" action={<Link className="btn quiet" href={`/tests/${ev.id}?tab=timeline`}>Full timeline</Link>} flush>
          {!recent.length && <p className="panel-empty">Nothing has happened yet.</p>}
          <ul className="timeline">
            {recent.map((x, i) => (
              <li key={i} className={x.type}>
                <span className="when">{relative(x.occurred_at)}</span><span className="dot" />
                <div><div className="what">{x.message}</div><div className="who xs">{x.actor ?? x.source}</div></div>
              </li>
            ))}
          </ul>
        </Panel>
      </div>
      <div className="stack">
        <Panel title="Activities" meta={`${total} total`} action={<Link className="btn quiet" href={`/tests/${ev.id}?tab=activities`}>Open</Link>}>
          {total ? (
            <>
              <div style={{ display: "flex", height: 10, borderRadius: 3, overflow: "hidden", background: "var(--idle-tint)" }} aria-hidden="true">
                {order.map((s) => counts[s] ? <span key={s} style={{
                  width: `${(100 * counts[s]) / total}%`,
                  background: { Completed: "var(--ready)", Submitted: "#7fb595", "In Progress": "var(--waiting)", Blocked: "var(--blocked)", "Not Started": "var(--idle-tint)" }[s],
                }} /> : null)}
              </div>
              <ul className="rows" style={{ marginTop: 8 }}>
                {order.filter((s) => counts[s]).map((s) => (
                  <li key={s} style={{ padding: "6px 0" }}><span className="grow small">{s}</span><strong className="small">{counts[s]}</strong></li>
                ))}
              </ul>
            </>
          ) : <p className="muted small">No activities yet.</p>}
          {ev.open_blockers > 0 && <p className="small overdue" style={{ marginTop: 8 }}>{ev.open_blockers} open blocker{ev.open_blockers > 1 ? "s" : ""}</p>}
        </Panel>
        <Panel title="Milestones" flush>
          {!ev.upcoming_milestones.length && <p className="panel-empty">No pending milestones.</p>}
          <ul className="rows">
            {ev.upcoming_milestones.map((m: any) => (
              <li key={m.id}><span className="grow small">{m.title}</span><span className="small muted">{fmtDateTime(m.planned_at, ev.timezone)}</span></li>
            ))}
          </ul>
        </Panel>
        {ev.series && (
          <Panel title="Tests in this series" meta={ev.series.title} flush>
            <ul className="rows">
              {ev.series.occurrences.map((o: any) => (
                <li key={o.id} style={{ padding: "8px 18px", alignItems: "center" }}>
                  <span className="grow small">
                    {o.id === ev.id ? <strong>{fmtDateTime(o.planned_start_at, ev.timezone)} (this test)</strong>
                      : <Link href={`/tests/${o.id}`}>{fmtDateTime(o.planned_start_at, ev.timezone)}</Link>}
                  </span>
                  <Pill value={o.lifecycle_status} />
                </li>
              ))}
            </ul>
          </Panel>
        )}
        {(ev.objectives || ev.description) && <Panel title="Objectives"><p className="small">{ev.objectives || ev.description}</p></Panel>}
        <RecoveryTargets {...ctx} />
      </div>
    </div>
  );
}

function RecoveryTargets({ ev, reloadEvent }: Ctx) {
  const { run, feedback } = useAction(reloadEvent);
  const { ask, node } = useDialog();
  const editable = ev.can_manage && !["Closed", "Cancelled"].includes(ev.lifecycle_status);
  const mins = (m: number | null) => (m == null ? "Not set" : m >= 120 && m % 60 === 0 ? `${m / 60} hours` : `${m} min`);
  async function edit() {
    const v = await ask({ title: "Recovery targets", confirm: "Save",
      description: "Uploaded evidence is checked against these: a log showing a 40-minute recovery point against a 15-minute RPO is flagged for the reviewer.",
      fields: [
        { name: "rto", label: "RTO target, minutes", initial: ev.rto_target_minutes?.toString() ?? "", hint: "Leave empty for none" },
        { name: "rpo", label: "RPO target, minutes", initial: ev.rpo_target_minutes?.toString() ?? "", hint: "Leave empty for none" },
      ] });
    if (!v) return;
    const num = (x: string) => (x.trim() === "" ? null : Number(x));
    run(() => api("PATCH", `/test-events/${ev.id}`, { expected_version: ev.version, rto_target_minutes: num(v.rto), rpo_target_minutes: num(v.rpo) }),
      "Recovery targets saved");
  }
  return (
    <Panel title="Recovery targets" action={editable ? <button className="btn quiet" onClick={edit}>Edit</button> : undefined}>
      {feedback}
      <dl className="rt-facts">
        <div><dt>RTO</dt><dd>{mins(ev.rto_target_minutes)}</dd></div>
        <div><dt>RPO</dt><dd>{mins(ev.rpo_target_minutes)}</dd></div>
      </dl>
      {node}
    </Panel>
  );
}

/* ---------------------------------------------------------------- Activities */
export function ActivitiesTab(ctx: Ctx) {
  const { ev, me, reloadEvent } = ctx;
  const { data: map, reload: reloadMap } = useApi<any>(`/test-events/${ev.id}/dependencies`);
  const { data: acts, error, reload } = useApi<any[]>(`/test-events/${ev.id}/activities`);
  const { data: people } = useApi<any[]>(ev.can_manage ? `/test-events/${ev.id}/stakeholders` : null);
  const refresh = () => { reload(); reloadMap(); reloadEvent(); };
  const { run, feedback } = useAction(refresh);
  // While an Ansible job is running, keep the list current (the worker and AAP's notifications update it).
  const running = (acts ?? []).some((a) => a.runbook?.last_run && RUN_ACTIVE.includes(a.runbook.last_run.status));
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => { reload(); reloadMap(); }, 5000);
    return () => clearInterval(t);
  }, [running, reload, reloadMap]);
  const { ask, node } = useDialog();

  async function add() {
    const v = await ask({ title: "Add an activity", confirm: "Add activity", fields: [
      { name: "activity_code", label: "Code", required: true, hint: "Short and unique within this test, e.g. DB-02" },
      { name: "title", label: "What needs doing", required: true },
      { name: "stakeholder_id", label: "Owner", kind: "select", options: [{ value: "", label: "Unassigned" },
        ...(people ?? []).map((p) => ({ value: p.id, label: `${p.display_name} (${p.event_role})` }))] },
      { name: "due_at", label: "Due (your local time)", hint: "Format 2027-02-18 14:00" },
      { name: "evidence_required", label: "Evidence required", kind: "select", options: [{ value: "no", label: "No" }, { value: "yes", label: "Yes" }] },
    ] });
    if (!v) return;
    const due = v.due_at ? new Date(v.due_at.replace(" ", "T")) : null;
    run(() => api("POST", `/test-events/${ev.id}/activities`, {
      activity_code: v.activity_code, title: v.title, stakeholder_id: v.stakeholder_id || undefined,
      due_at: due && !isNaN(due.getTime()) ? due.toISOString() : undefined, evidence_required: v.evidence_required === "yes",
    }), "Activity added");
  }

  return (
    <div className="stack">
      {feedback}
      <Panel title="Dependencies" meta={map ? Object.entries(map.summary).map(([k, n]) => `${n} ${k.replace("_", " ")}`).join(", ") : ""} flush>
        {map ? <DependencyMap nodes={map.nodes} edges={map.edges} /> : <Loading />}
      </Panel>
      <Panel title="All activities" action={ev.can_manage && !["Closed", "Cancelled"].includes(ev.lifecycle_status)
        ? <button className="btn" onClick={add}>Add activity</button> : null} flush>
        <ErrorNotice error={error} />
        {!acts ? <div className="panel-body"><Loading /></div> : (
          <table className="table">
            <thead><tr><th>Activity</th><th>Owner</th><th>Due</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {acts.map((a) => (
                <tr key={a.id}>
                  <td><span className="code">{a.activity_code}</span><div className="title" style={{ fontSize: "var(--t-sm)" }}>{a.title}</div>
                    {a.blocked_reason && <div className="sub" style={{ color: "var(--blocked)" }}>{a.blocked_reason}</div>}
                    <RunbookLine a={a} /></td>
                  <td>{a.owner_name ?? <span className="muted">Unassigned</span>}</td>
                  <td className={a.is_overdue ? "overdue" : ""}>{fmtDateTime(a.due_at, ev.timezone)}</td>
                  <td><Pill value={a.status} />
                    {a.status === "Not Started" && <div style={{ marginTop: 4 }}><Pill value={a.dependency_state} dep /></div>}
                    {a.review_status === "Pending" && a.status === "Submitted" && <div className="sub">Awaiting review</div>}</td>
                  <td><div className="act-cell">
                    {!me.is_external && <div className="task-actions"><RunbookButtons a={a} ev={ev} meId={me.user_id} canManage={ev.can_manage} run={run} /></div>}
                    {!a.runbook?.last_run || !RUN_ACTIVE.includes(a.runbook.last_run.status)
                      ? <ActivityActions a={a} meId={me.user_id} canManage={ev.can_manage} run={run} /> : null}
                  </div></td>
                </tr>
              ))}
              {!acts.length && <tr><td colSpan={5} className="muted">No activities yet.</td></tr>}
            </tbody>
          </table>
        )}
      </Panel>
      {node}
    </div>
  );
}

/* ----------------------------------------------------------------- Readiness */
export function ReadinessTab(ctx: Ctx) {
  const { ev, me, isPM, reloadEvent } = ctx;
  const { data: rd, error, reload } = useApi<any>(`/test-events/${ev.id}/readiness`);
  const { data: users } = useApi<any[]>(ev.can_manage ? "/users" : null);
  const [deciding, setDeciding] = useState<null | boolean>(null);   // which decision dialog is open (go / no-go)
  const { data: teams } = useApi<any[]>(ev.can_manage ? "/teams" : null);
  const refresh = () => { reload(); reloadEvent(); };
  const { run, busy, feedback } = useAction(refresh);
  const { ask, node } = useDialog();
  const upload = useEvidenceUpload((msg) => run(async () => undefined, msg));   // hooks before any early return
  if (error) return <ErrorNotice error={error} />;
  if (!rd) return <Loading what="Checking readiness" />;

  const preReady = ["Draft", "Planned", "Mobilising", "Readiness"].includes(ev.lifecycle_status);
  const editable = !["Closure", "Closed", "Cancelled"].includes(ev.lifecycle_status);
  const canException = isPM || has(me, "RiskCompliance");

  async function setStatus(item: any) {
    const v = await ask({ title: `Update "${item.title}"`, confirm: "Save status", fields: [
      { name: "status", label: "Status", kind: "select", initial: item.status,
        options: ["Pending", "In Progress", "Ready", "Blocked", "Failed"].map((s) => ({ value: s, label: s })) },
      ...(ev.can_manage ? [{ name: "owner_user_id", label: "Owner", kind: "select" as const, initial: item.owner_user_id ?? "",
        hint: "The owner can update this condition themselves",
        options: [{ value: "", label: "Test manager only" }, ...(users ?? []).filter((u) => !u.is_external).map((u) => ({ value: u.id, label: u.display_name }))] }] : []),
      { name: "comment", label: "Note for the timeline", kind: "textarea" },
    ] });
    if (v) run(() => api("PATCH", `/test-readiness-items/${item.id}`, {
      status: v.status, comment: v.comment || undefined,
      ...(ev.can_manage && v.owner_user_id !== (item.owner_user_id ?? "") && v.owner_user_id ? { owner_user_id: v.owner_user_id } : {}),
    }), "Readiness updated");
  }
  async function resolve(b: any) {
    const v = await ask({ title: `Resolve ${b.issue_code}`, confirm: "Mark resolved", description: b.title,
      fields: [{ name: "comment", label: "How was it resolved?", kind: "textarea", required: true }] });
    if (v) run(() => api("PATCH", `/test-issues/${b.id}`, { status: "Resolved", comment: v.comment }), `${b.issue_code} resolved`);
  }
  async function exception(item: any) {
    const v = await ask({ title: "Grant a readiness exception", confirm: "Grant exception", danger: true,
      description: `"${item.title}" will no longer block the go decision. Your justification is kept in the audit record.`,
      fields: [{ name: "comments", label: "Justification", kind: "textarea", required: true }] });
    if (v) run(() => api("POST", `/test-events/${ev.id}/approvals`, { approval_type: "ReadinessException", decision: "Approved", subject_id: item.id, comments: v.comments }), "Exception granted");
  }
  function evidence(item: any) {
    upload.open({ kind: "readiness", id: item.id, label: `"${item.title}"` });
  }
  async function addItem() {
    const v = await ask({ title: "Add a readiness condition", confirm: "Add condition", fields: [
      { name: "title", label: "Condition", required: true, hint: "Something that must be true before the test starts" },
      { name: "is_mandatory", label: "Blocks the go decision", kind: "select", options: [{ value: "yes", label: "Yes, mandatory" }, { value: "no", label: "No, optional" }] },
      { name: "evidence_required", label: "Evidence required", kind: "select", options: [{ value: "no", label: "No" }, { value: "yes", label: "Yes" }] },
      { name: "owner_user_id", label: "Owner", kind: "select", options: [{ value: "", label: "Test manager" }, ...(users ?? []).filter((u) => !u.is_external).map((u) => ({ value: u.id, label: u.display_name }))] },
    ] });
    if (v) run(() => api("POST", `/test-events/${ev.id}/readiness-items`, {
      title: v.title, is_mandatory: v.is_mandatory === "yes", evidence_required: v.evidence_required === "yes", owner_user_id: v.owner_user_id || undefined,
    }), "Condition added");
  }
  function decided(go: boolean, v: GoDecision | null) {
    setDeciding(null);
    if (v) run(() => api("POST", `/test-events/${ev.id}/approvals`, {
      approval_type: "Go/No-go", decision: !go ? "Rejected" : v.conditions.trim() ? "Conditional" : "Approved",
      comments: v.comments.trim() || undefined, conditions: v.conditions.trim() || undefined,
      present_user_ids: v.present_user_ids, dissents: v.dissents,
    }), go ? "Go decision recorded and sealed. See the Decisions tab." : "No-go recorded and sealed. See the Decisions tab.");
  }
  function decide(go: boolean) { setDeciding(go); }
  async function requestGo() {
    const approvers = (users ?? []).filter((u) => u.roles.includes("DRProgrammeManager") || u.id === ev.owner_user_id);
    run(() => api("POST", `/test-events/${ev.id}/approval-requests`, { approval_type: "Go/No-go", approver_user_ids: approvers.map((u) => u.id) }),
      `Go decision requested from ${approvers.map((u) => u.display_name).join(" and ")}`);
  }

  return (
    <div className="grid-2">
      <div className="stack">
        {feedback}
        <ReadinessGate summary={rd} lifecycle={ev.lifecycle_status} renderItemAction={(item, s) => {
          const mine = ev.can_manage || item.owner_user_id === me.user_id;
          return (
            <div className="task-actions">
              {editable && mine && <button className="btn" onClick={() => setStatus(item)}>Update</button>}
              {editable && mine && item.evidence_required && s === "unmet" && <button className="btn quiet" onClick={() => evidence(item)}>Add evidence</button>}
              {preReady && canException && s === "unmet" && <button className="btn quiet" onClick={() => exception(item)}>Grant exception</button>}
            </div>
          );
        }} />
        {preReady && ev.can_manage && <div><button className="btn" onClick={addItem}>Add readiness condition</button></div>}
        {upload.node}
        {deciding !== null && <GoDecisionDialog ev={ev} me={me} go={deciding} onDone={(v) => decided(deciding, v)} />}
      </div>
      <div className="stack">
        {!me.is_external && <AssistantCards ev={ev} features={["blocker_briefing", "slip_forecast"]} />}
        <Panel title="Go decision">
          {ev.lifecycle_status === "Ready" ? (
            <>
              <p className="small" style={{ marginBottom: 12 }}>
                {rd.go_decision_valid ? "A go decision is recorded. The test manager can start live coordination."
                  : "This test is ready. It needs a go or no-go from an authorised approver."}
              </p>
              <div className="btn-row">
                {canApproveGo(ctx) && !rd.go_decision_valid && <>
                  <button className="btn primary" disabled={busy || !rd.gate.passed} onClick={() => decide(true)}>Record go</button>
                  <button className="btn danger" disabled={busy} onClick={() => decide(false)}>Record no-go</button>
                </>}
                {ev.can_manage && !rd.go_decision_valid && <button className="btn" disabled={busy} onClick={requestGo}>Ask approvers to decide</button>}
              </div>
              {!canApproveGo(ctx) && me.user_id === ev.test_manager_user_id && <p className="xs muted" style={{ marginTop: 10 }}>
                As test manager you can&apos;t approve your own test.</p>}
            </>
          ) : <p className="small muted">{preReady ? "The go decision opens once the test is marked ready." : "Decided. See the history below."}</p>}
        </Panel>
        <Panel title="Decision history" flush>
          {!rd.decisions.length && <p className="panel-empty">No decisions yet.</p>}
          <ul className="rows">
            {rd.decisions.map((d: any) => (
              <li key={d.id} style={{ opacity: d.superseded_at ? 0.55 : 1 }}>
                <div className="grow small">
                  <div><strong>{d.approval_type === "ReadinessException" ? "Readiness exception" : "Go / no-go"}</strong> <Pill value={d.decision} /></div>
                  {d.comments && <div className="muted">{d.comments}</div>}
                  {d.conditions && <div>Conditions: {d.conditions}</div>}
                  <div className="xs muted">{fmtDateTime(d.decided_at)}{d.superseded_at ? ", no longer valid" : ""}</div>
                </div>
              </li>
            ))}
          </ul>
        </Panel>
        {rd.critical_blockers.length > 0 && (
          <Panel title="Open blockers" flush>
            <ul className="rows">{rd.critical_blockers.map((b: any) => (
              <li key={b.id}><span className="code">{b.issue_code}</span><span className="grow small">{b.title}</span>
                {(ev.can_manage || b.owner_user_id === me.user_id)
                  ? <button className="btn" onClick={() => resolve(b)}>Resolve</button> : <Pill value="Blocked" />}</li>
            ))}</ul>
          </Panel>
        )}
      </div>
      {node}
    </div>
  );
}

/* -------------------------------------------------------------------- People */
export function PeopleTab(ctx: Ctx) {
  const { ev } = ctx;
  const [history, setHistory] = useState(false);
  const { data: people, error, reload } = useApi<any[]>(`/test-events/${ev.id}/stakeholders${history ? "?include_history=true" : ""}`);
  const { data: users } = useApi<any[]>(ev.can_manage ? "/users" : null);
  const { data: teams } = useApi<any[]>(ev.can_manage ? "/teams" : null);
  const { run, feedback } = useAction(reload);
  const { ask, node } = useDialog();
  const open = !["Closed", "Cancelled"].includes(ev.lifecycle_status);
  const activeIds = new Set((people ?? []).filter((p) => p.is_active).map((p) => p.user_id));
  const candidates = (users ?? []).filter((u) => !activeIds.has(u.id));

  async function add() {
    const v = await ask({ title: "Add a person to this test", confirm: "Add and notify", description: "They receive an invitation and are asked to acknowledge it.",
      fields: [
        { name: "user_id", label: "Person", kind: "select", required: true, options: candidates.map((u) => ({ value: u.id, label: `${u.display_name}, ${u.department ?? ""}` })) },
        { name: "event_role", label: "Role in this test", required: true, hint: "For example Database recovery lead" },
      ] });
    if (v) run(() => api("POST", `/test-events/${ev.id}/stakeholders`, v), "Added and notified");
  }
  // Teams with at least one active member who isn't already on this test
  const addableTeams = (teams ?? []).map((t) => ({ ...t, fresh: t.members.filter((m: any) => !activeIds.has(m.user_id)) }))
    .filter((t) => t.fresh.length > 0);
  async function addTeam() {
    const v = await ask({ title: "Add a team to this test", confirm: "Add team and notify",
      description: "Everyone in the team who isn't already on this test is added with the same role. Each person receives an invitation and is asked to acknowledge it. You can change or replace individuals afterwards.",
      fields: [
        { name: "team_id", label: "Team", kind: "select", required: true, options: addableTeams.map((t) => {
          const already = t.members.length - t.fresh.length;
          return { value: t.id, label: `${t.name}: ${t.fresh.map((m: any) => m.display_name).join(", ")}${already ? ` (${already} already on this test)` : ""}` };
        }) },
        { name: "event_role", label: "Role in this test", required: true, hint: "Given to everyone added, for example Database recovery" },
      ] });
    if (!v) return;
    const team = addableTeams.find((t) => t.id === v.team_id);
    run(() => api<any[]>("POST", `/test-events/${ev.id}/stakeholders`, v),
      (added) => `Added ${added.length === 1 ? added[0].display_name : `${added.length} people`} from ${team?.name ?? "the team"} and notified`);
  }
  async function replace(p: any) {
    const v = await ask({ title: `Replace ${p.display_name}`, confirm: "Replace", description: "Their open activities move to the new person. Assignment history is kept.",
      fields: [
        { name: "new_user_id", label: "Replacement", kind: "select", required: true, options: candidates.map((u) => ({ value: u.id, label: u.display_name })) },
        { name: "reason", label: "Reason", required: true },
      ] });
    if (v) run(() => api("POST", `/test-stakeholders/${p.id}/replace`, v), `${p.display_name} replaced`);
  }

  return (
    <div className="stack">
      {feedback}
      <Panel title="People" meta={people ? `${people.filter((p) => p.is_active).length} active` : ""} action={<>
        <label className="small" style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={history} onChange={(e) => setHistory(e.target.checked)} /> Show replaced
        </label>
        {ev.can_manage && open && <button className="btn" onClick={addTeam} disabled={!addableTeams.length}
          title={addableTeams.length ? undefined : "Everyone in every team is already on this test"}>Add a team</button>}
        {ev.can_manage && open && <button className="btn" onClick={add}>Add person</button>}
      </>} flush>
        <ErrorNotice error={error} />
        {!people ? <div className="panel-body"><Loading /></div> : (
          <table className="table">
            <thead><tr><th>Person</th><th>Role in this test</th><th>Assignment</th><th>Acknowledged</th><th></th></tr></thead>
            <tbody>
              {people.map((p) => (
                <tr key={p.id} style={{ opacity: p.is_active ? 1 : 0.55 }}>
                  <td><div className="title" style={{ fontSize: "var(--t-sm)" }}>{p.display_name}</div><div className="sub">{p.department}{p.is_external ? ", external" : ""}</div></td>
                  <td>{p.event_role}{p.team_name && <div className="sub">Added with {p.team_name}</div>}</td>
                  <td><Pill value={p.assignment_status} /></td>
                  <td>{p.acknowledged_at ? fmtDateTime(p.acknowledged_at) : <span className="muted">Not yet</span>}</td>
                  <td>{ev.can_manage && open && p.is_active && <button className="btn quiet" onClick={() => replace(p)}>Replace</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      {node}
    </div>
  );
}

/* ------------------------------------------------------------------ Timeline */
export function TimelineTab(ctx: Ctx) {
  const { ev, me } = ctx;
  const { data: tl, error, reload } = useApi<any[]>(`/test-events/${ev.id}/timeline`);
  const { run, feedback } = useAction(reload);
  const [text, setText] = useState("");
  const [showAudit, setShowAudit] = useState(false);
  const around = ["Ready", "Live", "Paused", "Closure"].includes(ev.lifecycle_status);
  const items = (tl ?? []).filter((x) => showAudit || x.kind === "log").slice().reverse();

  function post(kind: "announce" | "status") {
    if (!text.trim()) return;
    run(() => kind === "announce"
      ? api("POST", `/test-events/${ev.id}/announcements`, { message: text })
      : api("POST", `/test-events/${ev.id}/logs`, { log_type: "Status", message: text }),
    kind === "announce" ? "Announcement sent to everyone on this test" : "Update added").then(() => setText(""));
  }

  return (
    <div className="grid-2">
      <Panel title="Timeline" action={!me.is_external && <label className="small" style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <input type="checkbox" checked={showAudit} onChange={(e) => setShowAudit(e.target.checked)} /> Include audit record</label>} flush>
        <ErrorNotice error={error} />
        {!tl ? <div className="panel-body"><Loading /></div> : (
          <ul className="timeline" style={{ padding: "8px 18px" }}>
            {items.map((x, i) => (
              <li key={i} className={x.type}>
                <span className="when" title={fmtDateTime(x.occurred_at, ev.timezone)}>{fmtTime(x.occurred_at, ev.timezone)}<br /><span className="xs">{fmtDateTime(x.occurred_at, ev.timezone).split(",")[0]}</span></span>
                <span className="dot" />
                <div>
                  <div className="what">{x.kind === "log" ? x.message : <span className="muted">{x.type.replace(/[._]/g, " ")}</span>}</div>
                  <div className="who xs">{x.actor ?? (x.source === "System" ? "Platform rule" : x.source)}</div>
                </div>
              </li>
            ))}
            {!items.length && <li><span /><span /><span className="muted">Nothing recorded yet.</span></li>}
          </ul>
        )}
      </Panel>
      <div className="stack">
        {feedback}
        {!me.is_external && <AssistantCards ev={ev} features={["status_summary"]} />}
        {ev.can_manage && !["Closed", "Cancelled"].includes(ev.lifecycle_status) && <MessageDrafts ev={ev} onSent={reload} />}
        {around && (
          <Panel title={ev.can_manage ? "Post to this test" : "Add an update"}>
            <div className="field">
              <label htmlFor="msg">{ev.can_manage ? "Message" : "What's happening?"}</label>
              <textarea id="msg" className="textarea" value={text} onChange={(e) => setText(e.target.value)}
                placeholder={ev.can_manage ? "e.g. Database recovered; application teams can begin" : "e.g. Session store restore at 70%"} />
            </div>
            <div className="btn-row">
              {ev.can_manage && <button className="btn primary" onClick={() => post("announce")} disabled={!text.trim()}>Send announcement</button>}
              <button className="btn" onClick={() => post("status")} disabled={!text.trim()}>Add update</button>
            </div>
            {ev.can_manage && <p className="xs muted" style={{ marginTop: 10 }}>Announcements notify everyone on the test in Teams and the portal. Updates only appear here.</p>}
          </Panel>
        )}
        <Panel title="About this record">
          <p className="small muted">Every update is timestamped in {ev.timezone} and attributed. This timeline, not team chat, is the official record of the test.</p>
        </Panel>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------- Closure */
export function ClosureTab(ctx: Ctx) {
  const { ev, me, isPM, reloadEvent } = ctx;
  const inClosure = ["Closure", "Closed"].includes(ev.lifecycle_status);
  const { data: rep, error, reload } = useApi<any>(inClosure ? `/test-events/${ev.id}/closure-report` : null);
  const refresh = () => { reload(); reloadEvent(); };
  const { run, feedback } = useAction(refresh);
  const { ask, node } = useDialog();

  async function raise() {
    const v = await ask({ title: "Raise an issue or finding", confirm: "Raise", fields: [
      { name: "issue_type", label: "Type", kind: "select", options: ["Finding", "Blocker", "Risk", "Incident"].map((x) => ({ value: x, label: x })) },
      { name: "severity", label: "Severity", kind: "select", initial: "Medium", options: ["Critical", "High", "Medium", "Low"].map((x) => ({ value: x, label: x })) },
      { name: "title", label: "Summary", required: true },
      { name: "description", label: "Details", kind: "textarea" },
    ] });
    if (v) run(() => api("POST", `/test-events/${ev.id}/issues`, v), "Raised");
  }
  async function outcome() {
    const v = await ask({ title: "Record the outcome", confirm: "Save outcome", fields: [
      { name: "outcome", label: "Outcome", kind: "select", initial: ev.outcome ?? "Successful",
        options: (ev.outcome === "Aborted" ? ["Aborted"] : ["Successful", "Partial", "Failed"]).map((x) => ({ value: x, label: x })) },
      { name: "summary", label: "Summary for the closure report", kind: "textarea", required: true, initial: ev.outcome_summary ?? "" },
    ] });
    if (v) run(() => api("PUT", `/test-events/${ev.id}/outcome`, { ...v, expected_version: ev.version }), "Outcome recorded");
  }
  async function signoff(type: string, label: string) {
    const v = await ask({ title: label, confirm: "Sign off", fields: [{ name: "comments", label: "Comments", kind: "textarea" }] });
    if (v) run(() => api("POST", `/test-events/${ev.id}/approvals`, { approval_type: type, decision: "Approved", comments: v.comments || undefined }), `${label}: recorded`);
  }

  if (!inClosure) {
    return (
      <div className="stack">
        {feedback}
        <Panel title="Findings and closure" action={!me.is_external && !["Draft", "Cancelled"].includes(ev.lifecycle_status)
          ? <button className="btn" onClick={raise}>Raise issue or finding</button> : null}>
          <p className="small muted">The closure checklist opens when live coordination is complete. Findings raised during the test appear in the closure report.</p>
        </Panel>
        {node}
      </div>
    );
  }
  if (error) return <ErrorNotice error={error} />;
  if (!rep) return <Loading what="Preparing closure report" />;
  const final = rep.status === "final";
  const signers = [
    { type: "Business", label: "Business sign-off", show: has(me, "BusinessOwner") },
    { type: "RiskReview", label: "Risk review", show: has(me, "RiskCompliance") },
    { type: "Closure", label: "Closure approval", show: (isPM || (me.user_id === ev.owner_user_id && ev.criticality !== "Critical")) && me.user_id !== ev.test_manager_user_id },
  ];

  return (
    <div className="grid-2">
      <div className="stack">
        {feedback}
        {!final && (
          <Panel title="Closure checklist" meta={rep.blockers.length ? `${rep.blockers.length} outstanding` : "Complete"} flush>
            {!rep.blockers.length && <p className="panel-empty">Everything is in place. The test manager can close the test.</p>}
            <ul className="rows">
              {rep.blockers.map((b: string) => <li key={b}><Pill value="Blocked" /><span className="grow small">{b}</span></li>)}
            </ul>
          </Panel>
        )}
        <Panel title={final ? "Closure report" : "Closure report (draft)"} meta={final ? `Frozen ${fmtDateTime(rep.generated_at)}` : "Updates as records change"}
          action={<button className="btn" onClick={() => run(() => downloadFile(`/test-events/${ev.id}/closure-report.pdf`, `${ev.event_code}.pdf`))}>
            {final ? "Download PDF" : "Download draft PDF"}</button>}>
          <dl className="facts" style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0,1fr))", gap: 14, margin: 0 }}>
            <div><dt className="muted small">Outcome</dt><dd style={{ margin: 0, fontWeight: 650 }}>{rep.event.outcome ?? "Not recorded"}</dd></div>
            <div><dt className="muted small">Actual window</dt><dd style={{ margin: 0 }}>{fmtDateTime(rep.event.actual_start_at, ev.timezone)} to {fmtDateTime(rep.event.actual_end_at, ev.timezone)}</dd></div>
          </dl>
          {rep.event.outcome_summary && <p className="small" style={{ marginTop: 12 }}>{rep.event.outcome_summary}</p>}
          <h3 style={{ marginTop: 18 }}>Recovery measurements</h3>
          {!rep.measurements.length ? <p className="small muted">None recorded.</p> : (
            <table className="table" style={{ marginTop: 6 }}>
              <thead><tr><th>Measure</th><th>Target</th><th>Actual</th><th>Result</th></tr></thead>
              <tbody>{rep.measurements.map((m: any, i: number) => (
                <tr key={i}><td>{m.type}</td><td>{m.target}</td><td>{m.actual}</td>
                  <td><span className={`pill ${m.result === "Met" ? "ready" : m.result === "Not Met" ? "blocked" : ""}`}>{m.result}</span></td></tr>
              ))}</tbody>
            </table>
          )}
          <h3 style={{ marginTop: 18 }}>Issues and findings</h3>
          {!rep.issues.length ? <p className="small muted">None raised.</p> : (
            <ul className="rows" style={{ marginTop: 4 }}>{rep.issues.map((i: any) => (
              <li key={i.code} style={{ padding: "8px 0" }}><span className="code">{i.code}</span><span className="grow small">{i.title} ({i.severity})</span><Pill value={i.status === "Open" ? "Pending" : i.status} /></li>
            ))}</ul>
          )}
          <h3 style={{ marginTop: 18 }}>Decisions</h3>
          <ul className="rows" style={{ marginTop: 4 }}>{rep.decisions.filter((d: any) => !d.superseded).map((d: any, i: number) => (
            <li key={i} style={{ padding: "8px 0" }}><span className="grow small"><strong>{d.type}</strong> by {d.by}, {fmtDateTime(d.at)}</span><Pill value={d.decision} /></li>
          ))}</ul>
        </Panel>
      </div>
      <div className="stack">
        {!final && ev.can_manage && (
          <Panel title="Test manager">
            <div className="btn-row">
              <button className="btn" onClick={outcome}>{ev.outcome ? "Change outcome" : "Record outcome"}</button>
              <button className="btn" onClick={raise}>Raise finding</button>
            </div>
          </Panel>
        )}
        {!final && signers.some((s) => s.show) && (
          <Panel title="Your sign-off">
            <p className="small muted" style={{ marginBottom: 10 }}>Business sign-off and risk review come first. Closure approval needs the checklist complete.</p>
            <div className="btn-row">{signers.filter((s) => s.show).map((s) => (
              <button key={s.type} className="btn primary" onClick={() => signoff(s.type, s.label)}>{s.label}</button>
            ))}</div>
          </Panel>
        )}
        <Panel title="People at closure" flush>
          <ul className="rows">{rep.participants.filter((p: any) => p.active).map((p: any, i: number) => (
            <li key={i} style={{ padding: "8px 18px" }}><span className="grow small">{p.name}</span><span className="xs muted">{p.role}</span></li>
          ))}</ul>
        </Panel>
      </div>
      {node}
    </div>
  );
}


/* ------------------------------------------------------------------ Evidence */
const fmtBytes = (n?: number | null) => (n == null ? "" : n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`);

/** Scan result, encryption, retention and legal hold for one uploaded file. */
function EvidenceSafety({ x }: { x: any }) {
  const scan = {
    Clean: <span className="safe ok">Scanned clean</span>,
    NotScanned: <span className="safe muted">Not malware-scanned (development)</span>,
    Pending: <span className="safe wait">Waiting for malware scan; can&apos;t be opened or accepted yet</span>,
    Infected: <span className="safe bad">Quarantined: {x.scan_detail ?? "malware found"}</span>,
    Error: <span className="safe bad">Scan failed: {x.scan_detail ?? "unknown error"}</span>,
  }[x.scan_status as string] ?? null;
  return (
    <div className="sub xs safety">
      {scan}
      {x.encryption && <span className="safe">Encrypted ({x.encryption === "aws:kms" ? "KMS" : x.encryption})</span>}
      {x.retain_until && <span className="safe">Locked until {fmtDate(x.retain_until)}</span>}
      {x.legal_hold && <span className="safe hold" title={x.legal_hold_reason}>Legal hold: {x.legal_hold_reason}</span>}
    </div>
  );
}

export function EvidenceTab(ctx: Ctx) {
  const { ev } = ctx;
  const { data: rows, error, reload } = useApi<any[]>(`/test-events/${ev.id}/evidence`);
  const { run, feedback } = useAction(reload);
  const { ask, node } = useDialog();
  const [checks, setChecks] = useState<Record<string, any>>({});
  const [openPc, setOpenPc] = useState<Record<string, boolean>>({});

  async function review(x: any, accept: boolean) {
    const warns = openWarnings(x.precheck);
    const v = !accept
      ? await ask({ title: `Reject ${x.evidence_code}`, confirm: "Reject", danger: true,
          description: "The person who submitted it is told why, so they can resubmit.",
          fields: [{ name: "comment", label: "What's wrong with it?", kind: "textarea", required: true,
            initial: warns.length ? warns.map((w) => w.title).join(". ") + "." : undefined }] })
      : warns.length
        ? await ask({ title: `Accept ${x.evidence_code} despite the pre-check?`, confirm: "Accept",
            description: `The pre-check raised ${warns.length} thing${warns.length === 1 ? "" : "s"} to check: ${warns.map((w) => w.title).join("; ")}. Say why the file is acceptable; your note is kept with the decision.`,
            fields: [{ name: "comment", label: "Why it's acceptable", kind: "textarea", required: true }] })
        : {};
    if (v) run(() => api("POST", `/test-evidence/${x.id}/review`, { accept, comment: (v as any).comment }),
      accept ? `${x.evidence_code} accepted` : `${x.evidence_code} rejected`);
  }
  async function legalHold(x: any, on: boolean) {
    const v = await ask(on
      ? { title: `Place a legal hold on ${x.evidence_code}`, confirm: "Place legal hold",
          description: "The file is kept beyond its retention date until the hold is released. The hold and its reason are recorded in the audit trail.",
          fields: [{ name: "reason", label: "Reason", kind: "textarea", required: true, hint: "The investigation, dispute or request it relates to" }] }
      : { title: `Release the legal hold on ${x.evidence_code}`, confirm: "Release hold",
          description: `Held for: ${x.legal_hold_reason}. After release the file follows its normal retention period.` });
    if (v) run(() => api("POST", `/test-evidence/${x.id}/legal-hold`, { on, reason: (v as any).reason }),
      on ? `${x.evidence_code} is on legal hold` : `Legal hold released on ${x.evidence_code}`);
  }
  async function markWrong(x: any, f: Finding) {
    const v = await ask({ title: "This check isn't right", confirm: "Save",
      description: `"${f.title}". Say what it got wrong. The reviewer sees your note, and it no longer counts as a warning; the pre-check's accuracy is tracked from these notes.`,
      fields: [{ name: "note", label: "What it got wrong", kind: "textarea", required: true }] });
    if (v) run(() => api("POST", `/test-evidence/${x.id}/precheck/feedback`, { key: f.key, verdict: "wrong", note: (v as any).note }),
      "Noted. The reviewer will see it.");
  }
  async function verify(x: any) {
    run(async () => {
      const r = await api("POST", `/test-evidence/${x.id}/verify`);
      setChecks((c) => ({ ...c, [x.id]: r }));
      if (r.valid === false) throw new Error("The stored file no longer matches its fingerprint. It may have been altered; it won't be served.");
    }, `${x.evidence_code}: file matches its fingerprint`);
  }

  if (error) return <ErrorNotice error={error} />;
  if (!rows) return <Loading what="Loading evidence" />;
  const pending = rows.filter((x) => x.review_status === "Pending").length;
  return (
    <div className="stack">
      {feedback}
      <Panel title="Evidence register" meta={`${rows.length} item${rows.length === 1 ? "" : "s"}${pending ? `, ${pending} awaiting review` : ""}`} flush>
        {!rows.length ? <p className="panel-empty">No evidence yet. Owners add it from their activities or readiness conditions.</p> : (
          <table className="table">
            <thead><tr><th>Evidence</th><th>For</th><th>Submitted</th><th>Review</th><th></th></tr></thead>
            <tbody>
              {rows.map((x) => {
                const chk = checks[x.id];
                return (
                  <Fragment key={x.id}>
                  <tr style={{ opacity: x.superseded ? 0.55 : 1 }}>
                    <td>
                      <div className="title" style={{ fontSize: "var(--t-sm)" }}>{x.filename ?? x.storage_reference}</div>
                      <div className="sub">
                        <span className="code">{x.evidence_code}</span>{" "}
                        {x.storage_backend === "link" ? "Link to an external record" : `${x.evidence_type}, ${fmtBytes(x.size_bytes)}`}
                        {x.superseded ? ", replaced by a newer upload" : ""}
                      </div>
                      {x.storage_backend !== "link" && (
                        <div className="sub xs" title={x.checksum}>
                          Fingerprint {x.checksum?.slice(0, 12)}…
                          {chk ? (chk.valid ? <span style={{ color: "var(--ready)", fontWeight: 600 }}> verified just now</span>
                            : <span className="overdue"> DOES NOT MATCH</span>)
                            : x.last_verified_at ? <> verified {fmtDateTime(x.last_verified_at)}</> : null}
                        </div>
                      )}
                      {x.storage_backend !== "link" && <EvidenceSafety x={x} />}
                      <PrecheckLine pc={x.precheck} open={!!openPc[x.id]} onOpen={() => setOpenPc((o) => ({ ...o, [x.id]: !o[x.id] }))} />
                    </td>
                    <td className="small">{x.activity_code ? <><span className="code">{x.activity_code}</span> {x.activity_title}</> : x.readiness_title ? <>Readiness: {x.readiness_title}</> : "—"}</td>
                    <td className="small">{x.submitted_by_name}<div className="sub">{fmtDateTime(x.submitted_at, ev.timezone)}</div></td>
                    <td><Pill value={x.review_status === "Accepted" ? "Approved" : x.review_status === "Rejected" ? "Rejected" : "Pending"} />
                      {x.review_comment && <div className="sub">{x.review_comment}</div>}
                      {x.reviewed_by_name && <div className="sub">{x.reviewed_by_name}</div>}</td>
                    <td>
                      <div className="task-actions">
                        {x.storage_backend === "link"
                          ? (/^https?:\/\//.test(x.storage_reference) ? <a className="btn quiet" href={x.storage_reference} target="_blank" rel="noopener noreferrer">Open link</a> : null)
                          : <>
                            <button className="btn" disabled={x.scan_status === "Pending" || x.scan_status === "Infected"}
                              title={x.scan_status === "Pending" ? "Available once the malware scan finds it clean" : x.scan_status === "Infected" ? "Quarantined by the malware scan" : undefined}
                              onClick={() => run(() => downloadFile(`/test-evidence/${x.id}/download`, x.filename ?? "evidence"))}>Download</button>
                            <button className="btn quiet" onClick={() => verify(x)}>Verify</button>
                            {x.can_legal_hold && <button className="btn quiet" onClick={() => legalHold(x, !x.legal_hold)}>
                              {x.legal_hold ? "Release hold" : "Legal hold"}</button>}
                          </>}
                        {x.can_review && <>
                          <button className="btn primary" onClick={() => review(x, true)}>Accept</button>
                          <button className="btn" onClick={() => review(x, false)}>Reject</button>
                        </>}
                      </div>
                    </td>
                  </tr>
                  {x.precheck && openPc[x.id] && (
                    <tr className="pc-row">
                      <td colSpan={5}>
                        <PrecheckDetails pc={x.precheck} tz={ev.timezone} canAct={x.can_precheck}
                          onFeedback={(f) => markWrong(x, f)}
                          onRerun={() => run(() => api("POST", `/test-evidence/${x.id}/precheck`), `${x.evidence_code}: pre-check run again`)} />
                      </td>
                    </tr>
                  )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      <Panel title="How evidence is protected">
        <ul className="small muted protect-list">
          <li><strong>Fingerprinted.</strong> Each file&apos;s SHA-256 fingerprint is taken in your browser and on the server, and checked again on every download. A file that has changed isn&apos;t served, and the failure is written to the audit record.</li>
          <li><strong>Pre-checked.</strong> Each readable file is checked against its activity: dates against the test window, recovery figures against the RTO and RPO targets, host names for the wrong environment, copies of other evidence, and card numbers, Aadhaar numbers or passwords that should be redacted. Every finding shows the lines it came from. The reviewer still decides.</li>
          <li><strong>Scanned.</strong> Every upload is scanned for malware before anyone can open or accept it. Infected files are refused, or quarantined if found later, and the submitter and test manager are told.</li>
          <li><strong>Encrypted and retention-locked.</strong> In production, files are kept in encrypted object storage under a retention lock: no one, administrators included, can delete or overwrite them before the retention date. Risk, audit and the programme administrator can place a legal hold to keep a file longer.</li>
        </ul>
      </Panel>
      {node}
    </div>
  );
}
