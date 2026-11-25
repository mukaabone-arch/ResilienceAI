"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { api, useApi } from "@/lib/api";
import { roleName } from "@/lib/format";
import { MeContext } from "@/lib/me";
import { getSession, Session, setSession } from "@/lib/session";

export function BrandMark({ className = "brand-mark" }: { className?: string }) {
  // Two posts and a crossbar: a gate, the platform's central idea.
  return (
    <svg className={className} viewBox="0 0 26 26" aria-hidden="true">
      <rect x="3" y="3" width="4" height="20" rx="1" fill="#fff" />
      <rect x="19" y="3" width="4" height="20" rx="1" fill="#fff" />
      <rect x="7" y="9" width="12" height="3" fill="#7fc2a0" />
      <rect x="7" y="15" width="12" height="3" fill="#7fc2a0" opacity="0.55" />
    </svg>
  );
}

const PORTFOLIO_ROLES = ["EnterpriseProgrammeAdministrator", "DRProgrammeManager", "RiskCompliance", "SeniorManagement", "Auditor"];

export function AppFrame({ children }: { children: ReactNode }) {
  const [session, setS] = useState<Session | null | undefined>(undefined);
  useEffect(() => {
    const sync = () => setS(getSession());
    sync();
    window.addEventListener("resilienceai-session", sync);
    return () => window.removeEventListener("resilienceai-session", sync);
  }, []);
  if (session === undefined) return null;
  if (!session) return <SignIn />;
  return <Shell key={session.subject + session.tenantId}>{children}</Shell>;
}

/** Small-screen helpers that work for every page without per-table markup:
 *  - label each table cell with its column name, so the phone layout can show rows as labelled cards;
 *  - bring the current tab and lifecycle stage into view inside their own horizontal scroll. */
function useSmallScreenHelpers() {
  useEffect(() => {
    const root = document.querySelector("main");
    if (!root) return;
    let frame = 0;
    const apply = () => {
      frame = 0;
      root.querySelectorAll<HTMLTableElement>("table.table").forEach((table) => {
        const heads = [...table.querySelectorAll("thead th")].map((th) => th.textContent?.trim() ?? "");
        table.querySelectorAll("tbody tr").forEach((tr) => {
          [...tr.children].forEach((td, i) => {
            const label = heads[i] ?? "";
            if ((td as HTMLElement).dataset.label !== label) (td as HTMLElement).dataset.label = label;
          });
        });
      });
      root.querySelectorAll<HTMLElement>(".tabs [aria-current], .track [aria-current]").forEach((el) => {
        const strip = el.parentElement;
        if (!strip || strip.scrollWidth <= strip.clientWidth || el.dataset.shown === "1") return;
        el.dataset.shown = "1";
        strip.scrollLeft = el.offsetLeft - (strip.clientWidth - el.offsetWidth) / 2;
      });
    };
    const observer = new MutationObserver(() => { if (!frame) frame = requestAnimationFrame(apply); });
    observer.observe(root, { childList: true, subtree: true });
    apply();
    return () => { observer.disconnect(); if (frame) cancelAnimationFrame(frame); };
  }, []);
}

function Shell({ children }: { children: ReactNode }) {
  const path = usePathname();
  useSmallScreenHelpers();
  const { data: me, error } = useApi<any>("/me");
  const { data: ws } = useApi<any>("/me/workspace");
  useEffect(() => {
    if (error?.status === 403 || error?.status === 401) setSession(null);
  }, [error]);
  const due = ws ? ws.overdue.length + ws.pending_acknowledgements.length : 0;
  const isPortfolio = me?.roles?.some((r: string) => PORTFOLIO_ROLES.includes(r));
  const nav = [
    { href: "/workspace", label: "My work", count: due },
    { href: "/tests", label: "Tests" },
    { href: "/calendar", label: "Calendar" },
    { href: "/reports", label: "Reports" },
    ...(isPortfolio ? [{ href: "/command", label: "Command centre" }] : []),
  ];
  return (
    <div className="shell">
      <aside className="rail">
        <Link href="/workspace" className="brand">
          <BrandMark />
          <span className="brand-name">ResilienceAI<span className="brand-sub">Test coordination</span></span>
        </Link>
        <nav className="nav" aria-label="Main">
          {nav.map((n) => (
            <Link key={n.href} href={n.href} aria-current={path.startsWith(n.href) && !(n.href === "/tests" && path === "/tests/new") ? "page" : undefined}>
              {n.label}
              {n.count ? <span className="count" aria-label={`${n.count} need attention`}>{n.count}</span> : null}
            </Link>
          ))}
        </nav>
        <div className="rail-foot">
          {me && (
            <>
              <div className="who">{me.display_name}</div>
              <div className="role">{me.roles.map(roleName).join(", ")}</div>
            </>
          )}
          <button type="button" onClick={() => setSession(null)}>Switch person</button>
        </div>
      </aside>
      <main className="main">
        {/* Pages render only once the person is known, and all read the same identity. */}
        {me ? <MeContext.Provider value={me}>{children}</MeContext.Provider> : <p className="muted small">Loading…</p>}
      </main>
    </div>
  );
}

function SignIn() {
  const [dir, setDir] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tenant, setTenant] = useState<string>("");
  useEffect(() => {
    api("GET", "/dev/directory")
      .then((d) => { setDir(d); setTenant(d.tenants[0]?.id ?? ""); })
      .catch(() => setErr("Can't reach the ResilienceAI API. Start it with: uvicorn app.main:app (in test-coordination/)."));
  }, []);
  const people = dir?.users.filter((u: any) => u.tenant_id === tenant) ?? [];
  return (
    <div className="signin">
      <section className="signin-side">
        <div className="brand"><BrandMark /><span className="brand-name">ResilienceAI</span></div>
        <div>
          <h1>Every resilience test, one place to coordinate it.</h1>
          <p>Plan the annual programme, mobilise the people, hold the go decision to its readiness conditions,
            and keep the evidence auditors will ask for.</p>
        </div>
        <p className="xs">Development sign-in. Production uses your organisation&apos;s single sign-on.</p>
      </section>
      <section className="signin-main">
        <h2>Choose who to act as</h2>
        <p className="muted small" style={{ marginTop: 6 }}>Each person sees only the tests and actions their role allows.</p>
        {err && <div className="notice error" style={{ marginTop: 16 }}>{err}</div>}
        {dir && dir.tenants.length > 1 && (
          <div className="field" style={{ marginTop: 16, maxWidth: 320 }}>
            <label htmlFor="tenant">Organisation</label>
            <select id="tenant" className="select" value={tenant} onChange={(e) => setTenant(e.target.value)}>
              {dir.tenants.map((t: any) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </div>
        )}
        {dir && !dir.tenants.length && (
          <div className="notice error" style={{ marginTop: 16 }}>No organisations yet. Load demo data with: python -m scripts.demo</div>
        )}
        <ul className="people">
          {people.map((u: any) => (
            <li key={u.idp_subject}>
              <button type="button" onClick={() => setSession({ tenantId: tenant, subject: u.idp_subject, displayName: u.display_name })}>
                <span className="n">{u.display_name}</span>
                <span className="r">{u.roles.map(roleName).join(", ")}</span>
                <span className="r" style={{ display: "block" }}>{u.department}</span>
              </button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
