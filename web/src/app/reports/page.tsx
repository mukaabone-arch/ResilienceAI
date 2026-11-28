"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { OversightLog } from "@/components/Assistant";
import { SystemHealth } from "@/components/SystemHealth";
import { AgentsPanel } from "@/components/Agents";
import { ErrorNotice, Loading, Panel, Pill, useAction } from "@/components/ui";
import { downloadFile, useApi } from "@/lib/api";
import { TIMEZONES, fmtDateTime } from "@/lib/format";
import { hasRole, useMe } from "@/lib/me";

const PORTFOLIO = ["EnterpriseProgrammeAdministrator", "DRProgrammeManager", "RiskCompliance", "SeniorManagement", "Auditor"];

export default function Reports() {
  const { data: me } = useMe();
  const canPortfolio = hasRole(me, ...PORTFOLIO);
  const { data: programmes } = useApi<any[]>("/test-programmes");
  const { data: tests, error } = useApi<any[]>("/test-events");
  const [programme, setProgramme] = useState("");
  const [tz, setTz] = useState("Asia/Kolkata");
  const { run, busy, feedback } = useAction();
  useEffect(() => {
    if (programmes?.length && !programme) setProgramme((programmes.find((p) => p.status === "Active") ?? programmes[0]).id);
  }, [programmes]); // eslint-disable-line react-hooks/exhaustive-deps

  const closing = (tests ?? []).filter((t) => ["Closure", "Closed"].includes(t.lifecycle_status))
    .sort((a, b) => (b.actual_end_at ?? b.planned_end_at).localeCompare(a.actual_end_at ?? a.planned_end_at));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Reports</h1>
          <p>Downloads for management packs and audit. They are built from the same records as the screens, and each download is logged.</p>
        </div>
      </div>
      {feedback}
      <ErrorNotice error={error} />
      <div className="grid-2">
        <div className="stack">
          <Panel title="Closure reports" meta="PDF" flush>
            {!tests ? <div className="panel-body"><Loading /></div> : !closing.length ? (
              <p className="panel-empty">No tests have reached closure yet. A test&apos;s closure report becomes available once coordination is complete.</p>
            ) : (
              <table className="table">
                <thead><tr><th>Test</th><th>Finished</th><th>Status</th><th></th></tr></thead>
                <tbody>
                  {closing.map((t) => (
                    <tr key={t.id}>
                      <td><Link href={`/tests/${t.id}?tab=closure`} className="title" style={{ fontSize: "var(--t-sm)" }}>{t.title}</Link>
                        <div className="sub code">{t.event_code}</div></td>
                      <td className="small">{fmtDateTime(t.actual_end_at ?? t.planned_end_at, t.timezone)}</td>
                      <td><Pill value={t.lifecycle_status} />{t.outcome && <div className="sub">{t.outcome}</div>}</td>
                      <td><button className="btn" disabled={busy}
                        onClick={() => run(() => downloadFile(`/test-events/${t.id}/closure-report.pdf`, `${t.event_code}.pdf`))}>
                        {t.lifecycle_status === "Closed" ? "Download PDF" : "Download draft"}</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
          {hasRole(me, "EnterpriseProgrammeAdministrator", "RiskCompliance", "Auditor") && <SystemHealth />}
          {me && !me.is_external && <AgentsPanel />}
          {hasRole(me, "EnterpriseProgrammeAdministrator", "DRProgrammeManager", "RiskCompliance", "Auditor") && <OversightLog />}
        </div>
        <div className="stack">
          <Panel title="Programme status" meta="Excel">
            {!canPortfolio ? (
              <p className="small muted">The programme workbook is for programme, risk, audit and senior management roles.</p>
            ) : (
              <>
                <p className="small" style={{ marginBottom: 14 }}>One workbook with six sheets: summary, every test with its readiness,
                  exceptions (unmet readiness, delays, overdue work), findings and remediation, decisions, and schedule changes.</p>
                <div className="field"><label htmlFor="prog">Programme</label>
                  <select id="prog" className="select" value={programme} onChange={(e) => setProgramme(e.target.value)}>
                    {(programmes ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select></div>
                <div className="field"><label htmlFor="tz">Show times in</label>
                  <select id="tz" className="select" value={tz} onChange={(e) => setTz(e.target.value)}>
                    {TIMEZONES.map((z) => <option key={z.value} value={z.value}>{z.label}</option>)}
                  </select></div>
                <button className="btn primary" disabled={busy || !programme}
                  onClick={() => run(() => downloadFile(`/reports/programme-status.xlsx?programme_id=${programme}&tz=${encodeURIComponent(tz)}`, "programme-status.xlsx"))}>
                  Download workbook</button>
              </>
            )}
          </Panel>
          <Panel title="About these reports">
            <p className="small muted">A closed test&apos;s PDF is built from the record frozen at closure, so it won&apos;t change later. Until a test is closed, the PDF is marked DRAFT and lists what is still outstanding.</p>
          </Panel>
        </div>
      </div>
    </>
  );
}
