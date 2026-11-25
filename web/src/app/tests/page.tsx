"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { ErrorNotice, Loading, Pill } from "@/components/ui";
import { useApi } from "@/lib/api";
import { fmtDateTime, relative, tzLabel } from "@/lib/format";

const FILTERS = [
  { key: "", label: "All" },
  { key: "active", label: "In preparation" },
  { key: "Live", label: "Live" },
  { key: "Closure", label: "In closure" },
  { key: "Closed", label: "Closed" },
];
const ACTIVE = ["Draft", "Planned", "Mobilising", "Readiness", "Ready"];

export default function Tests() {
  const router = useRouter();
  const [filter, setFilter] = useState("");
  const [q, setQ] = useState("");
  const { data, error } = useApi<any[]>(`/test-events${q ? `?q=${encodeURIComponent(q)}` : ""}`);
  const { data: me } = useApi<any>("/me");
  const canCreate = me?.roles?.some((r: string) => ["DRProgrammeManager", "TestManager"].includes(r));

  const rows = (data ?? []).filter((e) => !filter || (filter === "active" ? ACTIVE.includes(e.lifecycle_status) : e.lifecycle_status === filter));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Tests</h1>
          <p>Every resilience test you can see in the programme, in schedule order.</p>
        </div>
        <div className="btn-row" style={{ alignItems: "center" }}>
          <input className="input" style={{ width: 260 }} placeholder="Search by name or code" aria-label="Search tests"
            value={q} onChange={(e) => setQ(e.target.value)} />
          {canCreate && <Link className="btn primary" href="/tests/new">New test</Link>}
        </div>
      </div>
      <div className="tabs" role="tablist">
        {FILTERS.map((f) => (
          <a key={f.key} href="#" role="tab" aria-current={filter === f.key ? "page" : undefined}
            onClick={(e) => { e.preventDefault(); setFilter(f.key); }}>
            {f.label}
            <span className="muted" style={{ marginLeft: 6, fontWeight: 500 }}>
              {(data ?? []).filter((e) => !f.key || (f.key === "active" ? ACTIVE.includes(e.lifecycle_status) : e.lifecycle_status === f.key)).length}
            </span>
          </a>
        ))}
      </div>
      <ErrorNotice error={error} />
      {!data ? <Loading what="Loading tests" /> : (
        <section className="panel">
          <table className="table">
            <thead>
              <tr><th>Test</th><th>Window</th><th>Category</th><th>Criticality</th><th>Status</th></tr>
            </thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id} className="link-row" onClick={() => router.push(`/tests/${e.id}`)}>
                  <td>
                    <a href={`/tests/${e.id}`} className="title" style={{ textDecoration: "none" }}
                      onClick={(ev) => { ev.preventDefault(); router.push(`/tests/${e.id}`); }}>{e.title}</a>
                    <div className="sub code">{e.event_code}</div>
                  </td>
                  <td>
                    {fmtDateTime(e.planned_start_at, e.timezone)} {tzLabel(e.timezone)}
                    <div className="sub">{relative(e.planned_start_at)}</div>
                  </td>
                  <td>{e.category}</td>
                  <td>{e.criticality}</td>
                  <td><Pill value={e.lifecycle_status} />{e.outcome && <div className="sub">{e.outcome}</div>}</td>
                </tr>
              ))}
              {!rows.length && <tr><td colSpan={5} className="muted">No tests match.</td></tr>}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}
