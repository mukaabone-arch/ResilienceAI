"use client";

import Link from "next/link";
import { ActivityActions } from "@/components/ActivityActions";
import { ErrorNotice, Loading, Panel, Pill, useAction, useDialog } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { useMe } from "@/lib/me";
import { fmtDateTime, relative } from "@/lib/format";

export default function Workspace() {
  const { data: me } = useMe() as { data: any };
  const { data: ws, error, reload } = useApi<any>("/me/workspace");
  const { run, feedback } = useAction(reload);
  const { ask, node } = useDialog();

  if (error) return <ErrorNotice error={error} />;
  if (!ws || !me) return <Loading what="Loading your work" />;

  const first = me.display_name.split(" ")[0];
  const open = ws.assigned_activities;
  const overdueIds = new Set(ws.overdue.map((a: any) => a.id));
  const byEvent = new Map<string, any[]>();
  open.forEach((a: any) => byEvent.set(a.event_id, [...(byEvent.get(a.event_id) ?? []), a]));
  const eventMeta = new Map<string, any>(ws.upcoming_tests.map((e: any) => [e.id, e]));

  async function respond(ack: any, accept: boolean) {
    const v = accept ? {} : await ask({ title: "Decline this assignment", confirm: "Decline", danger: true,
      description: "The test manager is told straight away so they can find someone else.",
      fields: [{ name: "comment", label: "Reason", kind: "textarea", required: true }] });
    if (v) run(() => api("POST", `/acknowledgements/${ack.id}`, { accept, comment: (v as any).comment }),
      accept ? "Assignment accepted" : "Assignment declined");
  }

  const strip = [
    { label: "Overdue", n: ws.overdue.length, href: "#activities", alert: ws.overdue.length > 0 },
    { label: "Due today", n: ws.due_today.length, href: "#activities" },
    { label: "To acknowledge", n: ws.pending_acknowledgements.length, href: "#acks", alert: ws.pending_acknowledgements.length > 0 },
    { label: "Reviews for you", n: ws.reviews_awaiting_me.length, href: "#reviews" },
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{first}&apos;s work</h1>
          <p>Activities assigned to you across every test, with what you can start now and what is waiting on someone else.</p>
        </div>
      </div>

      {feedback}
      <nav className="today" aria-label="Summary">
        {strip.map((s) => (
          <a key={s.label} href={s.href} className={s.alert ? "alert" : ""}><strong>{s.n}</strong><span>{s.label}</span></a>
        ))}
      </nav>

      <div className="grid-2">
        <div className="stack">
          <div id="activities">
            <Panel title="My activities" meta={`${open.length} open`} flush>
              {!open.length && <p className="panel-empty">Nothing assigned to you right now.</p>}
              {[...byEvent.entries()].map(([eid, acts]) => (
                <div key={eid}>
                  <div style={{ padding: "12px 18px 4px", display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
                    <Link href={`/tests/${eid}`} style={{ fontWeight: 650 }}>{acts[0].event_title}</Link>
                    <span className="code">{acts[0].event_code}</span>
                    <Pill value={acts[0].event_status} />
                    {eventMeta.get(eid) && <span className="muted xs">starts {relative(eventMeta.get(eid).planned_start_at)}</span>}
                  </div>
                  <ul className="rows">
                    {acts.map((a: any) => (
                      <li key={a.id}>
                        <span className="code" style={{ minWidth: 56 }}>{a.activity_code}</span>
                        <div className="grow">
                          <div style={{ fontWeight: 600 }}>{a.title}</div>
                          <div className="small muted" style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 4 }}>
                            <Pill value={a.status} />
                            {a.status === "Not Started" && a.dependency_state !== "actionable" && <Pill value={a.dependency_state} dep />}
                            {a.due_at && <span className={overdueIds.has(a.id) ? "overdue" : ""}>
                              {overdueIds.has(a.id) ? "Overdue, was due " : "Due "}{fmtDateTime(a.due_at)}</span>}
                            {a.evidence_required && <span>Evidence required</span>}
                          </div>
                          {a.blocked_reason && <p className="small" style={{ marginTop: 6, color: "var(--blocked)" }}>{a.blocked_reason}</p>}
                        </div>
                        <ActivityActions a={a} meId={me.user_id} run={run} />
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </Panel>
          </div>
        </div>

        <div className="stack">
          <div id="acks">
            <Panel title="Waiting for your acknowledgement" flush>
              {!ws.pending_acknowledgements.length && <p className="panel-empty">You&apos;ve acknowledged everything.</p>}
              <ul className="rows">
                {ws.pending_acknowledgements.map((k: any) => (
                  <li key={k.id} style={{ flexDirection: "column", gap: 8 }}>
                    <div className="small">{k.subject.replace(/^\[[^\]]+\]\s*/, "")}</div>
                    <div className="small muted">Please respond by {fmtDateTime(k.due_at)}</div>
                    <div className="btn-row">
                      <button className="btn primary" onClick={() => respond(k, true)}>Accept</button>
                      <button className="btn" onClick={() => respond(k, false)}>Decline</button>
                    </div>
                  </li>
                ))}
              </ul>
            </Panel>
          </div>

          {ws.reviews_awaiting_me.length > 0 && (
            <div id="reviews">
              <Panel title="Reviews for you" flush>
                <ul className="rows">
                  {ws.reviews_awaiting_me.map((r: any) => (
                    <li key={r.id}>
                      <span className="code">{r.activity_code}</span>
                      <div className="grow small"><Link href={`/tests/${r.event_id}?tab=activities`}>{r.title}</Link></div>
                      <ActivityActions a={{ ...r, status: "Submitted", reviewer_user_id: me.user_id, owner_user_id: "-" }} meId={me.user_id} run={run} />
                    </li>
                  ))}
                </ul>
              </Panel>
            </div>
          )}

          <Panel title="My upcoming tests" flush>
            {!ws.upcoming_tests.length && <p className="panel-empty">You aren&apos;t part of any upcoming test.</p>}
            <ul className="rows">
              {ws.upcoming_tests.map((e: any) => (
                <li key={e.id}>
                  <div className="grow">
                    <Link href={`/tests/${e.id}`} style={{ fontWeight: 600 }}>{e.title}</Link>
                    <div className="small muted">{fmtDateTime(e.planned_start_at, e.timezone)} ({relative(e.planned_start_at)})</div>
                  </div>
                  <Pill value={e.lifecycle_status} />
                </li>
              ))}
            </ul>
          </Panel>

          <Panel title="Notifications" flush>
            {!ws.notifications.length && <p className="panel-empty">No notifications.</p>}
            <ul className="rows">
              {ws.notifications.slice(0, 8).map((n: any) => (
                <li key={n.id}>
                  <div className="grow small">
                    <Link href={`/tests/${n.event_id}`}>{n.subject.replace(/^\[[^\]]+\]\s*/, "")}</Link>
                    <div className="muted xs">{relative(n.created_at)}</div>
                  </div>
                </li>
              ))}
            </ul>
          </Panel>
        </div>
      </div>
      {node}
    </>
  );
}
