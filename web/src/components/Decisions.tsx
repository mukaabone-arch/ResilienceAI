"use client";

/**
 * Go/no-go decision records: the decision dialog (who's present, disagreement raised in the call), the sealed records
 * with their verification and dissent, the replay of the test, and the audit pack download.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, downloadFile, useApi } from "@/lib/api";
import { fmtDateTime, tzLabel } from "@/lib/format";
import { ErrorNotice, Loading, Panel, Pill, useAction, useDialog } from "./ui";

type Person = { id: string; name: string; note?: string };
type DissentRow = { user_id: string; kind: "Dissent" | "Concern"; reason: string };
export type GoDecision = { comments: string; conditions: string; present_user_ids: string[]; dissents: DissentRow[] };

/** Everyone who could take part: the people on the test, its managers and the decider. */
function usePeople(ev: any, me: any): Person[] | null {
  const { data: st } = useApi<any[]>(`/test-events/${ev.id}/stakeholders`);
  const { data: users } = useApi<any[]>("/users");
  return useMemo(() => {
    if (!st || !users) return null;
    const byId = new Map(users.map((u) => [u.id, u]));
    const out = new Map<string, Person>();
    const add = (id: string, note?: string) => {
      const u = byId.get(id);
      if (u && !out.has(id)) out.set(id, { id, name: u.display_name, note });
    };
    add(me.user_id, "you");
    if (ev.test_manager_user_id) add(ev.test_manager_user_id, "test manager");
    if (ev.owner_user_id) add(ev.owner_user_id, "owner");
    st.filter((s) => s.is_active).forEach((s) => add(s.user_id, s.event_role));
    return [...out.values()];
  }, [st, users, ev, me]);
}

