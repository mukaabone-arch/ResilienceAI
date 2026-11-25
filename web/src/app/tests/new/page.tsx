"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { ErrorNotice, Loading, Panel } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { fmtDateTime, isoToZonedInput, TIMEZONES, zonedToIso } from "@/lib/format";

const CATEGORIES = ["Infrastructure", "Application", "Database", "Cyber", "Business"];
const CRITICALITY = ["Critical", "High", "Medium", "Low"];

export default function NewTest() {
  const router = useRouter();
  const { data: me } = useApi<any>("/me");
  const { data: programmes } = useApi<any[]>("/test-programmes");
  const { data: templates } = useApi<any[]>("/test-templates");
  const { data: users } = useApi<any[]>("/users");
  const { data: services } = useApi<any[]>("/business-services");
  const [idem] = useState(() => (globalThis.crypto?.randomUUID?.() ?? String(Date.now())));

  const [f, setF] = useState({
    programme_id: "", template_id: "", title: "", category: "Application", criticality: "High",
    timezone: "Asia/Kolkata", start: "", end: "", test_manager_user_id: "", owner_user_id: "",
    objectives: "", blackout_override_reason: "",
  });
  const [svc, setSvc] = useState<string[]>([]);
  const [endTouched, setEndTouched] = useState(false);
  const [conflicts, setConflicts] = useState<any>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f, v: string) => setF((x) => ({ ...x, [k]: v }));

  // Sensible defaults once lookups arrive
  useEffect(() => {
    if (programmes?.length && !f.programme_id) set("programme_id", (programmes.find((p) => p.status === "Active") ?? programmes[0]).id);
  }, [programmes]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (me && !f.owner_user_id) set("owner_user_id", me.user_id);
    if (me && !f.test_manager_user_id && me.roles.includes("TestManager")) set("test_manager_user_id", me.user_id);
  }, [me]); // eslint-disable-line react-hooks/exhaustive-deps

  const tpl = templates?.find((t) => t.id === f.template_id);
  // A template fills in the category and, until the end is edited by hand, the duration.
  useEffect(() => {
    if (tpl) set("category", tpl.category);
    if (tpl && f.start && !endTouched) {
      const s = zonedToIso(f.start, f.timezone);
      if (s) set("end", isoToZonedInput(new Date(new Date(s).getTime() + tpl.default_duration * 60_000).toISOString(), f.timezone));
    }
  }, [f.template_id, f.start, f.timezone]); // eslint-disable-line react-hooks/exhaustive-deps

  const startIso = f.start ? zonedToIso(f.start, f.timezone) : null;
  const endIso = f.end ? zonedToIso(f.end, f.timezone) : null;
  const windowError = startIso && endIso && endIso <= startIso ? "The end must be after the start." : null;

  // Live clash check while the window, manager or services change
  useEffect(() => {
    if (!startIso || !endIso || windowError) { setConflicts(null); return; }
    const qs = new URLSearchParams({ start: startIso, end: endIso });
    if (f.test_manager_user_id) qs.set("test_manager_user_id", f.test_manager_user_id);
    svc.forEach((id) => qs.append("business_service_ids", id));
    const h = setTimeout(() => api("GET", `/schedule-check?${qs}`).then(setConflicts).catch(() => setConflicts(null)), 350);
    return () => clearTimeout(h);
  }, [startIso, endIso, f.test_manager_user_id, svc.join(","), windowError]); // eslint-disable-line react-hooks/exhaustive-deps

  const managers = useMemo(() => (users ?? []).filter((u) => u.roles.includes("TestManager") || u.roles.includes("DRProgrammeManager")), [users]);
  const isPM = me?.roles?.includes("DRProgrammeManager");
  const canCreate = me?.roles?.some((r: string) => ["DRProgrammeManager", "TestManager"].includes(r));
  const inBlackout = conflicts?.blackouts?.length > 0;
  const ready = f.programme_id && f.title.trim() && startIso && endIso && !windowError
    && (!inBlackout || (isPM && f.blackout_override_reason.trim()));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!ready) return;
    setBusy(true); setError(null);
    try {
      const ev = await api("POST", "/test-events", {
        programme_id: f.programme_id, template_id: f.template_id || undefined, title: f.title.trim(),
        category: f.category, criticality: f.criticality, timezone: f.timezone,
        planned_start_at: startIso, planned_end_at: endIso,
        test_manager_user_id: f.test_manager_user_id || undefined, owner_user_id: f.owner_user_id || undefined,
        objectives: f.objectives || undefined, business_service_ids: svc,
        blackout_override_reason: inBlackout ? f.blackout_override_reason : undefined,
      }, { "Idempotency-Key": idem });
      router.push(`/tests/${ev.id}`);
    } catch (err) {
      setError(err); setBusy(false);
    }
  }

  if (!me || !programmes || !users) return <Loading what="Loading" />;
  if (!canCreate) return <ErrorNotice error={{ message: "Only test managers and programme managers can register tests." }} />;

  const clashCount = conflicts ? conflicts.shared_service_overlaps.length + conflicts.test_manager_overlaps.length : 0;
  return (
    <>
      <Link href="/calendar" className="small muted">Calendar</Link>
      <div className="page-head" style={{ marginTop: 10 }}>
        <div>
          <h1>New test</h1>
          <p>Register the test in the programme. It starts as a draft; people, activities and readiness conditions come next.</p>
        </div>
      </div>
      <form className="grid-2" onSubmit={submit}>
        <div className="stack">
          <ErrorNotice error={error} />
          <Panel title="What is being tested">
            <div className="field"><label htmlFor="title">Test name</label>
              <input id="title" className="input" value={f.title} onChange={(e) => set("title", e.target.value)} placeholder="e.g. Core Banking DR Simulation" /></div>
            <div className="form-row">
              <div className="field"><label htmlFor="prog">Programme</label>
                <select id="prog" className="select" value={f.programme_id} onChange={(e) => set("programme_id", e.target.value)}>
                  {programmes.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select></div>
              <div className="field"><label htmlFor="tpl">Start from a template (optional)</label>
                <select id="tpl" className="select" value={f.template_id} onChange={(e) => set("template_id", e.target.value)}>
                  <option value="">No template</option>
                  {(templates ?? []).map((t) => <option key={t.id} value={t.id}>{t.name} (v{t.version})</option>)}
                </select>
                {tpl && <span className="hint">Adds the template&apos;s activities, dependencies, readiness conditions and milestones.</span>}</div>
            </div>
            <div className="form-row">
              <div className="field"><label htmlFor="cat">Category</label>
                <select id="cat" className="select" value={f.category} onChange={(e) => set("category", e.target.value)}>
                  {CATEGORIES.map((c) => <option key={c}>{c}</option>)}
                </select></div>
              <div className="field"><label htmlFor="crit">Criticality</label>
                <select id="crit" className="select" value={f.criticality} onChange={(e) => set("criticality", e.target.value)}>
                  {CRITICALITY.map((c) => <option key={c}>{c}</option>)}
                </select>
                {f.criticality === "Critical" && <span className="hint">Critical tests need the programme manager for go and closure decisions.</span>}</div>
            </div>
            <fieldset className="field" style={{ border: 0, padding: 0, margin: "0 0 14px" }}>
              <legend style={{ fontSize: "var(--t-sm)", fontWeight: 600, marginBottom: 6 }}>Business services in scope</legend>
              <div className="checks">
                {(services ?? []).map((s) => (
                  <label key={s.id}><input type="checkbox" checked={svc.includes(s.id)}
                    onChange={(e) => setSvc(e.target.checked ? [...svc, s.id] : svc.filter((x) => x !== s.id))} /> {s.name}</label>
                ))}
              </div>
            </fieldset>
            <div className="field"><label htmlFor="obj">Objectives (optional)</label>
              <textarea id="obj" className="textarea" value={f.objectives} onChange={(e) => set("objectives", e.target.value)}
                placeholder="What this test needs to prove, e.g. recover core banking at the DR site within the 4-hour RTO" /></div>
          </Panel>

          <Panel title="When">
            <div className="form-row">
              <div className="field"><label htmlFor="start">Starts</label>
                <input id="start" type="datetime-local" className="input" value={f.start} onChange={(e) => set("start", e.target.value)} /></div>
              <div className="field"><label htmlFor="end">Ends</label>
                <input id="end" type="datetime-local" className="input" value={f.end}
                  onChange={(e) => { setEndTouched(true); set("end", e.target.value); }} />
                {tpl && !endTouched && <span className="hint">Set from the template ({Math.round(tpl.default_duration / 60)} h). You can change it.</span>}</div>
            </div>
            <div className="field" style={{ maxWidth: 320 }}><label htmlFor="tz">Time zone of the test</label>
              <select id="tz" className="select" value={f.timezone} onChange={(e) => set("timezone", e.target.value)}>
                {TIMEZONES.map((z) => <option key={z.value} value={z.value}>{z.label}</option>)}
              </select>
              <span className="hint">The times above are read in this zone. Everyone sees them converted to their own.</span></div>
            {windowError && <p className="small overdue">{windowError}</p>}
          </Panel>

          <Panel title="Who is accountable">
            <div className="form-row">
              <div className="field"><label htmlFor="tm">Test manager</label>
                <select id="tm" className="select" value={f.test_manager_user_id} onChange={(e) => set("test_manager_user_id", e.target.value)}>
                  <option value="">Choose later</option>
                  {managers.map((u) => <option key={u.id} value={u.id}>{u.display_name}</option>)}
                </select></div>
              <div className="field"><label htmlFor="own">Test owner</label>
                <select id="own" className="select" value={f.owner_user_id} onChange={(e) => set("owner_user_id", e.target.value)}>
                  {(users ?? []).filter((u) => !u.is_external).map((u) => <option key={u.id} value={u.id}>{u.display_name}</option>)}
                </select>
                <span className="hint">The owner is accountable for the result and can approve go for non-critical tests.</span></div>
            </div>
          </Panel>
        </div>

        <div className="stack" style={{ position: "sticky", top: 20 }}>
          <Panel title="Schedule check">
            {!conflicts ? <p className="small muted">Pick a start and end to check for clashes.</p> : (
              <>
                {!inBlackout && !clashCount && <p className="small" style={{ color: "var(--ready)", fontWeight: 600 }}>No clashes in this window.</p>}
                {inBlackout && (
                  <div className="notice error" style={{ marginBottom: 12 }}>
                    This window falls in a blackout period:
                    <ul>{conflicts.blackouts.map((b: any) => <li key={b.id}>{b.name} ({fmtDateTime(b.starts_at)} to {fmtDateTime(b.ends_at)})</li>)}</ul>
                  </div>
                )}
                {inBlackout && (isPM ? (
                  <div className="field"><label htmlFor="ovr">Reason for scheduling inside the blackout</label>
                    <textarea id="ovr" className="textarea" value={f.blackout_override_reason}
                      onChange={(e) => set("blackout_override_reason", e.target.value)} />
                    <span className="hint">Recorded as your approved override in the audit trail.</span></div>
                ) : <p className="small muted" style={{ marginBottom: 12 }}>Only a DR programme manager can schedule inside a blackout. Choose another window or ask them.</p>)}
                {conflicts.shared_service_overlaps.map((x: any) => (
                  <p key={"s" + x.id} className="small" style={{ marginBottom: 6 }}>Overlaps <Link href={`/tests/${x.id}`}>{x.event_code}</Link> ({x.title}), which tests the same service.</p>
                ))}
                {conflicts.test_manager_overlaps.map((x: any) => (
                  <p key={"m" + x.id} className="small" style={{ marginBottom: 6 }}>The test manager is also running <Link href={`/tests/${x.id}`}>{x.event_code}</Link> ({x.title}) at this time.</p>
                ))}
                {clashCount > 0 && <p className="xs muted" style={{ marginTop: 8 }}>These clashes don&apos;t block registration, but they will be flagged on the calendar.</p>}
              </>
            )}
          </Panel>
          <div className="btn-row">
            <button type="submit" className="btn primary" disabled={!ready || busy}>{busy ? "Registering…" : "Register test"}</button>
            <Link href="/calendar" className="btn">Cancel</Link>
          </div>
          {!ready && !busy && <p className="xs muted">
            {!f.title.trim() ? "Add a test name." : !startIso || !endIso ? "Add the start and end." : windowError ? windowError
              : inBlackout && !isPM ? "Choose a window outside the blackout." : inBlackout ? "Give a reason for the blackout override." : ""}</p>}
        </div>
      </form>
    </>
  );
}
