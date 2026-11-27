"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { api, useApi } from "@/lib/api";
import { fmtDateTime, relative, tzLabel } from "@/lib/format";
import { ErrorNotice, Loading, Pill, useAction, useDialog } from "@/components/ui";

/**
 * One-tap acknowledgement, opened from the Acknowledge button in Outlook or Teams.
 * Opening the page changes nothing (mail scanners follow links); the person presses Acknowledge.
 */
export default function AcknowledgePage() {
  const { id } = useParams<{ id: string }>();
  const { data: ack, error, reload } = useApi<any>(`/acknowledgements/${id}`);
  const { run, feedback } = useAction(reload);
  const { ask, node } = useDialog();

  async function decline() {
    const v = await ask({ title: "Decline this assignment", confirm: "Decline", danger: true,
      description: "The test manager is told straight away so they can nominate someone else.",
      fields: [{ name: "comment", label: "Reason", kind: "textarea", required: true,
                 hint: "For example: on leave that week; suggest Rahul Das instead" }] });
    if (v) run(() => api("POST", `/acknowledgements/${id}`, { accept: false, comment: v.comment }), "Declined. The test manager has been told.");
  }

  if (error) return <div className="ack-page"><ErrorNotice error={error} /><Link href="/workspace" className="small">Go to My work</Link></div>;
  if (!ack) return <div className="ack-page"><Loading /></div>;
  const ev = ack.event;
  const pending = ack.status === "Pending";
  const overdue = pending && ack.due_at && new Date(ack.due_at) < new Date();
  return (
    <div className="ack-page">
      {feedback}
      <section className="panel ack-card" aria-labelledby="ack-title">
        <div className="ack-top">
          <span className="small muted">{ev.event_code}</span>
          <Pill value={ev.lifecycle_status} />
        </div>
        <h1 id="ack-title">{ack.assignment ? "You've been assigned to a resilience test" : ack.subject}</h1>
        <p className="ack-test">{ev.title}</p>
        <dl className="ack-facts">
          {ack.assignment && <div><dt>Your role</dt><dd>{ack.assignment.event_role}</dd></div>}
          {ack.assignment?.responsibility && <div><dt>Responsibility</dt><dd>{ack.assignment.responsibility}</dd></div>}
          <div><dt>Window ({tzLabel(ev.timezone)})</dt><dd>{fmtDateTime(ev.planned_start_at, ev.timezone)} to {fmtDateTime(ev.planned_end_at, ev.timezone)}</dd></div>
          <div><dt>Criticality</dt><dd>{ev.criticality}</dd></div>
          {pending && ack.due_at && <div><dt>Please respond</dt><dd className={overdue ? "blocked-text" : ""}>{overdue ? `Overdue, was due ${relative(ack.due_at)}` : `by ${fmtDateTime(ack.due_at)}`}</dd></div>}
        </dl>

        {pending ? (
          <div className="ack-actions">
            <button className="btn primary ack-go" onClick={() => run(() => api("POST", `/acknowledgements/${id}`, { accept: true }), "Acknowledged. Thank you.")}>
              Acknowledge
            </button>
            <button className="btn" onClick={decline}>I can&apos;t do this</button>
          </div>
        ) : (
          <div className={`ack-done ${ack.status === "Declined" ? "declined" : ""}`} role="status">
            <strong>{ack.status === "Declined" ? "You declined" : "You acknowledged"}</strong>
            {ack.responded_at && <span> on {fmtDateTime(ack.responded_at)}</span>}
            {ack.response_comment && <p className="small" style={{ marginTop: 4 }}>&ldquo;{ack.response_comment}&rdquo;</p>}
          </div>
        )}
        <p className="small" style={{ marginTop: 18 }}>
          <Link href={`/tests/${ev.id}`}>Open the test</Link><span className="muted"> · </span><Link href="/workspace">My work</Link>
        </p>
      </section>
      {node}
    </div>
  );
}
