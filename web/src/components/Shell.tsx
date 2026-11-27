"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import type { AccountInfo } from "@azure/msal-browser";
import { api, useApi } from "@/lib/api";
import { initEntra, loadAuthConfig, signInWithMicrosoft, signOut } from "@/lib/auth";
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

/* Line icons for the sections, 24-unit grid, drawn in the current text colour. */
const ICONS: Record<string, ReactNode> = {
  "/workspace": <><path d="M9 6h11M9 12h11M9 18h11" /><path d="m3.5 6 1.5 1.5L7.5 5M3.5 12l1.5 1.5L7.5 11M3.5 18l1.5 1.5L7.5 17" /></>,
  "/tests": <><path d="M9 3h6M10 3v6.5L4.8 18.2A1.8 1.8 0 0 0 6.3 21h11.4a1.8 1.8 0 0 0 1.5-2.8L14 9.5V3" /><path d="M7.5 15h9" /></>,
  "/calendar": <><rect x="3.5" y="5" width="17" height="15.5" rx="2" /><path d="M3.5 10h17M8 3v4M16 3v4" /></>,
  "/reports": <><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5M9 13h6M9 17h6" /></>,
  "/command": <><path d="M4.5 18a8.5 8.5 0 1 1 15 0" /><path d="m12 13.5 4-4" /><circle cx="12" cy="14" r="1.3" /></>,
};

function NavIcon({ href }: { href: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {ICONS[href]}
    </svg>
  );
}

const PORTFOLIO_ROLES = ["EnterpriseProgrammeAdministrator", "DRProgrammeManager", "RiskCompliance", "SeniorManagement", "Auditor"];

type AuthState =
  | { kind: "loading" }
  | { kind: "unreachable" }
  | { kind: "dev"; session: Session | null }
  | { kind: "sso"; account: AccountInfo | null; error?: string };

function friendlySignInError(e: unknown): string {
  const code = (e as { errorCode?: string })?.errorCode ?? "";
  if (code === "user_cancelled") return "Sign-in was cancelled.";
  if (code === "popup_window_error" || code === "interaction_in_progress") return "A sign-in is already in progress. Try again in a moment.";
  const msg = e instanceof Error ? e.message : String(e);
  return `Microsoft sign-in didn't complete. ${msg.split("\n")[0]}`;
}

export function AppFrame({ children }: { children: ReactNode }) {
  const [auth, setAuth] = useState<AuthState>({ kind: "loading" });
  useEffect(() => {
    let live = true;
    const syncDev = () => live && setAuth({ kind: "dev", session: getSession() });
    loadAuthConfig()
      .then(async (cfg) => {
        if (cfg.mode === "dev") {
          syncDev();
          window.addEventListener("resilienceai-session", syncDev);
          return;
        }
        try {
          const account = await initEntra(cfg);
          if (live) setAuth({ kind: "sso", account });
        } catch (e) {
          if (live) setAuth({ kind: "sso", account: null, error: friendlySignInError(e) });
        }
      })
      .catch(() => live && setAuth({ kind: "unreachable" }));
    return () => { live = false; window.removeEventListener("resilienceai-session", syncDev); };
  }, []);
  if (auth.kind === "loading") return null;
  if (auth.kind === "unreachable") return <SignIn />;   // shows how to start the API
  if (auth.kind === "sso") {
    return auth.account ? <Shell key={auth.account.homeAccountId} sso>{children}</Shell> : <MicrosoftSignIn error={auth.error} />;
  }
  if (!auth.session) return <SignIn />;
  return <Shell key={auth.session.subject + auth.session.tenantId}>{children}</Shell>;
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
            const label = (td as HTMLTableCellElement).colSpan > 1 ? "" : heads[i] ?? "";
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

function Shell({ children, sso = false }: { children: ReactNode; sso?: boolean }) {
  const path = usePathname();
  useSmallScreenHelpers();
  const { data: me, error } = useApi<any>("/me");
  const { data: ws } = useApi<any>(me ? "/me/workspace" : null);
  const denied = error?.status === 403 || error?.status === 401;
  useEffect(() => {
    if (denied && !sso) setSession(null);   // dev: the chosen person no longer exists (e.g. demo data reloaded)
  }, [denied, sso]);
  if (denied && sso) return <AccessProblem message={error!.message} />;
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
              <NavIcon href={n.href} />
              {n.label}
              {n.count ? <span className="count" aria-label={`${n.count} need attention`}>{n.count}</span> : null}
            </Link>
          ))}
        </nav>
        <div className="rail-foot">
          {me && (
            <>
              <span className={me.is_external ? "av ext" : "av"} aria-hidden="true">{initials(me.display_name)}</span>
              <div className="who">{me.display_name}</div>
              <div className="role">{me.roles.map(roleName).join(", ")}</div>
            </>
          )}
          {sso
            ? <button type="button" onClick={() => signOut()}>Sign out</button>
            : <button type="button" onClick={() => setSession(null)}>Switch person</button>}
        </div>
      </aside>
      <main className="main">
        {/* Pages render only once the person is known, and all read the same identity. */}
        {me ? <MeContext.Provider value={me}>{children}</MeContext.Provider> : <p className="muted small">Loading…</p>}
      </main>
    </div>
  );
}

