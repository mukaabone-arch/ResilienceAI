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

export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const s = getSession();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
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
