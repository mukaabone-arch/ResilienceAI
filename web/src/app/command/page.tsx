"use client";

import Link from "next/link";
import { useState } from "react";
import { ErrorNotice, Loading, Panel, Pill } from "@/components/ui";
import { useApi } from "@/lib/api";
import { fmtDateTime, relative } from "@/lib/format";

const ORDER = ["Draft", "Planned", "Mobilising", "Readiness", "Ready", "Live", "Paused", "Closure", "Closed", "Cancelled"];

export default function Command() {
  const [horizon, setHorizon] = useState(30);
  const { data: p, error } = useApi<any>(`/portfolio/summary?horizon_days=${horizon}`);
  if (error) return <ErrorNotice error={error.status === 403 ? { message: "The command centre is for programme, risk and management roles." } : error} />;
  if (!p) return <Loading what="Loading the programme" />;
  const sev = ["Critical", "High", "Medium", "Low"];
  const exceptions = [
    ...p.at_risk.map((e: any) => ({ ...e, why: `${e.unmet_mandatory} mandatory readiness condition${e.unmet_mandatory > 1 ? "s" : ""} unmet` })),
    ...p.delayed.map((e: any) => ({ ...e, why: `Past its planned start and still ${e.lifecycle_status.toLowerCase()}` })),
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Command centre</h1>
          <p>The annual programme at a glance: what is at risk, what is coming, and who is carrying the load.
            Figures as of {fmtDateTime(p.data_as_of)}.</p>
        </div>
        <label className="small" style={{ display: "flex", gap: 8, alignItems: "center" }}>Looking ahead
          <select className="select" style={{ width: 120 }} value={horizon} onChange={(e) => setHorizon(Number(e.target.value))}>
            <option value={30}>30 days</option><option value={60}>60 days</option><option value={90}>90 days</option>
          </select>
        </label>
      </div>

      <nav className="today" aria-label="Programme summary">
        <a href="#exceptions" className={exceptions.length ? "alert" : ""}><strong>{exceptions.length}</strong><span>Tests needing attention</span></a>
        <a href="#upcoming"><strong>{p.upcoming.length}</strong><span>Starting in {horizon} days</span></a>
        <a href="#acks" className={p.acknowledgements.overdue ? "alert" : ""}><strong>{p.acknowledgements.overdue}</strong><span>Acknowledgements overdue</span></a>
        <a href="#load" className={p.overdue_activities ? "alert" : ""}><strong>{p.overdue_activities}</strong><span>Activities overdue</span></a>
      </nav>

      <div className="grid-2">
        <div className="stack">
          <div id="exceptions">
            <Panel title="Needs attention" flush>
              {!exceptions.length && <p className="panel-empty">No exceptions in this period.</p>}
              <ul className="rows">{exceptions.map((e: any, i: number) => (
                <li key={e.id + i}>
                  <div className="grow">
                    <Link href={`/tests/${e.id}?tab=readiness`} style={{ fontWeight: 600 }}>{e.title}</Link>
                    <div className="small" style={{ color: "var(--blocked)" }}>{e.why}</div>
                  </div>
                  <span className="code">{e.event_code}</span>
                </li>
              ))}</ul>
            </Panel>
          </div>
          <div id="upcoming">
            <Panel title="Coming up" flush>
              {!p.upcoming.length && <p className="panel-empty">Nothing scheduled in this period.</p>}
              <table className="table">
                <tbody>{p.upcoming.map((e: any) => (
                  <tr key={e.id}>
                    <td><Link href={`/tests/${e.id}`} className="title" style={{ fontSize: "var(--t-sm)" }}>{e.title}</Link><div className="sub code">{e.event_code}</div></td>
                    <td>{fmtDateTime(e.planned_start_at)}<div className="sub">{relative(e.planned_start_at)}</div></td>
                    <td>{e.criticality}</td>
                    <td><Pill value={e.lifecycle_status} /></td>
                  </tr>
                ))}</tbody>
              </table>
            </Panel>
          </div>
        </div>
        <div className="stack">
          <Panel title="Programme by stage" meta={`${p.completion.closed} of ${p.completion.planned} closed`} flush>
            <ul className="rows">{ORDER.filter((s) => p.by_status[s]).map((s) => (
              <li key={s} style={{ padding: "8px 18px" }}><span className="grow"><Pill value={s} /></span><strong className="small">{p.by_status[s]}</strong></li>
            ))}</ul>
          </Panel>
          <div id="acks">
            <Panel title="Acknowledgements">
              <p className="small"><strong>{p.acknowledgements.acknowledged}</strong> acknowledged, <strong>{p.acknowledgements.pending}</strong> pending
                {p.acknowledgements.overdue ? <>, <span className="overdue">{p.acknowledgements.overdue} past their deadline</span></> : null}.</p>
            </Panel>
          </div>
          <Panel title="Open findings" flush>
            {!Object.keys(p.open_findings_by_severity).length && <p className="panel-empty">No open findings.</p>}
            <ul className="rows">{sev.filter((s) => p.open_findings_by_severity[s]).map((s) => (
              <li key={s} style={{ padding: "8px 18px" }}><span className="grow small">{s}</span><strong className="small">{p.open_findings_by_severity[s]}</strong></li>
            ))}</ul>
          </Panel>
          <div id="load">
            <Panel title="Open activities by person" flush>
              <ul className="rows">{p.workload.map((w: any) => (
                <li key={w.display_name} style={{ padding: "8px 18px" }}><span className="grow small">{w.display_name}</span><strong className="small">{w.open_activities}</strong></li>
              ))}</ul>
            </Panel>
          </div>
        </div>
      </div>
    </>
  );
}
