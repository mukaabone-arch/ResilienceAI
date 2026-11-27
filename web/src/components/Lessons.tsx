"use client";

import Link from "next/link";
import { useState } from "react";
import { api, useApi } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import { Loading, Panel, useAction, useDialog } from "./ui";

/**
 * Lessons from related earlier tests (same series or template, same business services): what blocked, overran,
 * missed its target, was sent back, or is still waiting for remediation. Planners turn a lesson into a
 * readiness condition, or set it aside with a reason.
 */
type Lesson = {
  key: string; kind: string; severity: number; title: string; detail: string; times: number;
  source: { event_id: string; event_code: string; event_title: string; when: string };
  also_in: { event_id: string; event_code: string }[];
  applies_to: { id: string; code: string; title: string } | null;
  suggested_condition: string; happening_again?: boolean; now?: string; overdue?: boolean;
  decision: null | { decision: "Applied" | "Dismissed"; reason?: string; by: string; at: string; readiness_title?: string; readiness_status?: string };
};
type Data = {
  related: { id: string; event_code: string; title: string; when: string; outcome?: string; why: string[] }[];
  lessons: Lesson[]; summary: { total: number; open: number; applied: number; dismissed: number }; can_decide: boolean; note?: string;
};

const KIND: Record<string, string> = {
  open_action: "Open remediation", blocker: "Blocker", missed_target: "Missed target", finding: "Finding",
  overran: "Overran", rejected_evidence: "Evidence sent back",
};

export function LessonsPanel({ eventId, onChanged }: { eventId: string; onChanged?: () => void }) {
  const { data, reload } = useApi<Data>(`/test-events/${eventId}/lessons`);
  const { run, feedback } = useAction(() => { reload(); onChanged?.(); });
  const { ask, node } = useDialog();
  const [all, setAll] = useState(false);
  if (!data) return <Panel title="Lessons from earlier tests"><Loading /></Panel>;
  if (!data.related.length) return null;

  async function apply(l: Lesson) {
    const v = await ask({ title: "Add as a readiness condition", confirm: "Add condition",
      description: `From ${l.source.event_code}: ${l.title}. The condition links back to that test, so reviewers can see why it's there.`,
      fields: [
        { name: "title", label: "Condition", kind: "textarea", required: true, initial: l.suggested_condition },
        { name: "is_mandatory", label: "Blocks the go decision", kind: "select", options: [{ value: "yes", label: "Yes, mandatory" }, { value: "no", label: "No, optional" }] },
        { name: "evidence_required", label: "Evidence required", kind: "select", options: [{ value: "no", label: "No" }, { value: "yes", label: "Yes" }] },
      ] });
    if (v) run(() => api("POST", `/test-events/${eventId}/lessons/${l.key}/apply`, {
      title: v.title, is_mandatory: v.is_mandatory === "yes", evidence_required: v.evidence_required === "yes" }),
      "Added to the readiness conditions");
  }
  async function dismiss(l: Lesson) {
    const v = await ask({ title: "Set this lesson aside", confirm: "Set aside",
      description: `${l.title}. Your reason is kept with this test, for the next planner and for audit.`,
      fields: [{ name: "reason", label: "Why it doesn't apply this time", kind: "textarea", required: true }] });
    if (v) run(() => api("POST", `/test-events/${eventId}/lessons/${l.key}/dismiss`, { reason: v.reason }), "Lesson set aside");
  }

  const shown = all ? data.lessons : data.lessons.slice(0, 4);
  const s = data.summary;
  return (
    <Panel title="Lessons from earlier tests" meta={s.total ? `${s.open} to consider${s.applied ? `, ${s.applied} added` : ""}${s.dismissed ? `, ${s.dismissed} set aside` : ""}` : undefined}>
      {feedback}
      <p className="xs muted lessons-from">
        Learning from{" "}
        {data.related.map((r, i) => (
          <span key={r.id}>{i > 0 && (i === data.related.length - 1 ? " and " : ", ")}
            <Link href={`/tests/${r.id}`} title={r.why.join("; ")}>{r.event_code}</Link> ({fmtDate(r.when)}{r.outcome ? `, ${r.outcome.toLowerCase()}` : ""})</span>
        ))}
        . Related by {Array.from(new Set(data.related.flatMap((r) => r.why))).slice(0, 3).join("; ")}.
      </p>
      {!s.total ? <p className="small muted">Nothing went wrong in those tests that applies here.</p> : (
        <ul className="lessons">
          {shown.map((l) => (
            <li key={l.key} className={`lesson ${l.decision ? "decided" : ""} ${l.happening_again ? "again" : ""}`}>
              <div className="lesson-head">
                <span className={`lesson-kind k-${l.kind}`}>{KIND[l.kind] ?? l.kind}</span>
                {l.applies_to && <span className="lesson-applies">Applies to <span className="code">{l.applies_to.code}</span></span>}
              </div>
              <div className="lesson-title">{l.title}</div>
              <div className="lesson-detail">{l.detail}</div>
              {l.happening_again && <div className="lesson-again">{l.now}</div>}
              <div className="lesson-src xs">
                From <Link href={`/tests/${l.source.event_id}`}>{l.source.event_code}</Link>, {fmtDate(l.source.when)}
                {l.also_in.length > 0 && <> and {l.also_in.map((x) => x.event_code).join(", ")}</>}
              </div>
              {l.decision ? (
                <div className="lesson-decision xs">
                  {l.decision.decision === "Applied"
                    ? <>Added as a readiness condition by {l.decision.by}: <strong>{l.decision.readiness_title}</strong> ({l.decision.readiness_status?.toLowerCase()})</>
                    : <>Set aside by {l.decision.by}: “{l.decision.reason}”
                      {data.can_decide && <> <button className="linkish" onClick={() => run(() => api("DELETE", `/test-events/${eventId}/lessons/${l.key}/decision`), "Lesson brought back")}>Undo</button></>}</>}
                </div>
              ) : data.can_decide ? (
                <div className="task-actions lesson-actions">
                  <button className="btn" onClick={() => apply(l)}>Add as readiness condition</button>
                  <button className="btn quiet" onClick={() => dismiss(l)}>Not relevant</button>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {data.lessons.length > 4 && (
        <button className="linkish" style={{ marginTop: 10 }} onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `Show all ${data.lessons.length} lessons`}</button>
      )}
      {!data.can_decide && data.note && s.open > 0 && <p className="xs muted" style={{ marginTop: 8 }}>{data.note}</p>}
      {node}
    </Panel>
  );
}
