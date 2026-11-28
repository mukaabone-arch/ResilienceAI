"use client";

import { useState } from "react";
import { api, useApi } from "@/lib/api";
import { fmtDate, fmtDateTime, relative } from "@/lib/format";
import { Panel, useAction, useDialog } from "./ui";

/** Oversight of the bank's own AI agents: who is accountable, what each may do, what it did, and a way to stop it. */
const SCOPE_NAMES: Record<string, string> = {
  read: "read its tests", acknowledge: "accept assignments", update_status: "update its activities",
  report_blocker: "report blockers", post_update: "post updates", attach_evidence: "attach evidence",
  request_run: "ask for runbooks",
};

export function AgentsPanel() {
  const { data, error, reload } = useApi<{ agents: any[]; recent: any[] }>("/agents");
  const { run, feedback } = useAction(reload);
  const { ask, node } = useDialog();
  const [showLog, setShowLog] = useState(false);
  if (error || !data || !data.agents.length) return null;

  async function suspend(a: any) {
    const v = await ask({ title: `Suspend ${a.name}?`, confirm: "Suspend now", danger: true,
      description: "Its key stops working immediately and its pending run requests are withdrawn. Operations can resume it after review.",
      fields: [{ name: "reason", label: "Why", kind: "textarea", required: true }] });
    if (v) run(() => api("POST", `/agents/${a.id}/suspend`, { reason: v.reason }), `${a.name} suspended`);
  }

  return (
    <Panel title="AI agents" meta={`${data.agents.length} registered`}>
      {feedback}
      <ul className="agents">
        {data.agents.map((a) => {
          const state = a.suspended_at ? "suspended" : a.expired ? "expired" : "active";
          return (
            <li key={a.id} className={`agent ${state}`}>
              <div className="agent-head">
                <strong>{a.name}</strong>
                <span className={`agent-state ${state}`}>{state === "active" ? "Active" : state === "expired" ? "Key expired" : "Suspended"}</span>
                {a.can_suspend && state === "active" && <button className="btn danger" onClick={() => suspend(a)}>Suspend</button>}
              </div>
              <p className="small">{a.purpose}</p>
              <dl className="agent-facts">
                <div><dt>Accountable</dt><dd>{a.accountable}</dd></div>
                <div><dt>May</dt><dd>{a.scopes.map((s: string) => SCOPE_NAMES[s] ?? s).join(", ")}</dd></div>
                <div><dt>Last 24 hours</dt><dd>{a.calls_24h} call{a.calls_24h === 1 ? "" : "s"}{a.refused_24h ? `, ${a.refused_24h} refused` : ""}</dd></div>
                <div><dt>Last used</dt><dd>{a.last_used_at ? relative(a.last_used_at) : "never"}</dd></div>
                <div><dt>Key</dt><dd>…{a.key_hint}, expires {fmtDate(a.expires_at)}</dd></div>
              </dl>
              {a.suspended_at && <p className="xs agent-why">Suspended {fmtDateTime(a.suspended_at)}: {a.suspended_reason}</p>}
            </li>
          );
        })}
      </ul>
      <p className="xs muted" style={{ marginTop: 10 }}>Agents can never approve, review, accept evidence or change a test&apos;s stage.
        Runs they ask for wait for their accountable person or the test manager.</p>
      {data.recent.length > 0 && (
        <>
          <button className="linkish" onClick={() => setShowLog(!showLog)}>{showLog ? "Hide" : "Show"} the latest calls</button>
          {showLog && (
            <table className="table agent-log">
              <thead><tr><th>When</th><th>Agent</th><th>Call</th><th>Result</th></tr></thead>
              <tbody>{data.recent.map((x) => (
                <tr key={x.id}><td className="small">{fmtDateTime(x.at)}</td><td className="small">{x.agent}</td>
                  <td className="small"><code>{x.method} {x.path.replace("/api/v1", "")}</code></td>
                  <td className={`small ${x.outcome === "done" ? "" : "overdue"}`}>{x.outcome === "done" ? "Done" : `Refused (${x.status_code})`}</td></tr>
              ))}</tbody>
            </table>
          )}
        </>
      )}
      {node}
    </Panel>
  );
}
