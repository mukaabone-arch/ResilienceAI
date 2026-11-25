"use client";

import { useCallback, useEffect, useState } from "react";
import { getSession } from "./session";

export class ApiError extends Error {
  status: number;
  details: string[];
  constructor(status: number, message: string, details: string[] = []) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

export async function api<T = any>(method: string, path: string, body?: unknown, extra?: Record<string, string>): Promise<T> {
  const s = getSession();
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(extra ?? {}) };
  if (s) {
    headers["X-Tenant-Id"] = s.tenantId;
    headers["X-User-Subject"] = s.subject;
  }
  const res = await fetch(`/api/v1${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    throw new ApiError(res.status, data?.error ?? `Request failed (${res.status})`, data?.details ?? []);
  }
  return data as T;
}

/** Load a resource and re-load it on demand (after a mutation). */
export function useApi<T = any>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!path) return;
    let live = true;
    setLoading(true);
    api<T>("GET", path)
      .then((d) => live && (setData(d), setError(null)))
      .catch((e) => live && setError(e instanceof ApiError ? e : new ApiError(0, String(e))))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [path, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}

function authHeaders(): Record<string, string> {
  const s = getSession();
  return s ? { "X-Tenant-Id": s.tenantId, "X-User-Subject": s.subject } : {};
}

/** Multipart upload (the browser sets the boundary, so no Content-Type header here). */
export async function apiUpload<T = any>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`/api/v1${path}`, { method: "POST", headers: authHeaders(), body: form });
  const text = await res.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* non-JSON error page */ }
  if (!res.ok) {
    const msg = res.status === 413 ? "The file is too large to upload." : data?.error ?? `Upload failed (${res.status})`;
    throw new ApiError(res.status, msg, data?.details ?? []);
  }
  return data as T;
}

/** Fetch a protected file and hand it to the browser as a download. */
export async function downloadFile(path: string, fallbackName: string): Promise<void> {
  const res = await fetch(`/api/v1${path}`, { headers: authHeaders(), cache: "no-store" });
  if (!res.ok) {
    let data: any = null;
    try { data = await res.json(); } catch { /* ignore */ }
    throw new ApiError(res.status, data?.error ?? `Download failed (${res.status})`, data?.details ?? []);
  }
  const cd = res.headers.get("content-disposition") ?? "";
  const m = cd.match(/filename\*=UTF-8''([^;]+)/i) ?? cd.match(/filename="?([^";]+)"?/i);
  const name = m ? decodeURIComponent(m[1]) : fallbackName;
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

export async function sha256Hex(file: Blob): Promise<string | null> {
  try {
    const buf = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("");
  } catch {
    return null;   // crypto.subtle needs a secure context (https or localhost); the server still hashes
  }
}