function SignInSide({ footnote }: { footnote: string }) {
  return (
    <section className="signin-side">
      <div className="brand"><BrandMark /><span className="brand-name">ResilienceAI</span></div>
      <div>
        <h1>Every resilience test, one place to coordinate it.</h1>
        <p>Plan the annual programme, mobilise the people, hold the go decision to its readiness conditions,
          and keep the evidence auditors will ask for.</p>
      </div>
      <p className="xs">{footnote}</p>
    </section>
  );
}

function MicrosoftLogo() {
  return (
    <svg viewBox="0 0 21 21" width="20" height="20" aria-hidden="true">
      <rect x="1" y="1" width="9" height="9" fill="#f25022" /><rect x="11" y="1" width="9" height="9" fill="#7fba00" />
      <rect x="1" y="11" width="9" height="9" fill="#00a4ef" /><rect x="11" y="11" width="9" height="9" fill="#ffb900" />
    </svg>
  );
}

function MicrosoftSignIn({ error }: { error?: string }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(error);
  async function go() {
    setBusy(true); setErr(undefined);
    try { await signInWithMicrosoft(); } catch (e) { setErr(friendlySignInError(e)); setBusy(false); }
  }
  return (
    <div className="signin">
      <SignInSide footnote="Single sign-on with Microsoft Entra ID. Your organisation manages your account and access." />
      <section className="signin-main sso">
        <div className="sso-card">
          <h2>Sign in</h2>
          <p className="muted small" style={{ marginTop: 6 }}>Use your work Microsoft account. You&apos;ll see the tests and actions your role allows.</p>
          {err && <div className="notice error" role="alert" style={{ marginTop: 16, marginBottom: 0 }}>{err}</div>}
          <button type="button" className="ms-btn" onClick={go} disabled={busy}>
            <MicrosoftLogo />{busy ? "Opening Microsoft sign-in…" : "Sign in with Microsoft"}
          </button>
        </div>
      </section>
    </div>
  );
}

/** Signed in with Microsoft, but ResilienceAI doesn't know the person or their organisation. */
function AccessProblem({ message }: { message: string }) {
  return (
    <div className="signin">
      <SignInSide footnote="Single sign-on with Microsoft Entra ID." />
      <section className="signin-main sso">
        <div className="sso-card">
          <h2>You can&apos;t open ResilienceAI yet</h2>
          <p className="small" style={{ marginTop: 8 }}>{message}</p>
          <p className="muted small" style={{ marginTop: 8 }}>Once they&apos;ve added your work email, try again.</p>
          <div className="btn-row" style={{ marginTop: 20 }}>
            <button type="button" className="ms-btn" style={{ marginTop: 0 }} onClick={() => window.location.reload()}>Try again</button>
            <button type="button" className="ms-btn quiet" style={{ marginTop: 0 }} onClick={() => signOut()}>Use another account</button>
          </div>
        </div>
      </section>
    </div>
  );
}

function initials(name: string) {
  const words = name.replace(/\(.*?\)/g, "").trim().split(/\s+/);
  return ((words[0]?.[0] ?? "") + (words.length > 1 ? words[words.length - 1][0] : "")).toUpperCase();
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
      <SignInSide footnote="Development sign-in. Production uses your organisation's single sign-on." />
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
                <span className={u.roles.includes("ExternalVendor") ? "av ext" : "av"} aria-hidden="true">{initials(u.display_name)}</span>
                <span>
                  <span className="n">{u.display_name}</span>
                  <span className="r">{u.roles.map(roleName).join(", ")}</span>
                  <span className="d">{u.department}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
