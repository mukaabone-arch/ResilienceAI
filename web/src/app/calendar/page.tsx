"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { ErrorNotice, Loading, Panel } from "@/components/ui";
import { useApi } from "@/lib/api";
import { browserTz, fmtDateTime, fmtTime, tzLabel } from "@/lib/format";

type View = "year" | "month" | "week";
const DAY = 86_400_000;
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const CONFLICT_TEXT: Record<string, string> = {
  blackout: "In the blackout",
  shared_service: "Tests the same service as",
  same_test_manager: "Same test manager as",
};

// Local-calendar helpers (the calendar is laid out in the viewer's own time zone).
const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const addDays = (d: Date, n: number) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const startOfWeek = (d: Date) => addDays(startOfDay(d), -((d.getDay() + 6) % 7));
const sameDay = (a: Date, b: Date) => a.toDateString() === b.toDateString();
const dayKey = (d: Date) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;

function windowFor(view: View, anchor: Date): [Date, Date] {
  if (view === "year") return [new Date(anchor.getFullYear(), 0, 1), new Date(anchor.getFullYear() + 1, 0, 1)];
  if (view === "week") { const s = startOfWeek(anchor); return [s, addDays(s, 7)]; }
  const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const s = startOfWeek(first);
  const last = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0);
  return [s, addDays(startOfWeek(last), 7)];
}

function shift(view: View, anchor: Date, dir: number): Date {
  if (view === "year") return new Date(anchor.getFullYear() + dir, anchor.getMonth(), 1);
  if (view === "week") return addDays(anchor, 7 * dir);
  return new Date(anchor.getFullYear(), anchor.getMonth() + dir, 1);
}

function tone(status: string) {
  return ({ Live: "live", Paused: "blocked", Ready: "ready", Readiness: "waiting", Mobilising: "waiting",
    Closure: "idle", Closed: "done", Cancelled: "cancelled" } as Record<string, string>)[status] ?? "idle";
}

