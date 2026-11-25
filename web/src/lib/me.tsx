"use client";

import { createContext, useContext } from "react";

/** The signed-in person, loaded once by the Shell and shared by every page,
 *  so the sidebar and the page can never disagree about who is acting. */
export type Me = { user_id: string; tenant_id: string; display_name: string; roles: string[]; is_external: boolean };

export const MeContext = createContext<Me | null>(null);

export function useMe(): { data: Me | null } {
  return { data: useContext(MeContext) };
}

export const hasRole = (me: Me | null | undefined, ...roles: string[]) => !!me && roles.some((r) => me.roles.includes(r));
