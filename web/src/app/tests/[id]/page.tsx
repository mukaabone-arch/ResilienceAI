"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { ErrorNotice, Loading, Pill, useAction, useDialog } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { fmtDateTime, relative, tzLabel } from "@/lib/format";
import { ActivitiesTab, ClosureTab, OverviewTab, PeopleTab, ReadinessTab, TimelineTab } from "./tabs";

const TRACK = ["Draft", "Planned", "Mobilising", "Readiness", "Ready", "Live", "Closure", "Closed"];
const TABS = [
  { key: "overview", label: "Overview" },
  { key: "activities", label: "Activities" },
  { key: "readiness", label: "Readiness and go" },
  { key: "people", label: "People" },
  { key: "timeline", label: "Timeline" },
  { key: "closure", label: "Findings and closure" },
];

type ActionMeta = { label: string; pm?: boolean; reason?: string; danger?: boolean; back?: boolean };
const ACTIONS: Record<string, ActionMeta> = {
  submit: { label: "Submit plan for approval" },
  approve_plan: { label: "Approve plan", pm: true },
  return_to_draft: { label: "Send back to draft", pm: true, reason: "Why is the plan being sent back?", back: true },
  begin_readiness: { label: "Start readiness tracking" },
  mark_ready: { label: "Mark ready for go decision" },
  return_to_mobilising: { label: "Return to mobilising", reason: "What still needs preparing?", back: true },
  start: { label: "Start live coordination" },
  pause: { label: "Pause", reason: "Why is coordination pausing?", back: true },
  resume: { label: "Resume" },
  complete: { label: "Complete coordination" },
  abort: { label: "Stop the test", reason: "Why is the test being stopped? The outcome will be recorded as aborted.", danger: true, back: true },
  close: { label: "Close test" },
  cancel: { label: "Cancel test", pm: true, reason: "Why is this test being cancelled?", danger: true, back: true },
};

const DONE: Record<string, string> = {
  submit: "Plan submitted for approval", approve_plan: "Plan approved", return_to_draft: "Plan sent back to draft",
  begin_readiness: "Readiness tracking started", mark_ready: "Marked ready. Approvers can now record the go decision.",
  return_to_mobilising: "Returned to mobilising", start: "Live coordination started. Everyone on the test has been notified.",
  pause: "Coordination paused", resume: "Coordination resumed", complete: "Coordination completed. The closure checklist is open.",
  abort: "Test stopped", close: "Test closed. The closure report is now frozen.", cancel: "Test cancelled",
};

export default function EventPage() {
  return <Suspense fallback={<Loading />}><EventWorkspace /></Suspense>;
}

