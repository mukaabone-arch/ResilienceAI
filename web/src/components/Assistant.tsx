"use client";

import { useState } from "react";
import { api, useApi } from "@/lib/api";
import { fmtDateTime, relative } from "@/lib/format";
import { ErrorNotice, Loading, Panel, useAction, useDialog } from "./ui";

type Feature = "blocker_briefing" | "slip_forecast" | "status_summary";
const FEATURE: Record<Feature, { title: string; ask: string; empty: string }> = {
  blocker_briefing: { title: "What's holding up the go", ask: "Brief me", empty: "A plain-English briefing of what stands between this test and the go decision, and who should do what next." },
  slip_forecast: { title: "Readiness forecast", ask: "Forecast", empty: "An estimate of when readiness will be met, what drives that date, and the rules behind it." },
  status_summary: { title: "Status summary", ask: "Summarise", empty: "A short update for executives, written from the timeline, milestones and open issues." },
};
const VERDICT: Record<string, { label: string; cls: string }> = {
  on_track: { label: "On track", cls: "ready" }, at_risk: { label: "At risk", cls: "waiting" },
  likely_slip: { label: "Likely to slip", cls: "blocked" }, ready: { label: "Ready", cls: "ready" },
};

/** Renders the assistant's plain text: "- " bullets, "1. " steps, blank lines as breaks. */
function AiText({ text }: { text: string }) {
  const blocks: React.ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  const flush = (k: number) => {
    if (!list) return;
    const L = list.ordered ? "ol" : "ul";
    blocks.push(<L key={`l${k}`} className="ai-list">{list.items.map((x, i) => <li key={i}>{x}</li>)}</L>);
    list = null;
  };
  text.split("\n").forEach((line, i) => {
    const b = line.match(/^- (.*)/), o = line.match(/^\d+\. (.*)/);
    if (b || o) {
      const ordered = !!o;
      if (!list || list.ordered !== ordered) { flush(i); list = { ordered, items: [] }; }
      list.items.push((b ?? o)![1]);
    } else {
      flush(i);
      if (line.trim()) blocks.push(<p key={i} className={line.endsWith(":") ? "ai-sub" : ""}>{line}</p>);
    }
  });
  flush(-1);
  return <div className="ai-text">{blocks}</div>;
}

function Provenance({ s }: { s: any }) {
  return (
    <p className="xs muted ai-prov">
      Suggested {relative(s.created_at)}{s.requested_by ? "" : " on a schedule"}.{" "}
      {s.provider === "anthropic" ? `Worded by ${s.model ?? "Claude"} from the records.` : "Written from this test's records by the built-in assistant."}
      {s.fallback_reason && <> The external model wasn&apos;t used: {s.fallback_reason}.</>}
    </p>
  );
}

export function AssistantCards({ ev, features }: { ev: any; features: Feature[] }) {
  const { data, error, reload } = useApi<any>(`/test-events/${ev.id}/assistant`);
  const { run, busy, feedback } = useAction(reload);
  const { ask, node } = useDialog();

  async function dismiss(s: any) {
    const v = await ask({ title: "Not useful?", confirm: "Dismiss", description: "Your reason is kept with the suggestion, for whoever reviews the assistant.",
      fields: [{ name: "feedback", label: "What was wrong or missing?", kind: "textarea", required: true }] });
    if (v) run(() => api("POST", `/assistant/suggestions/${s.id}/decision`, { action: "dismiss", feedback: v.feedback }), "Dismissed. Thanks for the feedback.");
  }

  if (error) return <ErrorNotice error={error} />;
  if (!data) return <Loading what="Loading the assistant" />;
  return (
    <>
      {feedback}
      {features.map((f) => {
        const s = data.latest[f];
        const meta = FEATURE[f];
        const v = s?.content?.verdict && VERDICT[s.content.verdict];
        return (
          <section key={f} className="panel ai-card" aria-label={meta.title}>
            <div className="panel-head">
              <h2>{meta.title}</h2>
              <div className="btn-row" style={{ alignItems: "center" }}>
                <span className="ai-tag">Assistant</span>
                <button className="btn quiet" disabled={busy}
                  onClick={() => run(() => api("POST", `/test-events/${ev.id}/assistant`, { feature: f }))}>{s ? "Refresh" : meta.ask}</button>
              </div>
            </div>
            <div className="panel-body">
              {!s ? <p className="small muted">{meta.empty}</p> : (
                <>
                  {v && <p style={{ marginBottom: 8 }}><span className={`pill ${v.cls}`}>{v.label}</span></p>}
                  <AiText text={s.content.text} />
                  {s.sources?.length > 1 && (
                    <p className="xs muted" style={{ marginTop: 10 }}>Based on: {s.sources.filter((x: any) => x.type !== "event").slice(0, 8).map((x: any) => x.label).join(", ")}</p>
                  )}
                  <Provenance s={s} />
                  {s.status === "Proposed" ? (
                    <div className="btn-row" style={{ marginTop: 10 }}>
                      <button className="btn" disabled={busy} onClick={() => run(() => api("POST", `/assistant/suggestions/${s.id}/decision`, { action: "accept" }), "Marked helpful")}>Helpful</button>
                      <button className="btn quiet" disabled={busy} onClick={() => dismiss(s)}>Not useful</button>
                    </div>
                  ) : <p className="xs muted" style={{ marginTop: 8 }}>{s.status === "Accepted" ? "Marked helpful" : s.status} by {s.decided_by_name} {fmtDateTime(s.decided_at)}</p>}
                </>
              )}
            </div>
          </section>
        );
      })}
      {node}
    </>
  );
}

