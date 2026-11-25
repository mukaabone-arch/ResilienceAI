export function fmtDateTime(iso?: string | null, tz?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
    timeZone: tz || undefined,
  }).format(d);
}

export function fmtDate(iso?: string | null, tz?: string): string {
  if (!iso) return "—";
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: tz || undefined })
    .format(new Date(iso));
}

export function fmtTime(iso?: string | null, tz?: string): string {
  if (!iso) return "—";
  return new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: tz || undefined })
    .format(new Date(iso));
}

/** "in 3 days", "2 h ago" */
export function relative(iso?: string | null): string {
  if (!iso) return "";
  const diff = new Date(iso).getTime() - Date.now();
  const abs = Math.abs(diff);
  const h = abs / 36e5;
  const txt = h < 1 ? `${Math.max(1, Math.round(abs / 6e4))} min` : h < 48 ? `${Math.round(h)} h` : `${Math.round(h / 24)} days`;
  return diff >= 0 ? `in ${txt}` : `${txt} ago`;
}

export function tzLabel(tz?: string): string {
  if (!tz) return "";
  return tz === "Asia/Kolkata" ? "IST" : tz.replace(/_/g, " ");
}

const ROLE_NAMES: Record<string, string> = {
  EnterpriseProgrammeAdministrator: "Programme administrator",
  DRProgrammeManager: "DR programme manager",
  TestManager: "Test manager",
  TeamLead: "Team lead",
  TechnicalStakeholder: "Technical stakeholder",
  BusinessOwner: "Business owner",
  RiskCompliance: "Risk and compliance",
  SeniorManagement: "Senior management",
  ExternalVendor: "External vendor",
  Auditor: "Auditor",
};

export const roleName = (r: string) => ROLE_NAMES[r] ?? r;