function EventWorkspace() {
  const { id } = useParams<{ id: string }>();
  const tab = useSearchParams().get("tab") ?? "overview";
  const { data: ev, error, reload } = useApi<any>(`/test-events/${id}`);
  const { data: me } = useApi<any>("/me");
  const { run, busy, feedback } = useAction(reload);
  const { ask, node } = useDialog();

  if (error) return <><Link href="/tests" className="small">All tests</Link><div style={{ marginTop: 12 }}><ErrorNotice error={error} /></div></>;
  if (!ev || !me) return <Loading what="Loading test" />;

  const isPM = me.roles.includes("DRProgrammeManager");
  const status = ev.lifecycle_status === "Paused" ? "Live" : ev.lifecycle_status;
  const idx = TRACK.indexOf(status);
  const actions = (ev.allowed_actions as string[]).filter((a) => ACTIONS[a] && (ACTIONS[a].pm ? isPM : ev.can_manage));
  const forward = actions.filter((a) => !ACTIONS[a].back);
  const backward = actions.filter((a) => ACTIONS[a].back);

  const blockedWhy: Record<string, string | undefined> = {
    mark_ready: ev.readiness.passed ? undefined : "Mandatory readiness conditions are not all met",
    start: !ev.readiness.passed ? "Mandatory readiness conditions are not all met" : !ev.go_decision_valid ? "Waiting for the go decision" : undefined,
    begin_readiness: ev.stakeholder_count ? undefined : "Add people to the test first",
  };

  async function act(action: string) {
    const m = ACTIONS[action];
    let reason: string | undefined;
    if (m.reason) {
      const v = await ask({ title: m.label, confirm: m.label, danger: m.danger, description: m.reason,
        fields: [{ name: "reason", label: "Reason", kind: "textarea", required: true }] });
      if (!v) return;
      reason = v.reason;
    }
    run(() => api("POST", `/test-events/${id}/transitions`, { action, reason, expected_version: ev.version }), DONE[action] ?? "Updated");
  }

  const ctx = { ev, me, isPM, reloadEvent: reload };
  return (
    <>
      <Link href="/tests" className="small muted">All tests</Link>
      <header className="event-head" style={{ marginTop: 10 }}>
        <div className="top">
          <div>
            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <span className="code">{ev.event_code}</span>
              <Pill value={ev.lifecycle_status} />
              {ev.outcome && <span className="small" style={{ fontWeight: 600 }}>Outcome: {ev.outcome}</span>}
            </div>
            <h1>{ev.title}</h1>
          </div>
          <div className="btn-row" style={{ alignItems: "flex-start" }}>
            {backward.map((a) => (
              <button key={a} className={`btn ${ACTIONS[a].danger ? "danger" : ""}`} disabled={busy} onClick={() => act(a)}>{ACTIONS[a].label}</button>
            ))}
            {forward.map((a, i) => (
              <button key={a} className={`btn ${i === forward.length - 1 ? "primary" : ""}`} disabled={busy || !!blockedWhy[a]}
                title={blockedWhy[a]} onClick={() => act(a)}>{ACTIONS[a].label}</button>
            ))}
          </div>
        </div>
        {forward.some((a) => blockedWhy[a]) && (
          <p className="xs muted" style={{ textAlign: "right", marginTop: 6 }}>{forward.map((a) => blockedWhy[a]).filter(Boolean)[0]}</p>
        )}
        <dl className="facts">
          <div><dt>Window ({tzLabel(ev.timezone)})</dt><dd>{fmtDateTime(ev.planned_start_at, ev.timezone)} to {fmtDateTime(ev.planned_end_at, ev.timezone)}</dd></div>
          <div><dt>Starts</dt><dd>{ev.actual_start_at ? `Started ${relative(ev.actual_start_at)}` : relative(ev.planned_start_at)}</dd></div>
          <div><dt>Criticality</dt><dd>{ev.criticality}</dd></div>
          <div><dt>Category</dt><dd>{ev.category}</dd></div>
          {ev.status_reason && <div><dt>Latest reason</dt><dd style={{ fontWeight: 500, maxWidth: "48ch" }}>{ev.status_reason}</dd></div>}
        </dl>
        <ol className="track" aria-label="Lifecycle">
          {ev.lifecycle_status === "Cancelled" ? (
            <li className="cancelled">Cancelled</li>
          ) : TRACK.map((s, i) => (
            <li key={s} className={i < idx ? "past" : i === idx ? "current" : ""} aria-current={i === idx ? "step" : undefined}>
              {s === "Live" && ev.lifecycle_status === "Paused" ? "Live (paused)" : s}
            </li>
          ))}
        </ol>
      </header>

      {feedback}
      <nav className="tabs" aria-label="Test sections">
        {TABS.map((t) => (
          <Link key={t.key} href={`/tests/${id}?tab=${t.key}`} aria-current={tab === t.key ? "page" : undefined} scroll={false}>{t.label}</Link>
        ))}
      </nav>

      {tab === "overview" && <OverviewTab {...ctx} />}
      {tab === "activities" && <ActivitiesTab {...ctx} />}
      {tab === "readiness" && <ReadinessTab {...ctx} />}
      {tab === "people" && <PeopleTab {...ctx} />}
      {tab === "timeline" && <TimelineTab {...ctx} />}
      {tab === "closure" && <ClosureTab {...ctx} />}
      {node}
    </>
  );
}