const KINDS = [
  { kind: "reminder", label: "Reminder", hint: "To people with overdue work, unmet conditions or unacknowledged assignments" },
  { kind: "escalation", label: "Escalation", hint: "To the test manager, owner and team leads about blockers" },
  { kind: "readiness_update", label: "Readiness update", hint: "To everyone on the test" },
  { kind: "go_request", label: "Go request", hint: "To the approvers, once the test is ready" },
];

export function MessageDrafts({ ev, onSent }: { ev: any; onSent?: () => void }) {
  const { data, reload } = useApi<any>(`/test-events/${ev.id}/assistant`);
  const { run, busy, feedback } = useAction(() => { reload(); onSent?.(); });
  const { ask, node } = useDialog();
  const [edit, setEdit] = useState<Record<string, { subject: string; intro: string; off: string[] }>>({});
  const drafts = data ? Object.entries(data.latest).filter(([k, s]: any) => k.startsWith("message_draft") && s.status === "Proposed").map(([, s]) => s as any) : [];

  async function discard(s: any) {
    const v = await ask({ title: "Discard this draft?", confirm: "Discard", danger: true,
      fields: [{ name: "feedback", label: "Why isn't it right?", kind: "textarea", required: true }] });
    if (v) run(() => api("POST", `/assistant/suggestions/${s.id}/decision`, { action: "dismiss", feedback: v.feedback }), "Draft discarded");
  }

  return (
    <section className="panel ai-card">
      <div className="panel-head"><h2>Draft a message</h2><span className="ai-tag">Assistant</span></div>
      <div className="panel-body">
        {feedback}
        <p className="small muted" style={{ marginBottom: 10 }}>The assistant drafts from this test&apos;s records. You edit it, choose who gets it, and send it; it goes out in your name.</p>
        <div className="btn-row">
          {KINDS.filter((k) => k.kind !== "go_request" || ev.lifecycle_status === "Ready").map((k) => (
            <button key={k.kind} className="btn" title={k.hint} disabled={busy}
              onClick={() => run(() => api("POST", `/test-events/${ev.id}/assistant`, { feature: "message_draft", kind: k.kind }))}>{k.label}</button>
          ))}
        </div>
        {drafts.map((s: any) => {
          const d = s.content;
          const e = edit[s.id] ?? { subject: d.subject, intro: d.intro, off: [] };
          const set = (x: Partial<typeof e>) => setEdit({ ...edit, [s.id]: { ...e, ...x } });
          const n = d.recipients.length - e.off.length;
          return (
            <div key={s.id} className="draft">
              <div className="small" style={{ fontWeight: 650, marginBottom: 8 }}>{d.label}</div>
              <div className="field"><label htmlFor={`sub-${s.id}`}>Subject</label>
                <input id={`sub-${s.id}`} className="input" value={e.subject} onChange={(x) => set({ subject: x.target.value })} /></div>
              <div className="field"><label htmlFor={`in-${s.id}`}>Message</label>
                <textarea id={`in-${s.id}`} className="textarea" rows={5} value={e.intro} onChange={(x) => set({ intro: x.target.value })} />
                {d.common?.length > 0 && <span className="hint">Followed by: {d.common.filter(Boolean).slice(0, 3).join(" ")}{d.common.length > 3 ? " …" : ""}</span>}</div>
              <fieldset className="field" style={{ border: 0, padding: 0 }}>
                <legend className="small" style={{ fontWeight: 600, marginBottom: 6 }}>Recipients, with what each one is told</legend>
                {d.recipients.map((r: any) => (
                  <label key={r.user_id} className="small recip">
                    <input type="checkbox" checked={!e.off.includes(r.user_id)}
                      onChange={(x) => set({ off: x.target.checked ? e.off.filter((i) => i !== r.user_id) : [...e.off, r.user_id] })} />
                    <span><strong>{r.name}</strong>{r.lines.length ? <span className="muted">: {r.lines.join(" ")}</span> : null}</span>
                  </label>
                ))}
              </fieldset>
              <Provenance s={s} />
              <div className="btn-row" style={{ marginTop: 10 }}>
                <button className="btn primary" disabled={busy || n < 1}
                  onClick={() => run(() => api("POST", `/assistant/suggestions/${s.id}/decision`, {
                    action: "accept", subject: e.subject, intro: e.intro, exclude_recipients: e.off }), `Sent to ${n} ${n === 1 ? "person" : "people"}`)}>
                  Send to {n} {n === 1 ? "person" : "people"}</button>
                <button className="btn quiet" disabled={busy} onClick={() => discard(s)}>Discard</button>
              </div>
            </div>
          );
        })}
      </div>
      {node}
    </section>
  );
}