export default function CalendarPage() {
  const router = useRouter();
  const [view, setView] = useState<View>("month");
  const [anchor, setAnchor] = useState(() => startOfDay(new Date()));
  const [category, setCategory] = useState("");
  const [criticality, setCriticality] = useState("");
  const [showCancelled, setShowCancelled] = useState(false);
  const { data: me } = useApi<any>("/me");
  const [from, to] = windowFor(view, anchor);
  const qs = new URLSearchParams({ start: from.toISOString(), end: to.toISOString() });
  if (category) qs.set("category", category);
  if (criticality) qs.set("criticality", criticality);
  const { data, error } = useApi<any>(`/calendar?${qs}`);

  const events = useMemo(() => (data?.events ?? []).filter((e: any) => showCancelled || e.lifecycle_status !== "Cancelled"), [data, showCancelled]);
  const byDay = useMemo(() => {
    const m = new Map<string, any[]>();
    for (const e of events) {
      const s = startOfDay(new Date(e.planned_start_at)), end = new Date(e.planned_end_at);
      for (let d = s; d < end; d = addDays(d, 1)) m.set(dayKey(d), [...(m.get(dayKey(d)) ?? []), e]);
    }
    return m;
  }, [events]);
  const blackoutOn = (d: Date) => (data?.blackouts ?? []).find((b: any) => new Date(b.starts_at) < addDays(d, 1) && new Date(b.ends_at) > d);
  const conflicted = events.filter((e: any) => e.conflicts.length);
  const canCreate = me?.roles?.some((r: string) => ["DRProgrammeManager", "TestManager"].includes(r));
  const today = startOfDay(new Date());

  const title = view === "year" ? `${anchor.getFullYear()}`
    : view === "month" ? `${MONTHS[anchor.getMonth()]} ${anchor.getFullYear()}`
    : `${from.getDate()} ${MONTHS[from.getMonth()].slice(0, 3)} to ${addDays(to, -1).getDate()} ${MONTHS[addDays(to, -1).getMonth()].slice(0, 3)} ${addDays(to, -1).getFullYear()}`;

  const chip = (e: any, withTime = false) => (
    <Link key={e.id} href={`/tests/${e.id}`} className={`cal-chip ${tone(e.lifecycle_status)} ${e.conflicts.length ? "clash" : ""}`}
      title={`${e.event_code} ${e.title}\n${e.lifecycle_status}, ${e.criticality}\n${fmtDateTime(e.planned_start_at)} to ${fmtDateTime(e.planned_end_at)}`
        + (e.conflicts.length ? `\nClashes with: ${e.conflicts.map((c: any) => c.title ?? c.with).join(", ")}` : "")}>
      {e.conflicts.length > 0 && <span className="clash-mark" aria-label="Has a scheduling conflict">!</span>}
      {withTime && <span className="t">{fmtTime(e.planned_start_at)}</span>}
      <span className="n">{e.title}</span>
    </Link>
  );

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Calendar</h1>
          <p>Every test you can see, laid out by date. Times are in your time zone ({tzLabel(browserTz())}).
            Tests marked <span className="clash-mark static">!</span> clash with a blackout, a shared service or the same test manager.</p>
        </div>
        {canCreate && <Link className="btn primary" href="/tests/new">New test</Link>}
      </div>

      <div className="cal-bar">
        <div className="seg" role="group" aria-label="View">
          {(["year", "month", "week"] as View[]).map((v) => (
            <button key={v} type="button" aria-pressed={view === v} onClick={() => setView(v)}>{v[0].toUpperCase() + v.slice(1)}</button>
          ))}
        </div>
        <div className="btn-row" style={{ alignItems: "center" }}>
          <button className="btn" aria-label="Previous" onClick={() => setAnchor(shift(view, anchor, -1))}>‹</button>
          <button className="btn" onClick={() => setAnchor(startOfDay(new Date()))}>Today</button>
          <button className="btn" aria-label="Next" onClick={() => setAnchor(shift(view, anchor, 1))}>›</button>
          <h2 style={{ marginLeft: 8, minWidth: 220 }}>{title}</h2>
        </div>
        <div className="btn-row" style={{ alignItems: "center", marginLeft: "auto" }}>
          <select className="select" style={{ width: 160 }} value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Category">
            <option value="">All categories</option>
            {["Infrastructure", "Application", "Database", "Cyber", "Business"].map((c) => <option key={c}>{c}</option>)}
          </select>
          <select className="select" style={{ width: 150 }} value={criticality} onChange={(e) => setCriticality(e.target.value)} aria-label="Criticality">
            <option value="">All criticality</option>
            {["Critical", "High", "Medium", "Low"].map((c) => <option key={c}>{c}</option>)}
          </select>
          <label className="small" style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input type="checkbox" checked={showCancelled} onChange={(e) => setShowCancelled(e.target.checked)} /> Cancelled
          </label>
        </div>
      </div>

      <ErrorNotice error={error} />
      {!data ? <Loading what="Loading the calendar" /> : (
        <div className="grid-cal">
          <div>
            {view === "year" && (
              <div className="year">
                {MONTHS.map((name, mi) => {
                  const first = new Date(anchor.getFullYear(), mi, 1);
                  const start = startOfWeek(first);
                  const days = Array.from({ length: 42 }, (_, i) => addDays(start, i));
                  const count = events.filter((e: any) => new Date(e.planned_start_at).getMonth() === mi
                    && new Date(e.planned_start_at).getFullYear() === anchor.getFullYear()).length;
                  return (
                    <button key={name} type="button" className="mini" onClick={() => { setAnchor(first); setView("month"); }}
                      aria-label={`${name}: ${count} test${count === 1 ? "" : "s"}. Open month`}>
                      <div className="mini-head"><strong>{name}</strong><span className="muted xs">{count ? `${count} test${count > 1 ? "s" : ""}` : ""}</span></div>
                      <div className="mini-grid">
                        {days.map((d) => {
                          const evs = byDay.get(dayKey(d)) ?? [];
                          const out = d.getMonth() !== mi;
                          return (
                            <span key={d.toISOString()} className={`md ${out ? "out" : ""} ${!out && blackoutOn(d) ? "bo" : ""} ${sameDay(d, today) ? "is-today" : ""}`}>
                              {d.getDate()}
                              {!out && evs.length > 0 && <i className={`dot ${evs.some((e) => e.conflicts.length) ? "clash" : tone(evs[0].lifecycle_status)}`} />}
                            </span>
                          );
                        })}
                      </div>
                    </button>
                  );
                })}
              </div>
            )}

            {view !== "year" && (
              <div className={`month ${view}`}>
                {WEEKDAYS.map((w) => <div key={w} className="wd">{w}</div>)}
                {Array.from({ length: Math.round((to.getTime() - from.getTime()) / DAY) }, (_, i) => addDays(from, i)).map((d) => {
                  const evs = byDay.get(dayKey(d)) ?? [];
                  const bo = blackoutOn(d);
                  const out = view === "month" && d.getMonth() !== anchor.getMonth();
                  const max = view === "week" ? 20 : 3;
                  return (
                    <div key={d.toISOString()} className={`day ${out ? "out" : ""} ${bo ? "bo" : ""} ${sameDay(d, today) ? "is-today" : ""}`}>
                      <div className="dn">
                        <span className="num">{d.getDate()}</span>
                        {bo && (d.getDay() === 1 || sameDay(d, startOfDay(new Date(bo.starts_at))) || sameDay(d, from)) &&
                          <span className="bo-label" title={bo.name}>{bo.name}</span>}
                      </div>
                      <div className="chips">
                        {evs.slice(0, max).map((e) => chip(e, view === "week"))}
                        {evs.length > max && (
                          <button type="button" className="more" onClick={() => { setAnchor(d); setView("week"); }}>{evs.length - max} more</button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
            <div className="legend small muted">
              <span><i className="sw live" />Live</span><span><i className="sw ready" />Ready</span>
              <span><i className="sw waiting" />Preparing</span><span><i className="sw idle" />Draft or planned</span>
              <span><i className="sw done" />Closed</span><span><i className="sw bo" />Blackout</span>
            </div>
          </div>

          <div className="stack">
            <Panel title="Clashes in view" meta={conflicted.length ? `${conflicted.length} test${conflicted.length > 1 ? "s" : ""}` : ""} flush>
              {!conflicted.length && <p className="panel-empty">No clashes in this period.</p>}
              <ul className="rows">
                {conflicted.map((e: any) => (
                  <li key={e.id} style={{ flexDirection: "column", gap: 4 }}>
                    <Link href={`/tests/${e.id}`} style={{ fontWeight: 600 }}>{e.title}</Link>
                    {e.conflicts.map((c: any, i: number) => (
                      <span key={i} className="small" style={{ color: "var(--blocked)" }}>
                        {CONFLICT_TEXT[c.kind] ?? "Clashes with"}{" "}
                        {c.event_id ? <a href={`/tests/${c.event_id}`} onClick={(ev) => { ev.preventDefault(); router.push(`/tests/${c.event_id}`); }}>{c.title ?? c.with}</a> : `“${c.with}”`}
                      </span>
                    ))}
                  </li>
                ))}
              </ul>
            </Panel>
            <Panel title="Blackout periods" flush>
              {!data.blackouts.length && <p className="panel-empty">None in this period.</p>}
              <ul className="rows">
                {data.blackouts.map((b: any) => (
                  <li key={b.id} style={{ flexDirection: "column", gap: 2 }}>
                    <strong className="small">{b.name}</strong>
                    <span className="xs muted">{fmtDateTime(b.starts_at)} to {fmtDateTime(b.ends_at)}</span>
                  </li>
                ))}
              </ul>
            </Panel>
          </div>
        </div>
      )}
    </>
  );
}