export function GoDecisionDialog({ ev, me, go, onDone }: {
  ev: any; me: any; go: boolean; onDone: (v: GoDecision | null) => void;
}) {
  const people = usePeople(ev, me);
  const [present, setPresent] = useState<Set<string> | null>(null);
  const [comments, setComments] = useState("");
  const [conditions, setConditions] = useState("");
  const [dissents, setDissents] = useState<DissentRow[]>([]);
  const first = useRef<HTMLTextAreaElement | null>(null);
  useEffect(() => {
    if (people && !present) setPresent(new Set([me.user_id, ev.test_manager_user_id].filter(Boolean)));
  }, [people, present, me, ev]);
  useEffect(() => {
    first.current?.focus();
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onDone(null);
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onDone]);
  const incomplete = (!go && !comments.trim()) || dissents.some((d) => !d.user_id || !d.reason.trim());
  const toggle = (id: string) => setPresent((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const setRow = (i: number, patch: Partial<DissentRow>) => setDissents((ds) => ds.map((d, j) => (j === i ? { ...d, ...patch } : d)));

  return createPortal(
    <div className="dialog-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onDone(null)}>
      <form className="dialog decision-dialog" role="dialog" aria-modal="true" aria-labelledby="go-title"
        onSubmit={(e) => { e.preventDefault(); if (!incomplete) onDone({ comments, conditions, present_user_ids: [...(present ?? [])], dissents }); }}>
        <header>
          <h2 id="go-title">{go ? "Record go decision" : "Record no-go decision"}</h2>
          <p>The decision is sealed with a snapshot of the readiness state, who was present and any disagreement.
            The sealed record can&apos;t be changed afterwards.</p>
        </header>
        <div className="body">
          <div className="field">
            <label htmlFor="go-notes">{go ? "Notes (optional)" : "Reason"}</label>
            <textarea id="go-notes" ref={first} className="textarea" value={comments} onChange={(e) => setComments(e.target.value)} />
          </div>
          {go && (
            <div className="field">
              <label htmlFor="go-cond">Conditions attached to the go (optional)</label>
              <textarea id="go-cond" className="textarea" value={conditions} onChange={(e) => setConditions(e.target.value)}
                placeholder="e.g. Abort if replica lag exceeds 5 minutes" />
            </div>
          )}
          <fieldset className="field present-list">
            <legend>Who took part in the decision</legend>
            {!people ? <Loading /> : people.map((p) => (
              <label key={p.id}>
                <input type="checkbox" checked={present?.has(p.id) ?? false} onChange={() => toggle(p.id)} />
                <span>{p.name}{p.note && <span className="muted"> · {p.note}</span>}</span>
              </label>
            ))}
          </fieldset>
          <fieldset className="field">
            <legend>Disagreement raised in the call</legend>
            <span className="hint">Recorded with the decision, under the person&apos;s name. Anyone can also add their own afterwards.</span>
            {dissents.map((d, i) => (
              <div className="dissent-row" key={i}>
                <select className="select" aria-label="Who" value={d.user_id} onChange={(e) => setRow(i, { user_id: e.target.value })}>
                  <option value="">Who…</option>
                  {(people ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
                <select className="select" aria-label="Kind" value={d.kind} onChange={(e) => setRow(i, { kind: e.target.value as DissentRow["kind"] })}>
                  <option value="Concern">Concern</option>
                  <option value="Dissent">Disagrees</option>
                </select>
                <input className="input" aria-label="Reason" placeholder="What they said" value={d.reason}
                  onChange={(e) => setRow(i, { reason: e.target.value })} />
                <button type="button" className="btn quiet" aria-label="Remove" onClick={() => setDissents((ds) => ds.filter((_, j) => j !== i))}>Remove</button>
              </div>
            ))}
            <div><button type="button" className="btn quiet" onClick={() => setDissents((ds) => [...ds, { user_id: "", kind: "Concern", reason: "" }])}>
              Add a concern or disagreement</button></div>
          </fieldset>
        </div>
        <footer>
          <button type="button" className="btn" onClick={() => onDone(null)}>Cancel</button>
          <button type="submit" className={`btn ${go ? "primary" : "danger"}`} disabled={incomplete}>{go ? "Record go" : "Record no-go"}</button>
        </footer>
      </form>
    </div>,
    document.body,
  );
}

const DECISION_LABEL: Record<string, string> = { Approved: "Go", Conditional: "Go with conditions", Rejected: "No-go" };

function Seal({ r }: { r: any }) {
  return r.verified ? (
    <span className="seal ok" title={`SHA-256 ${r.content_sha256}\nWritten into the audit trail at record #${r.audit_seq}`}>
      Sealed and verified · {r.content_sha256.slice(0, 10)}… · audit #{r.audit_seq}
    </span>
  ) : (
    <span className="seal bad">Doesn&apos;t match its seal: {r.fingerprint_matches ? "audit anchor missing" : "content changed since it was sealed"}</span>
  );
}

function RecordCard({ r, ev, canDissent, onDissent }: { r: any; ev: any; canDissent: boolean; onDissent: (r: any) => void }) {
  const s = r.snapshot;
  const [open, setOpen] = useState(false);
  const unmetOrExcepted = s.readiness.items.filter((i: any) => i.exception || (i.mandatory && i.status !== "Ready"));
  return (
    <section className="panel decision-card">
      <div className="decision-head">
        <div>
          <div className={`decision-verdict ${r.decision === "Rejected" ? "nogo" : "go"}`}>{DECISION_LABEL[r.decision] ?? r.decision}</div>
          <div className="small">by {r.decided_by} · {fmtDateTime(r.decided_at, ev.timezone)} {tzLabel(ev.timezone)}</div>
        </div>
        <Seal r={r} />
      </div>
      {(s.decision.conditions || s.decision.comments) && (
        <div className="decision-notes small">
          {s.decision.conditions && <p><strong>Conditions:</strong> {s.decision.conditions}</p>}
          {s.decision.comments && <p><strong>Notes:</strong> {s.decision.comments}</p>}
        </div>
      )}
      <dl className="decision-facts small">
        <div><dt>Readiness</dt><dd>{s.readiness.passed ? "All mandatory conditions met" : `${s.readiness.unmet.length} unmet`}
          {s.readiness.excepted.length ? `, ${s.readiness.excepted.length} by exception` : ""}</dd></div>
        <div><dt>Present</dt><dd>{s.people.present.length ? s.people.present.join(", ") : "Not recorded"}</dd></div>
        <div><dt>Not yet acknowledged</dt><dd>{s.people.not_acknowledged.length ? s.people.not_acknowledged.join(", ") : "Everyone acknowledged"}</dd></div>
        <div><dt>Activities</dt><dd>{Object.entries(s.activities.by_status).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(", ") || "None"}
          {s.activities.blocked.length ? `; blocked: ${s.activities.blocked.map((b: any) => b.code).join(", ")}` : ""}</dd></div>
        {s.open_issues.length > 0 && <div><dt>Open issues</dt><dd>{s.open_issues.map((i: any) => `${i.title} (${i.severity})`).join("; ")}</dd></div>}
        {s.evidence.awaiting_malware_scan > 0 && <div><dt>Evidence</dt><dd>{s.evidence.awaiting_malware_scan} file(s) still waiting for malware scan</dd></div>}
      </dl>
      {unmetOrExcepted.length > 0 && (
        <div className="small" style={{ marginTop: 10 }}>
          <strong>Conditions not met outright</strong>
          <ul className="plain-list">
            {unmetOrExcepted.map((i: any) => (
              <li key={i.title}>{i.title}: {i.exception
                ? <>exception approved by {i.exception.approved_by}: &ldquo;{i.exception.justification}&rdquo;</>
                : <>{i.status}</>}</li>
            ))}
          </ul>
        </div>
      )}
      <div className="dissent-block">
        <div className="dissent-title">
          <strong className="small">Concerns and disagreement</strong>
          {canDissent && <button className="btn quiet" onClick={() => onDissent(r)}>Add mine</button>}
        </div>
        {!r.dissent.length ? <p className="small muted">None recorded.</p> : (
          <ul className="plain-list small">
            {r.dissent.map((d: any) => (
              <li key={d.id}>
                <span className={`dissent-kind ${d.kind === "Dissent" ? "dis" : ""}`}>{d.kind === "Dissent" ? "Disagrees" : "Concern"}</span>{" "}
                <strong>{d.by}</strong>: {d.reason}{" "}
                <span className="muted">({d.during_decision ? "raised in the call" : `added ${fmtDateTime(d.raised_at, ev.timezone)}`})</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <button className="btn quiet small-toggle" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {open ? "Hide" : "Show"} every readiness condition at the time</button>
      {open && (
        <table className="table compact">
          <thead><tr><th>Condition</th><th>Status</th><th>Owner</th><th>Evidence</th></tr></thead>
          <tbody>{s.readiness.items.map((i: any) => (
            <tr key={i.title}><td>{i.title}{i.mandatory ? "" : " (optional)"}</td><td>{i.exception ? "Exception" : i.status}</td>
              <td>{i.owner ?? "Test manager"}</td><td>{i.evidence_required ? (i.evidence_accepted ? "Accepted" : "Required, not accepted") : "Not required"}</td></tr>
          ))}</tbody>
        </table>
      )}
    </section>
  );
}

function Replay({ ev }: { ev: any }) {
  const { data: base, error } = useApi<any>(`/test-events/${ev.id}/replay`);
  const [pos, setPos] = useState<number | null>(null);
  const [state, setState] = useState<any>(null);
  // Audit times have microseconds and the browser has milliseconds: land at the end of the moment's millisecond,
  // so jumping to a moment shows the state *after* it happened.
  const msOf = (iso: string) => new Date(iso).getTime() + 1;
  const start = base ? new Date(base.start).getTime() : 0;
  const end = base ? msOf(base.end) : 0;
  const at = pos ?? end;
  useEffect(() => {
    if (!base) return;
    if (pos === null) { setState(base); return; }
    const h = setTimeout(() => {
      api("GET", `/test-events/${ev.id}/replay?at=${encodeURIComponent(new Date(pos).toISOString())}`).then(setState).catch(() => {});
    }, 150);
    return () => clearTimeout(h);
  }, [pos, base, ev.id]);
  if (error) return <ErrorNotice error={error} />;
  if (!base || !state) return <Loading what="Loading replay" />;
  const s = state.state;
  const span = Math.max(end - start, 1);
  return (
    <Panel title="Replay" meta="The test as it stood at any moment">
      <div className="replay">
        <div className="replay-when">
          <strong>{fmtDateTime(new Date(at).toISOString(), ev.timezone)} {tzLabel(ev.timezone)}</strong>
          {pos !== null && <button className="btn quiet" onClick={() => setPos(null)}>Back to now</button>}
        </div>
        <input type="range" className="replay-slider" min={start} max={end} step={1000} value={at}
          aria-label="Move through the test's history" onChange={(e) => setPos(Number(e.target.value))} />
        <div className="replay-marks" aria-label="Key moments">
          {base.marks.map((m: any) => (
            <button key={m.seq} className="replay-mark" style={{ left: `${((new Date(m.at).getTime() - start) / span) * 100}%` }}
              title={`${m.label} · ${m.by} · ${fmtDateTime(m.at, ev.timezone)}`} aria-label={m.label}
              onClick={() => setPos(msOf(m.at))} />
          ))}
        </div>
        <ol className="replay-moments small">
          {base.marks.map((m: any) => (
            <li key={m.seq}>
              <button className={`link-button ${Math.abs(msOf(m.at) - at) < 1500 ? "here" : ""}`}
                onClick={() => setPos(msOf(m.at))}>{m.label}</button>
              <span className="muted"> {fmtDateTime(m.at, ev.timezone)}, {m.by}</span>
            </li>
          ))}
        </ol>
        <div className="replay-state">
          <div><span className="small muted">Stage</span><div><Pill value={s.stage} /></div></div>
          <div><span className="small muted">Readiness gate</span>
            {!s.readiness.items.length ? <div className="small muted">No conditions defined yet</div>
              : <div className={s.readiness.passed ? "gate-ok" : "gate-no"}>{s.readiness.passed ? "Met" : `${s.readiness.unmet.length} mandatory unmet`}</div>}
            {s.readiness.unmet.length > 0 && <div className="xs muted">{s.readiness.unmet.join("; ")}</div>}</div>
          <div><span className="small muted">Activities</span>
            <div className="small">{Object.entries(s.activities.by_status).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(", ") || "None"}</div>
            {s.activities.blocked.length > 0 && <div className="xs gate-no">Blocked: {s.activities.blocked.join("; ")}</div>}</div>
          <div><span className="small muted">People</span><div className="small">{s.people.acknowledged} of {s.people.assigned} acknowledged</div></div>
          <div><span className="small muted">Go decision</span>
            <div className="small">{s.go_decision ? `${DECISION_LABEL[s.go_decision.decision] ?? s.go_decision.decision} by ${s.go_decision.by}${s.go_decision.superseded_at ? " (since withdrawn)" : ""}` : "None yet"}</div></div>
        </div>
        <p className="xs muted">{state.note}</p>
      </div>
    </Panel>
  );
}

export function DecisionsTab({ ev }: { ev: any }) {
  const { data, error, reload } = useApi<any>(`/test-events/${ev.id}/decision-records`);
  const { run, feedback } = useAction(reload);
  const { ask, node } = useDialog();
  async function dissent(r: any) {
    const v = await ask({ title: `Add your view on the ${DECISION_LABEL[r.decision]?.toLowerCase() ?? "decision"}`, confirm: "Add to the record",
      description: "It's added under your name with today's time. It can't be edited or removed later.",
      fields: [
        { name: "kind", label: "This is", kind: "select", options: [{ value: "Dissent", label: "Disagreement with the decision" }, { value: "Concern", label: "A concern" }] },
        { name: "reason", label: "Why", kind: "textarea", required: true },
      ] });
    if (v) run(() => api("POST", `/decision-records/${r.id}/dissent`, { kind: v.kind, reason: v.reason }), "Added to the decision record");
  }
  if (error) return <ErrorNotice error={error} />;
  if (!data) return <Loading what="Loading decisions" />;
  return (
    <div className="stack">
      {feedback}
      <div className="decisions-top">
        <p className="small muted">Every go/no-go decision is sealed with what it rested on. Changing a sealed record afterwards is detected.</p>
        {data.can_download_pack && (
          <button className="btn" onClick={() => run(() => downloadFile(`/test-events/${ev.id}/audit-pack`, `${ev.event_code}-audit-pack.zip`),
            "Audit pack downloaded. Run verify.py inside it to check it without ResilienceAI.")}>Download audit pack</button>
        )}
      </div>
      {!data.records.length
        ? <Panel title="Decision records"><p className="panel-empty">No go/no-go decision yet. It&apos;s recorded from the Readiness and go tab once the test is Ready.</p></Panel>
        : data.records.map((r: any) => <RecordCard key={r.id} r={r} ev={ev} canDissent={data.can_add_dissent} onDissent={dissent} />)}
      <Replay ev={ev} />
      {node}
    </div>
  );
}
