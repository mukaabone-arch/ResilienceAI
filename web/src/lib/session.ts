"use client";

/**
 * Development sign-in: the backend's AUTH_MODE=dev trusts tenant + subject headers.
 * Production replaces this with OIDC (Entra ID) and a bearer token.
 */
export type Session = { tenantId: string; subject: string; displayName: string };

const KEY = "resilienceai.session";

export function getSession(): Session | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function setSession(s: Session | null) {
  try {
    if (s) window.localStorage.setItem(KEY, JSON.stringify(s));
    else window.localStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: session lasts for this page only */
  }
  window.dispatchEvent(new Event("resilienceai-session"));
}
