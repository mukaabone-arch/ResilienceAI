"use client";

import { useApi } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { Panel } from "./ui";

/** The same report monitoring tools get from /health/deep, for administrators, risk and audit. */
type Check = { check: string; status: "ok" | "warn" | "fail"; message: string };
const NAMES: Record<string, string> = {
  database: "Database", evidence_store: "Evidence store", worker: "Background worker", notifications: "Notifications",
  malware_scanning: "Malware scanning", audit_chain: "Audit trail", break_glass: "Break-glass", ai: "AI features",
};
const SUMMARY = { ok: "All systems working", warn: "Working, with something to look at", fail: "Needs attention now" };

export function SystemHealth() {
  const { data, error } = useApi<{ status: "ok" | "warn" | "fail"; checked_at: string; checks: Check[] }>("/ops/status");
  if (error || !data) return null;           // only administrators, risk and audit can see it
  return (
    <Panel title="System health" meta={`Checked ${fmtDateTime(data.checked_at)}`}>
      <p className={`health-sum ${data.status}`}>{SUMMARY[data.status]}</p>
      <ul className="health">
        {data.checks.map((c) => (
          <li key={c.check} className={c.status}>
            <span className="health-dot" aria-hidden="true" />
            <span className="health-name">{NAMES[c.check] ?? c.check}</span>
            <span className="health-msg">{c.message}</span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}
