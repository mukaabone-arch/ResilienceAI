"use client";

/**
 * How people sign in. The API says which mode the deployment uses (GET /api/v1/auth/config), so one build of the
 * web app serves both: "dev" (pick who to act as) and "oidc" (Microsoft Entra ID, authorization code + PKCE).
 */
import {
  AccountInfo, BrowserAuthError, InteractionRequiredAuthError, PublicClientApplication,
} from "@azure/msal-browser";
import { getSession } from "./session";

export type AuthConfig =
  | { mode: "dev"; break_glass?: boolean }
  | { mode: "oidc"; provider: "entra"; client_id: string; authority: string; scopes: string[]; break_glass?: boolean };

/** A break-glass session (Microsoft sign-in unavailable). Kept for this browser tab only. */
export type BreakGlass = { token: string; status: string; holder?: string | null; expires_at?: string | null; request_code: string };
const BG_KEY = "resilienceai.breakglass";

export function getBreakGlass(): BreakGlass | null {
  try { const v = sessionStorage.getItem(BG_KEY); return v ? (JSON.parse(v) as BreakGlass) : null; } catch { return null; }
}
export function setBreakGlass(v: BreakGlass | null) {
  try { if (v) sessionStorage.setItem(BG_KEY, JSON.stringify(v)); else sessionStorage.removeItem(BG_KEY); } catch { /* private mode */ }
  window.dispatchEvent(new Event("resilienceai-breakglass"));
}
/** End the break-glass session on the server and forget it here. */
export async function endBreakGlass(): Promise<void> {
  const bg = getBreakGlass();
  if (bg) await fetch("/api/v1/break-glass/end", { method: "POST", headers: { Authorization: `Bearer ${bg.token}` } }).catch(() => undefined);
  setBreakGlass(null);
}

let config: AuthConfig | null = null;
let msal: PublicClientApplication | null = null;

export async function loadAuthConfig(): Promise<AuthConfig> {
  if (config) return config;
  const res = await fetch("/api/v1/auth/config", { cache: "no-store" });
  if (!res.ok) throw new Error(`Can't reach the ResilienceAI API (${res.status})`);
  config = (await res.json()) as AuthConfig;
  return config;
}

export function authMode(): "dev" | "oidc" | null {
  return config?.mode ?? null;
}

/** Start Microsoft sign-in support and finish a sign-in that is coming back from Microsoft. */
export async function initEntra(cfg: Extract<AuthConfig, { mode: "oidc" }>): Promise<AccountInfo | null> {
  if (!msal) {
    msal = new PublicClientApplication({
      auth: { clientId: cfg.client_id, authority: cfg.authority, redirectUri: `${window.location.origin}/`,
              postLogoutRedirectUri: `${window.location.origin}/` },
      // Tokens live for this browser tab only; closing it signs out of ResilienceAI (not of Microsoft).
      cache: { cacheLocation: "sessionStorage" },
    });
    await msal.initialize();
  }
  const result = await msal.handleRedirectPromise();
  if (result?.account) msal.setActiveAccount(result.account);
  const account = msal.getActiveAccount() ?? msal.getAllAccounts()[0] ?? null;
  if (account) msal.setActiveAccount(account);
  // Come back to the page the person was on before signing in.
  const back = sessionStorage.getItem("resilienceai.returnTo");
  if (result && back) {
    sessionStorage.removeItem("resilienceai.returnTo");
    if (back !== window.location.pathname) window.history.replaceState(null, "", back);
  }
  return account;
}

export async function signInWithMicrosoft(): Promise<void> {
  if (!msal || config?.mode !== "oidc") return;
  sessionStorage.setItem("resilienceai.returnTo", window.location.pathname + window.location.search);
  await msal.loginRedirect({ scopes: config.scopes, prompt: "select_account" });
}

export async function signOut(): Promise<void> {
  if (!msal) return;
  await msal.logoutRedirect({ account: msal.getActiveAccount() ?? undefined });
}

/** A current access token for the API; renews silently, or sends the person to Microsoft when it must. */
async function accessToken(): Promise<string | null> {
  if (!msal || config?.mode !== "oidc") return null;
  const account = msal.getActiveAccount();
  if (!account) return null;
  try {
    return (await msal.acquireTokenSilent({ scopes: config.scopes, account })).accessToken;
  } catch (e) {
    if (e instanceof InteractionRequiredAuthError || e instanceof BrowserAuthError) {
      sessionStorage.setItem("resilienceai.returnTo", window.location.pathname + window.location.search);
      await msal.acquireTokenRedirect({ scopes: config.scopes, account });
    }
    throw e;
  }
}

/** Headers that identify the person to the API, in whichever mode the deployment uses. */
export async function authHeaders(): Promise<Record<string, string>> {
  const bg = getBreakGlass();
  if (bg?.status === "Active") return { Authorization: `Bearer ${bg.token}` };
  if (config?.mode === "oidc") {
    const token = await accessToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  }
  const s = getSession();
  return s ? { "X-Tenant-Id": s.tenantId, "X-User-Subject": s.subject } : {};
}
