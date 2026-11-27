"""Rule-based evidence pre-checks: plain functions over the file's text lines.

No model and no network: each check reads the lines, and every finding points at
the lines it came from. Excerpts are always masked (card numbers, Aadhaar
numbers, passwords...) before they leave this module, so a pre-check never
spreads the data it warns about.

Levels: "ok" (checked, fine), "warn" (a person should look), "info" (couldn't
be checked, or worth knowing).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
MON = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
TZ_ABBR = {"IST": "Asia/Kolkata", "UTC": "UTC", "GMT": "UTC", "Z": "UTC"}
EXCERPT = 160


def finding(key: str, level: str, title: str, detail: str | None = None, cites: list[dict] | None = None) -> dict:
    return {"key": key, "level": level, "title": title, "detail": detail, "cites": cites or []}


def cite(lines: list[str], i: int) -> dict:
    """i is a 0-based index; lines are shown 1-based."""
    text = mask(lines[i].strip())
    return {"line": i + 1, "text": text if len(text) <= EXCERPT else text[: EXCERPT - 1] + "…"}


# ---------------------------------------------------------------------------
# Sensitive data (also used to mask every excerpt)
# ---------------------------------------------------------------------------
def luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


_VD = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
       [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
       [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
       [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_VP = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
       [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
       [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VD[c][_VP[i % 8][int(ch)]]
    return c == 0


CARD_GROUPED = re.compile(r"(?<![\d-])\d{4}([ -])\d{4}\1\d{4}\1\d{4}(?:\1\d{1,3})?(?![\d-])")
CARD_PLAIN = re.compile(r"(?<!\d)\d{15,16}(?!\d)")
CARD_WORDS = re.compile(r"(?i)\b(card|pan|cc|ccn|visa|master ?card|rupay|amex|debit|credit)\b")
AADHAAR_GROUPED = re.compile(r"(?<![\d-])[2-9]\d{3}([ -])\d{4}\1\d{4}(?![\d-])")
AADHAAR_PLAIN = re.compile(r"(?<!\d)[2-9]\d{11}(?!\d)")
AADHAAR_WORDS = re.compile(r"(?i)\b(aadhaa?r|aadhar|uidai|uid)\b")
TAX_PAN = re.compile(r"(?<![A-Z0-9])[A-Z]{3}[ABCFGHLJPT][A-Z]\d{4}[A-Z](?![A-Z0-9])")
SECRET = re.compile(r"(?i)\b(password|passwd|pwd|passphrase|secret|api[_-]?key|access[_-]?key|client[_-]?secret|"
                    r"auth[_-]?token|bearer)\b(\s*[:=]\s*|\s+)([\"']?)([^\s\"',;]{3,})")
IDENTIFIED_BY = re.compile(r"(?i)\b(identified by)\s+([\"']?)([^\s\"';]{3,})")
CONN_PW = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)([^@\s]+)(@)")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")
AWS_KEY = re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")
NOT_A_SECRET = re.compile(r"(?i)^(\*+|x{3,}|•+|#+|<[^>]*>|\$\{[^}]*\}|%[^%]*%|\{\{[^}]*\}\}|redacted|masked|hidden|"
                          r"null|none|n/?a|not ?set|required|expired|changed|reset|policy|is|was|for|of|\(hidden\))$")
_BULLETS = "••••"


def _card_ok(digits: str) -> bool:
    return 13 <= len(digits) <= 19 and digits[0] in "3456" and len(set(digits)) > 1 and luhn(digits)


def sensitive_hits(line: str) -> list[tuple[str, int, int]]:
    """(kind, start, end) of possible sensitive values in a line, without overlaps."""
    hits: list[tuple[str, int, int]] = []

    def free(s: int, e: int) -> bool:
        return all(e <= hs or s >= he for _, hs, he in hits)

    for m in CARD_GROUPED.finditer(line):
        if _card_ok(re.sub(r"\D", "", m.group())):
            hits.append(("card number", m.start(), m.end()))
    if CARD_WORDS.search(line):
        for m in CARD_PLAIN.finditer(line):
            if free(m.start(), m.end()) and _card_ok(m.group()):
                hits.append(("card number", m.start(), m.end()))
    for m in AADHAAR_GROUPED.finditer(line):
        if free(m.start(), m.end()) and verhoeff(re.sub(r"\D", "", m.group())):
            hits.append(("Aadhaar number", m.start(), m.end()))
    if AADHAAR_WORDS.search(line):
        for m in AADHAAR_PLAIN.finditer(line):
            if free(m.start(), m.end()) and verhoeff(m.group()):
                hits.append(("Aadhaar number", m.start(), m.end()))
    for m in TAX_PAN.finditer(line):
        if free(m.start(), m.end()):
            hits.append(("PAN (tax ID)", m.start(), m.end()))
    for m in SECRET.finditer(line):
        if not NOT_A_SECRET.match(m.group(4)) and free(m.start(4), m.end(4)):
            hits.append(("password or key", m.start(4), m.end(4)))
    for m in IDENTIFIED_BY.finditer(line):
        if not NOT_A_SECRET.match(m.group(3)) and free(m.start(3), m.end(3)):
            hits.append(("password or key", m.start(3), m.end(3)))
    for m in CONN_PW.finditer(line):
        if free(m.start(2), m.end(2)) and not NOT_A_SECRET.match(m.group(2)):
            hits.append(("password or key", m.start(2), m.end(2)))
    for m in AWS_KEY.finditer(line):
        if free(m.start(), m.end()):
            hits.append(("password or key", m.start(), m.end()))
    if PRIVATE_KEY.search(line):
        hits.append(("private key", 0, len(line)))
    return sorted(hits, key=lambda h: h[1])


def _masked(kind: str, value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if kind == "card number":
        return f"{digits[:4]} {_BULLETS} {_BULLETS} {digits[-4:]}"
    if kind == "Aadhaar number":
        return f"XXXX XXXX {digits[-4:]}"
    if kind == "PAN (tax ID)":
        return value[:2] + "•" * 7 + value[-1]
    if kind == "private key":
        return "[private key removed]"
    return _BULLETS


def mask(line: str) -> str:
    out, pos = [], 0
    for kind, s, e in sensitive_hits(line):
        out.append(line[pos:s])
        out.append(_masked(kind, line[s:e]))
        pos = e
    out.append(line[pos:])
    return "".join(out)


def check_sensitive(lines: list[str]) -> dict | None:
    counts: dict[str, int] = {}
    cites: list[dict] = []
    for i, line in enumerate(lines):
        hits = sensitive_hits(line)
        for kind, _, _ in hits:
            counts[kind] = counts.get(kind, 0) + 1
        if hits and len(cites) < 5:
            cites.append(cite(lines, i))
    if not counts:
        return finding("sensitive", "ok", "No card numbers, Aadhaar numbers or passwords found")
    plural = {"card number": "card numbers", "Aadhaar number": "Aadhaar numbers", "PAN (tax ID)": "PANs (tax IDs)",
              "password or key": "passwords or keys", "private key": "private keys"}
    parts = [f"{n} {k if n == 1 else plural[k]}" for k, n in sorted(counts.items(), key=lambda x: -x[1])]
    return finding("sensitive", "warn", "Possible sensitive data: " + ", ".join(parts),
                   "Redact these and upload a cleaned copy, so reviewers and auditors don't handle them. "
                   "The excerpts below are masked.", cites)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Stamp:
    at: datetime
    line: int          # 0-based
    precise: bool      # has a time of day


P_ISO = re.compile(r"(?<![\d.])(20\d{2})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:[.,]\d+)?\s?(Z|[+-]\d{2}:?\d{2})?)?(?![\d])")
P_SLASH_YMD = re.compile(r"(?<![\d.])(20\d{2})/(\d{2})/(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?(?!\d)")
P_DMY = re.compile(r"(?<![\d.])(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})(?:[ T,]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?(?!\d)")
P_DMONY = re.compile(r"(?i)(?<![\w])(\d{1,2})[- ]" + MON + r"[- ,]+(20\d{2}|\d{2})(?![\d])(?:[ :T,]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")
P_MONDY = re.compile(r"(?i)(?<![\w])" + MON + r" (\d{1,2})(?:st|nd|rd|th)?,? (20\d{2})(?:,? (\d{1,2}):(\d{2})(?::(\d{2}))?)?")
P_UNIX = re.compile(r"(?i)\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]* " + MON + r" +(\d{1,2}) (\d{2}):(\d{2}):(\d{2})(?: ([A-Z]{2,5}))? (20\d{2})")
P_SYSLOG = re.compile(r"(?i)^\s*" + MON + r" +(\d{1,2}) (\d{2}):(\d{2}):(\d{2})\b")


def _mk(y, mo, d, h, mi, s, tz: ZoneInfo, off: str | None = None) -> tuple[datetime, bool] | None:
    try:
        y = int(y)
        y = y + 2000 if y < 100 else y
        precise = h is not None
        dt = datetime(y, int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0))
    except (TypeError, ValueError):
        return None
    if not 2000 <= dt.year <= 2100:
        return None
    if off:
        if off in TZ_ABBR:
            return dt.replace(tzinfo=ZoneInfo(TZ_ABBR[off])), precise
        if re.fullmatch(r"[+-]\d{2}:?\d{2}", off):
            sign = 1 if off[0] == "+" else -1
            hh, mm = int(off[1:3]), int(off[-2:])
            return dt.replace(tzinfo=timezone(sign * timedelta(hours=hh, minutes=mm))), precise
    return dt.replace(tzinfo=tz), precise


def stamps_in(line: str, idx: int, tz: ZoneInfo, year_hint: int) -> list[Stamp]:
    found: list[tuple[int, int, tuple[datetime, bool]]] = []

    def take(m, built):
        if built and all(m.end() <= s or m.start() >= e for s, e, _ in found):
            found.append((m.start(), m.end(), built))

    for m in P_ISO.finditer(line):
        take(m, _mk(m[1], m[2], m[3], m[4], m[5], m[6], tz, m[7]))
    for m in P_SLASH_YMD.finditer(line):
        take(m, _mk(m[1], m[2], m[3], m[4], m[5], m[6], tz))
    for m in P_UNIX.finditer(line):
        take(m, _mk(m[7], MONTHS[m[1][:3].lower()], m[2], m[3], m[4], m[5], tz, (m[6] or "").upper() or None))
    for m in P_DMONY.finditer(line):
        take(m, _mk(m[3], MONTHS[m[2][:3].lower()], m[1], m[4], m[5], m[6], tz))
    for m in P_MONDY.finditer(line):
        take(m, _mk(m[3], MONTHS[m[1][:3].lower()], m[2], m[4], m[5], m[6], tz))
    for m in P_DMY.finditer(line):                      # day first (Indian convention) unless that can't be
        d, mo = int(m[1]), int(m[2])
        if mo > 12 and d <= 12:
            d, mo = mo, d
        take(m, _mk(m[3], mo, d, m[4], m[5], m[6], tz))
    m = P_SYSLOG.match(line)
    if m and not found:                                 # syslog has no year: assume the upload's year
        take(m, _mk(year_hint, MONTHS[m[1][:3].lower()], m[2], m[3], m[4], m[5], tz))
    return [Stamp(b[0], idx, b[1]) for _, _, b in sorted(found, key=lambda f: f[0])]


def all_stamps(lines: list[str], tz: ZoneInfo, year_hint: int) -> list[Stamp]:
    out: list[Stamp] = []
    for i, line in enumerate(lines):
        if any(ch.isdigit() for ch in line):
            out.extend(stamps_in(line, i, tz, year_hint))
    return out


def fmt(dt: datetime, tz: ZoneInfo, precise: bool = True) -> str:
    local = dt.astimezone(tz)
    return local.strftime("%d %b %Y, %H:%M" if precise else "%d %b %Y")


FORWARD = re.compile(r"(?i)\b(next|scheduled|schedule|until|expir\w*|valid|due|planned|renew\w*|retention|retain\w*)\b")


def check_dates(lines: list[str], stamps: list[Stamp], lower: datetime, upper: datetime, tz: ZoneInfo) -> dict:
    if not stamps:
        return finding("dates", "info", "No dates found in the file, so its age couldn't be checked")
    future = [s for s in stamps if s.at > upper + (timedelta(0) if s.precise else timedelta(days=1))
              and not FORWARD.search(lines[s.line])]
    past_ok = [s for s in stamps if s not in future]
    if future:
        f = min(future, key=lambda s: s.at)
        return finding("dates", "warn", f"Dated after it was uploaded ({fmt(f.at, tz, f.precise)})",
                       "A date later than the upload usually means a wrong clock, a wrong time zone or an edited file.",
                       [cite(lines, f.line)])
    latest = max(past_ok, key=lambda s: s.at)
    earliest = min(past_ok, key=lambda s: s.at)
    if earliest.at == latest.at:
        span = fmt(latest.at, tz, latest.precise)
    elif earliest.at.astimezone(tz).date() == latest.at.astimezone(tz).date() and earliest.precise and latest.precise:
        span = f"{fmt(earliest.at, tz)} to {latest.at.astimezone(tz):%H:%M}"
    else:
        span = f"{fmt(earliest.at, tz, earliest.precise)} to {fmt(latest.at, tz, latest.precise)}"
    end_of = latest.at + (timedelta(0) if latest.precise else timedelta(days=1))
    if end_of < lower:
        days = max(1, (lower - end_of).days)
        return finding("dates", "warn", f"Older than expected: the latest date in the file is {fmt(latest.at, tz, latest.precise)}",
                       f"That is {days} day{'s' if days != 1 else ''} before evidence for this test is expected "
                       f"(from {fmt(lower, tz, False)}). It may be from an earlier test or an old run.",
                       [cite(lines, latest.line)])
    return finding("dates", "ok", f"Dated within the expected window ({span})", None, [cite(lines, latest.line)])


# ---------------------------------------------------------------------------
# Recovery point and recovery time
# ---------------------------------------------------------------------------
UNIT_SECONDS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60,
                "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600}
DURATION = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b|"
                      r"\+?(\d{2}) (\d{2}):(\d{2}):(\d{2})|(?<![\d:])(\d{1,2}):(\d{2}):(\d{2})(?![\d:])")
RPO_WORDS = re.compile(r"(?i)\b(rpo|data loss|apply lag|transport lag|replication lag|redo lag|lag)\b")
RTO_WORDS = re.compile(r"(?i)\b(rto|recovery time|time to recover|downtime|outage duration|total outage|failover (?:time|duration))\b")
NOT_ACTUAL = re.compile(r"(?i)\b(target|objective|sla|threshold|tolerance|expected|agreed|allowed|limit|max(?:imum)?|should|must)\b")
RP_LINE = re.compile(r"(?i)recovery point|restore point|point[- ]in[- ]time|consistent (?:as of|to|until)|"
                     r"last (?:applied|archived|received|successful|good|completed)?\s*(?:log|backup|transaction|sequence|redo|archive)|"
                     r"backup (?:completed|finished|taken|successful)|snapshot (?:taken|created|completed)|log applied")
INCIDENT_LINE = re.compile(r"(?i)failover (?:initiated|started|triggered|begun|begins)|switchover (?:initiated|started)|"
                           r"disaster declared|outage (?:start|began|started|declared)|primary (?:down|failed|lost|unavailable)|"
                           r"incident (?:start|declared)|failure (?:detected|occurred)|simulated failure|crash(?:ed)?\b")
DONE_LINE = re.compile(r"(?i)failover (?:completed?|successful|finished)|switchover (?:completed?|successful)|"
                       r"service (?:restored|available|resumed)|application (?:available|up|online|restored)|"
                       r"database (?:opened|is open|open(?:ed)? read write|available)|recovery (?:completed?|finished)|"
                       r"business (?:resumed|as usual)")


def _duration_s(text: str) -> float | None:
    m = DURATION.search(text)
    if not m:
        return None
    if m[1]:
        return float(m[1]) * UNIT_SECONDS[m[2].lower()]
    if m[3]:
        return int(m[3]) * 86400 + int(m[4]) * 3600 + int(m[5]) * 60 + int(m[6])
    return int(m[7]) * 3600 + int(m[8]) * 60 + int(m[9])


def _explicit(lines: list[str], words: re.Pattern) -> tuple[float, int] | None:
    best: tuple[float, int] | None = None
    for i, line in enumerate(lines):
        m = words.search(line)
        if not m or NOT_ACTUAL.search(line):
            continue
        val = _duration_s(line[m.end():])
        if val is not None and (best is None or val > best[0]):
            best = (val, i)
    return best


def _first_stamp(stamps_by_line: dict[int, list[Stamp]], lines: list[str], pat: re.Pattern) -> list[Stamp]:
    return [stamps_by_line[i][0] for i, line in enumerate(lines) if i in stamps_by_line and pat.search(line)]


def human(seconds: float) -> str:
    s = abs(seconds)
    if s < 90:
        return f"{round(s)} s"
    if s < 5400:
        return f"{round(s / 60)} min"
    if s < 172800:
        h = s / 3600
        return f"{h:.1f} h".replace(".0 h", " h")
    return f"{round(s / 86400)} days"


def check_recovery(lines: list[str], stamps: list[Stamp], rpo_target_s: float | None, rto_target_s: float | None,
                   expects_rpo: bool, expects_rto: bool) -> list[dict]:
    out: list[dict] = []
    by_line: dict[int, list[Stamp]] = {}
    for s in stamps:
        by_line.setdefault(s.line, []).append(s)
    incidents = _first_stamp(by_line, lines, INCIDENT_LINE)
    incident = min(incidents, key=lambda s: s.at) if incidents else None

    # Recovery point: an explicit figure ("apply lag 00:00:12", "RPO achieved: 4 min"), else the gap between the
    # last recovery point before the failure and the failure itself.
    rpo: tuple[float, list[int], str] | None = None
    ex = _explicit(lines, RPO_WORDS)
    if ex:
        rpo = (ex[0], [ex[1]], "reported in the file")
    elif incident:
        rps = [s for s in _first_stamp(by_line, lines, RP_LINE) if s.at <= incident.at and s.line != incident.line]
        if rps:
            rp = max(rps, key=lambda s: s.at)
            gap = (incident.at - rp.at).total_seconds()
            if gap < 7 * 86400:
                rpo = (gap, [rp.line, incident.line], "between the last recovery point and the failure")
    if rpo:
        val, at, how = rpo
        cites = [cite(lines, i) for i in at]
        if rpo_target_s is None:
            out.append(finding("rpo", "info", f"Recovery point about {human(val)} ({how})",
                               "No RPO target is set for this test, so it couldn't be compared. Set one on the Overview tab.", cites))
        elif val > rpo_target_s:
            out.append(finding("rpo", "warn", f"Recovery point about {human(val)}, beyond the {human(rpo_target_s)} RPO target",
                               f"Measured {how}. Check the figure before accepting this as proof the target was met.", cites))
        else:
            out.append(finding("rpo", "ok", f"Recovery point about {human(val)}, within the {human(rpo_target_s)} RPO target",
                               f"Measured {how}.", cites))
    elif expects_rpo and rpo_target_s is not None:
        out.append(finding("rpo", "info", f"No recovery point figure found to compare with the {human(rpo_target_s)} RPO target"))

    rto: tuple[float, list[int], str] | None = None
    ex = _explicit(lines, RTO_WORDS)
    if ex:
        rto = (ex[0], [ex[1]], "reported in the file")
    elif incident:
        dones = [s for s in _first_stamp(by_line, lines, DONE_LINE) if s.at >= incident.at and s.line != incident.line]
        if dones:
            done = min(dones, key=lambda s: s.at)
            gap = (done.at - incident.at).total_seconds()
            if gap < 7 * 86400:
                rto = (gap, [incident.line, done.line], "from the failure to the service being back")
    if rto:
        val, at, how = rto
        cites = [cite(lines, i) for i in at]
        if rto_target_s is None:
            out.append(finding("rto", "info", f"Recovery took about {human(val)} ({how})",
                               "No RTO target is set for this test, so it couldn't be compared. Set one on the Overview tab.", cites))
        elif val > rto_target_s:
            out.append(finding("rto", "warn", f"Recovery took about {human(val)}, beyond the {human(rto_target_s)} RTO target",
                               f"Measured {how}.", cites))
        else:
            out.append(finding("rto", "ok", f"Recovery took about {human(val)}, within the {human(rto_target_s)} RTO target",
                               f"Measured {how}.", cites))
    elif expects_rto and rto_target_s is not None:
        out.append(finding("rto", "info", f"No recovery time figure found to compare with the {human(rto_target_s)} RTO target"))
    return out


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
NONPROD_PARTS = {"uat", "sit", "dev", "qa", "stg", "staging", "preprod", "pprd", "sandbox", "sbx", "test", "tst",
                 "perf", "trn", "training", "nonprod", "np"}
PROD_PARTS = {"prod", "prd", "production", "dr", "drc", "drs", "live", "pr"}
NONPROD_LABEL = {"uat": "UAT", "sit": "SIT", "dev": "development", "qa": "QA", "stg": "staging", "staging": "staging",
                 "preprod": "pre-production", "pprd": "pre-production", "sandbox": "sandbox", "sbx": "sandbox",
                 "test": "test", "tst": "test", "perf": "performance test", "trn": "training", "training": "training",
                 "nonprod": "non-production", "np": "non-production"}
HOSTLIKE = re.compile(r"(?<![\w.-])[A-Za-z][A-Za-z0-9]*(?:[-._][A-Za-z0-9]+)+(?![\w-])|(?<![\w.-])[A-Za-z]+\d+[A-Za-z0-9]*(?![\w-])")
ENV_LINE = re.compile(r"(?i)\b(?:env|environment|region|tier|landscape)\s*[:=]\s*([A-Za-z-]+)")
BANNER = re.compile(r"(?<![A-Za-z])(UAT|SIT|PREPROD|PRE-PROD|STAGING|NON-PROD|NONPROD)(?![A-Za-z])")
FILE_EXT = re.compile(r"(?i)\.(log|txt|csv|pdf|xlsx?|docx?|json|xml|png|jpe?g|sql|sh|ora|dbf|arc|trc|bak|dmp|zip|gz)$")


def _parts(word: str) -> list[str]:
    return [p for p in re.split(r"[-._]|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", word.lower()) if p and not p.isdigit()]


def check_environment(lines: list[str], expects_nonprod: bool) -> dict | None:
    nonprod: dict[str, tuple[str, int]] = {}      # host -> (label, line)
    prod: dict[str, int] = {}
    for i, line in enumerate(lines):
        for m in ENV_LINE.finditer(line):
            v = m[1].lower().replace("-", "")
            if v in NONPROD_LABEL:
                nonprod.setdefault(m.group(0), (NONPROD_LABEL[v], i))
            elif v in PROD_PARTS:
                prod.setdefault(m.group(0), i)
        for m in BANNER.finditer(line):
            v = m[1].lower().replace("-", "")
            nonprod.setdefault(m[1], (NONPROD_LABEL.get(v, v), i))
        for m in HOSTLIKE.finditer(line):
            w = m.group(0)
            if FILE_EXT.search(w) or re.fullmatch(r"(?i)v?\d+(\.\d+)+", w):
                continue
            parts = set(_parts(w))
            np_hit = parts & NONPROD_PARTS
            p_hit = parts & PROD_PARTS
            if np_hit and not p_hit:
                label = NONPROD_LABEL[sorted(np_hit)[0]]
                if label == "test" and not re.search(r"\d", w):
                    continue                     # "dr-test", "test-plan": words, not host names
                nonprod.setdefault(w, (label, i))
            elif p_hit and not np_hit:
                prod.setdefault(w, i)
    if nonprod and not expects_nonprod:
        names = list(nonprod)[:4]
        labels = sorted({nonprod[n][0] for n in names})
        lines_hit = sorted({nonprod[n][1] for n in names})
        return finding("environment", "warn",
                       f"Looks like {' / '.join(labels)} systems: {', '.join(names)}",
                       "This test expects production or DR evidence. Check the file came from the right environment."
                       + (f" It also mentions {', '.join(list(prod)[:2])}." if prod else ""),
                       [cite(lines, i) for i in lines_hit[:3]])
    if prod and not nonprod:
        names = list(prod)[:3]
        return finding("environment", "ok", f"Host names look like production / DR ({', '.join(names)})", None,
                       [cite(lines, prod[names[0]])])
    return None


# ---------------------------------------------------------------------------
# Relevance by key terms (the model, when enabled, judges this more closely)
# ---------------------------------------------------------------------------
STOP = set("""a an and are as at be by for from has have in into is it its of on or that the this to was were will with
within before after during all any each per via not no our your their them they we you up out over under than then
there these those such can could should would may might must shall do does done make made get set prepare confirm
check verify ensure provide take taken run start complete completed evidence test testing activity activities step
team owner record records new update updated using use used ready readiness item items""".split())


def key_terms(*texts: str | None, limit: int = 8) -> list[str]:
    seen: list[str] = []
    for t in texts:
        for w in re.findall(r"[A-Za-z][A-Za-z0-9/+-]{2,}", t or ""):
            lw = w.lower().strip("-/+")
            if lw in STOP or len(lw) < 3 or lw in seen:
                continue
            seen.append(lw)
    return seen[:limit]


def term_found(term: str, text_lower: str) -> bool:
    stem = term if len(term) <= 5 else term[: max(5, len(term) - 2)]
    return re.search(r"(?<![a-z0-9])" + re.escape(stem), text_lower) is not None


def check_relevance(text: str, terms: list[str], what: str) -> dict | None:
    if not terms:
        return None
    low = text.lower()
    hit = [t for t in terms if term_found(t, low)]
    miss = [t for t in terms if t not in hit]
    ratio = len(hit) / len(terms)
    if ratio >= 0.6:
        return finding("relevance", "ok", f"Mentions {len(hit)} of {len(terms)} key terms from the {what}: {', '.join(hit)}")
    if ratio >= 0.3:
        return finding("relevance", "info", f"Mentions some of the {what}'s key terms ({len(hit)} of {len(terms)}: {', '.join(hit)})",
                       f"Not mentioned: {', '.join(miss)}.")
    return finding("relevance", "warn", f"Mentions few of the {what}'s key terms ({len(hit)} of {len(terms)})",
                   f"Not mentioned: {', '.join(miss)}. Check this is the right file for the {what}.")
