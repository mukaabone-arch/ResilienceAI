"use client";

import { useEffect, useState } from "react";
import { fmtTime } from "@/lib/format";
import { BreakGlass, endBreakGlass, getBreakGlass, setBreakGlass } from "@/lib/auth";

/**
 * Emergency access when Microsoft sign-in is unavailable. One holder asks; a second holder approves the request
 * code with their own credentials; the session then lasts a fixed time. Every step is recorded.
 */
type Creds = { org: string; email: string; passphrase: string; code: string };
const empty: Creds = { org: "", email: "", passphrase: "", code: "" };

async function post(path: string, body: unknown) {
  const res = await fetch(`/api/v1/break-glass/${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error ?? `Request failed (${res.status})`);
  return data;
}

function CredFields({ v, set, idp }: { v: Creds; set: (c: Creds) => void; idp: string }) {
  const f = (k: keyof Creds) => (e: React.ChangeEvent<HTMLInputElement>) => set({ ...v, [k]: e.target.value });
  return (
    <>
      <div className="form-row">
        <div className="field"><label htmlFor={`${idp}-org`}>Organisation code</label>
          <input id={`${idp}-org`} className="input" autoComplete="organization" value={v.org} onChange={f("org")} placeholder="e.g. BANK" /></div>
        <div className="field"><label htmlFor={`${idp}-code`}>Authenticator code</label>
          <input id={`${idp}-code`} className="input" inputMode="numeric" autoComplete="one-time-code" maxLength={7} value={v.code} onChange={f("code")} placeholder="6 digits" /></div>
      </div>
      <div className="field"><label htmlFor={`${idp}-email`}>Break-glass email</label>
        <input id={`${idp}-email`} className="input" type="email" autoComplete="username" value={v.email} onChange={f("email")} /></div>
      <div className="field"><label htmlFor={`${idp}-pass`}>Passphrase</label>
        <input id={`${idp}-pass`} className="input" type="password" autoComplete="current-password" value={v.passphrase} onChange={f("passphrase")} /></div>
    </>
  );
}

export function BreakGlassPage() {
  const [mode, setMode] = useState<"request" | "approve">("request");
  const [creds, setCreds] = useState(empty);
  const [reason, setReason] = useState("");
  const [reqCode, setReqCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [pending, setPending] = useState<BreakGlass | null>(null);
  useEffect(() => {                         // a request still waiting in this tab (e.g. after a reload)
    const bg = getBreakGlass();
    if (bg?.status === "Pending") setPending(bg);
  }, []);

  // While waiting for the second holder, check every few seconds; once approved, go in.
  useEffect(() => {
    if (!pending) return;
    const tick = setInterval(async () => {
      const res = await fetch("/api/v1/break-glass/session", { headers: { Authorization: `Bearer ${pending.token}` } });
      const s = await res.json().catch(() => null);
      if (!res.ok || !s) return;
      if (s.status === "Active") {
        setBreakGlass({ ...pending, status: "Active", expires_at: s.expires_at });
        window.location.assign("/workspace");
      } else if (s.status !== "Pending") {
        setBreakGlass(null); setPending(null);
        setErr(s.status === "Expired" ? "The request wasn't approved within 15 minutes. Ask again." : `The request was ${s.status.toLowerCase()}.`);
      }
    }, 4000);
    return () => clearInterval(tick);
  }, [pending]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setErr(null); setDone(null);
    try {
      const body = { ...creds, code: creds.code.replace(/\s/g, "") };
      if (mode === "request") {
        const r = await post("request", { ...body, reason });
        const bg: BreakGlass = { token: r.token, status: "Pending", holder: r.holder, request_code: r.request_code };
        setBreakGlass(bg); setPending(bg);
      } else {
        const r = await post("approve", { ...body, request_code: reqCode.trim().toUpperCase() });
        setDone(`Approved. ${r.holder} can now use ResilienceAI until ${fmtTime(r.expires_at)}.`);
        setReqCode("");
      }
      setCreds({ ...empty, org: creds.org });
    } catch (x) {
      setErr((x as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (pending) {
    return (
      <Frame>
        <h2>Waiting for a second holder</h2>
        <p className="muted small">Call another break-glass holder and read them this request code. They approve it on their own device with their own passphrase and code.</p>
        <div className="bg-code" aria-label="Request code">{pending.request_code}</div>
        <p className="xs muted">This page continues on its own once they approve. The request expires after 15 minutes.</p>
        {err && <div className="notice error" role="alert">{err}</div>}
        <button type="button" className="bg-cancel" onClick={() => { endBreakGlass(); setPending(null); }}>Cancel request</button>
      </Frame>
    );
  }
  const ok = creds.org && creds.email && creds.passphrase && creds.code.replace(/\s/g, "").length === 6
    && (mode === "request" ? reason.trim().length >= 10 : reqCode.trim().length >= 8);
  return (
    <Frame>
      <h2>Break-glass sign-in</h2>
      <p className="muted small">For named break-glass holders when Microsoft sign-in is unavailable. A second holder must approve, the session ends after a fixed time, and everything is recorded.</p>
      <div className="seg" role="group" aria-label="What do you need to do?" style={{ margin: "14px 0" }}>
        <button type="button" aria-pressed={mode === "request"} onClick={() => { setMode("request"); setErr(null); setDone(null); }}>Request access</button>
        <button type="button" aria-pressed={mode === "approve"} onClick={() => { setMode("approve"); setErr(null); setDone(null); }}>Approve a request</button>
      </div>
      <form onSubmit={submit}>
        {err && <div className="notice error" role="alert">{err}</div>}
        {done && <div className="notice ok" role="status">{done}</div>}
        {mode === "approve" && (
          <div className="field"><label htmlFor="bg-req">Request code they read to you</label>
            <input id="bg-req" className="input" value={reqCode} onChange={(e) => setReqCode(e.target.value)} placeholder="e.g. 7K3Q-M9PD" autoComplete="off" /></div>
        )}
        <CredFields v={creds} set={setCreds} idp={mode} />
        {mode === "request" && (
          <div className="field"><label htmlFor="bg-reason">Why it&apos;s needed</label>
            <textarea id="bg-reason" className="textarea" value={reason} onChange={(e) => setReason(e.target.value)}
              placeholder="e.g. Entra outage, incident INC0099123; DR test starts at 22:00" />
            <span className="hint">Recorded with the session and seen by risk, audit and administrators.</span></div>
        )}
        <button type="submit" className="btn primary" disabled={!ok || busy} style={{ marginTop: 6 }}>
          {busy ? "Checking…" : mode === "request" ? "Request access" : "Approve request"}</button>
      </form>
      <p className="xs muted" style={{ marginTop: 16 }}><a href="/">Back to normal sign-in</a></p>
    </Frame>
  );
}

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className="signin">
      <section className="signin-side">
        <div className="brand"><span className="brand-name">ResilienceAI</span></div>
        <div>
          <h1>Emergency access</h1>
          <p>Use this only when Microsoft sign-in is down and work can&apos;t wait. Each use is reviewed afterwards, and the credentials are rotated.</p>
        </div>
        <p className="xs">Two-person approval · time-limited · fully audited</p>
      </section>
      <section className="signin-main sso"><div className="sso-card bg-card">{children}</div></section>
    </div>
  );
}

/** Shown across the top of every page during a break-glass session. */
export function BreakGlassBanner({ bg, name }: { bg: BreakGlass; name?: string }) {
  const [left, setLeft] = useState("");
  useEffect(() => {
    const upd = () => {
      if (!bg.expires_at) return;
      const ms = new Date(bg.expires_at).getTime() - Date.now();
      if (ms <= 0) { setBreakGlass(null); window.location.assign("/"); return; }
      setLeft(ms > 3600_000 ? `${Math.floor(ms / 3600_000)} h ${Math.round((ms % 3600_000) / 60000)} min` : `${Math.max(1, Math.round(ms / 60000))} min`);
    };
    upd();
    const t = setInterval(upd, 20_000);
    return () => clearInterval(t);
  }, [bg.expires_at]);
  return (
    <div className="bg-banner" role="status">
      <strong>Break-glass session</strong>
      <span>{name ?? bg.holder} · ends {fmtTime(bg.expires_at)}{left ? ` (${left} left)` : ""} · everything you do is recorded</span>
      <button type="button" onClick={async () => { await endBreakGlass(); window.location.assign("/"); }}>End session</button>
    </div>
  );
}
