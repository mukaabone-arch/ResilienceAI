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

/* ---------- Time zones: form inputs are wall-clock time in the test's own zone ---------- */

function offsetMs(utcMs: number, tz: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date(utcMs));
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value);
  const asUtc = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
  return asUtc - utcMs;
}

/** "2027-02-18T14:00" read as wall time in `tz` → ISO string in UTC. */
export function zonedToIso(local: string, tz: string): string | null {
  const m = local.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!m) return null;
  const guess = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  let utc = guess - offsetMs(guess, tz);
  utc = guess - offsetMs(utc, tz); // second pass handles DST edges
  return new Date(utc).toISOString();
}

/** ISO instant → "YYYY-MM-DDTHH:mm" wall time in `tz`, for datetime-local inputs. */
export function isoToZonedInput(iso: string, tz: string): string {
  const d = new Date(new Date(iso).getTime() + offsetMs(new Date(iso).getTime(), tz));
  return d.toISOString().slice(0, 16);
}

export const browserTz = () => Intl.DateTimeFormat().resolvedOptions().timeZone;

export const TIMEZONES = [
  { value: "Asia/Kolkata", label: "India (IST)" },
  { value: "UTC", label: "UTC" },
  { value: "Asia/Dubai", label: "Dubai (GST)" },
  { value: "Asia/Singapore", label: "Singapore (SGT)" },
  { value: "Europe/London", label: "London (UK time)" },
  { value: "America/New_York", label: "New York (US Eastern)" },
];