export function OversightLog() {
  const { data, error } = useApi<any[]>("/assistant/log?limit=100");
  if (error) return null;      // the endpoint is for programme, risk, audit and admin roles only
  if (!data) return <Loading />;
  const label: Record<string, string> = { blocker_briefing: "Blocker briefing", slip_forecast: "Slip forecast", status_summary: "Status summary", message_draft: "Message draft", evidence_precheck: "Evidence pre-check" };
  const outcome = (x: any) => x.feature !== "evidence_precheck" ? x.status
    : x.status === "Accepted" ? "Reviewer agreed" : x.status === "Dismissed" ? "Reviewer disagreed" : x.status === "Proposed" ? "Awaiting review" : x.status;
  const pcs = data.filter((x: any) => x.feature === "evidence_precheck" && ["Accepted", "Dismissed"].includes(x.status));
  const agreed = pcs.filter((x: any) => x.status === "Accepted").length;
  const counts = data.reduce((m: Record<string, number>, x: any) => ({ ...m, [x.status]: (m[x.status] ?? 0) + 1 }), {});
  return (
    <Panel title="Assistant oversight" meta={Object.entries(counts).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(", ")
      + (pcs.length ? `. Evidence pre-checks: reviewers agreed with ${agreed} of ${pcs.length}` : "")} flush>
      {!data.length ? <p className="panel-empty">The assistant hasn&apos;t made any suggestions yet.</p> : (
        <table className="table">
          <thead><tr><th>Suggestion</th><th>Test</th><th>Outcome</th><th>Source</th></tr></thead>
          <tbody>
            {data.slice(0, 40).map((x: any) => (
              <tr key={x.id}>
                <td><div className="title" style={{ fontSize: "var(--t-sm)" }}>{label[x.feature] ?? x.feature}</div>
                  <div className="sub">{x.subject ?? x.headline}</div></td>
                <td className="small"><span className="code">{x.event_code}</span><div className="sub">{fmtDateTime(x.created_at)}{x.requested_by ? `, ${x.feature === "evidence_precheck" ? "uploaded" : "asked"} by ${x.requested_by}` : x.feature === "evidence_precheck" ? ", after malware scan" : ", scheduled"}</div></td>
                <td className="small">{outcome(x)}{x.decided_by ? ` by ${x.decided_by}` : ""}{x.feedback && <div className="sub">“{x.feedback}”</div>}</td>
                <td className="small">{x.provider === "anthropic" ? x.model : "Built-in"}{x.fallback_reason && <div className="sub">Fallback: {x.fallback_reason}</div>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
