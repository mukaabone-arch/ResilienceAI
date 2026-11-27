"use client";

import { fmtDateTime } from "@/lib/format";

function GateGlyph({ open }: { open: boolean }) {
  // Closed: the barrier lies across both posts. Open: it swings up from the left post.
  return (
    <svg className="glyph" viewBox="0 0 52 52" aria-hidden="true">
      <rect x="4" y="10" width="7" height="38" rx="1.5" fill="#fff" />
      <rect x="41" y="10" width="7" height="38" rx="1.5" fill="#fff" opacity={open ? 0.45 : 1} />
      {open ? (
        <rect x="11" y="22" width="34" height="6" fill="#fff" transform="rotate(-62 11 25)" />
      ) : (
        <>
          <rect x="11" y="22" width="30" height="6" fill="#fff" />
          <path d="M15 22l6 6M23 22l6 6M31 22l6 6" stroke="rgba(0,0,0,.25)" strokeWidth="2" />
        </>
      )}
    </svg>
  );
}

function Mark({ kind }: { kind: "met" | "unmet" | "excepted" | "optional" }) {
  const c = { met: "var(--ready)", unmet: "var(--blocked)", excepted: "var(--waiting)", optional: "var(--idle)" }[kind];
  return (
    <svg className="mark" viewBox="0 0 18 18" aria-hidden="true">
      <circle cx="9" cy="9" r="8" fill="none" stroke={c} strokeWidth="1.6" />
      {kind === "met" && <path d="M5 9.3l2.6 2.6L13 6.5" fill="none" stroke={c} strokeWidth="1.8" />}
      {kind === "unmet" && <path d="M6 6l6 6M12 6l-6 6" stroke={c} strokeWidth="1.8" />}
      {kind === "excepted" && <path d="M5.5 9h7" stroke={c} strokeWidth="1.8" />}
    </svg>
  );
}

export type GateItem = { id: string; title: string; status: string; is_mandatory: boolean; evidence_required: boolean; confirmed_at?: string; owner_user_id?: string;
  from_lesson?: { event_code: string; event_id: string; title: string } | null };

export function ReadinessGate({ summary, lifecycle, compact = false, renderItemAction }: {
  summary: any; lifecycle: string; compact?: boolean; renderItemAction?: (item: GateItem, state: string) => React.ReactNode;
}) {
  const gate = summary.gate;
  const items: GateItem[] = summary.items;
  const unmet = new Set<string>(gate.unmet_item_ids ?? []);
  const excepted = new Set<string>(gate.excepted ?? []);
  const after = ["Live", "Paused", "Closure", "Closed"].includes(lifecycle);
  const goDecision = summary.decisions?.find((d: any) => d.approval_type === "Go/No-go" && !d.superseded_at);

  let tone = "closed", title = "Go is blocked", text = "";
  const remaining = gate.mandatory_total - gate.mandatory_ready - excepted.size;
  if (after) {
    tone = "pending"; title = "Go decision taken";
    text = goDecision ? `${goDecision.decision} ${fmtDateTime(goDecision.decided_at)}. Readiness is now a record, not a gate.` : "";
  } else if (!gate.mandatory_total) {
    tone = "closed"; title = "No readiness conditions set";
    text = "Add at least one mandatory condition. The go decision can't be recorded until readiness is defined.";
  } else if (gate.passed && summary.go_decision_valid) {
    tone = "open"; title = "Cleared to start";
    text = "All mandatory conditions are met and the go decision is recorded.";
  } else if (gate.passed) {
    tone = "open"; title = "Ready for the go decision";
    text = lifecycle === "Ready" ? "All mandatory conditions are met. An approver can now record go or no-go."
      : "All mandatory conditions are met. Mark the test ready to ask for the go decision.";
  } else {
    text = `${remaining} mandatory condition${remaining === 1 ? " is" : "s are"} not met. The go decision can't be recorded until ${remaining === 1 ? "it is" : "they are"}, or an authorised exception is granted.`;
  }

  const mandatory = items.filter((i) => i.is_mandatory);
  const optional = items.filter((i) => !i.is_mandatory);
  const stateOf = (i: GateItem) => (!i.is_mandatory ? (i.status === "Ready" ? "met" : "optional")
    : excepted.has(i.title) ? "excepted" : unmet.has(i.id) ? "unmet" : "met") as "met" | "unmet" | "excepted" | "optional";

  return (
    <section className="gate" aria-label="Readiness gate">
      <div className={`gate-verdict ${tone}`}>
        <GateGlyph open={tone !== "closed"} />
        <div>
          <h2>{title}</h2>
          {text && <p>{text}</p>}
        </div>
        <div className="gate-count">
          <strong>{gate.mandatory_ready + excepted.size}/{gate.mandatory_total}</strong>
          <span>mandatory met</span>
        </div>
      </div>
      <div className={`gate-bars ${tone}`} aria-hidden="true">
        {mandatory.map((i) => <i key={i.id} className={stateOf(i) !== "unmet" ? "met" : ""} />)}
      </div>
      {!compact && (
        <ul className="gate-items">
          {[...mandatory, ...optional].map((i) => {
            const s = stateOf(i);
            const why = s === "unmet"
              ? (i.status === "Ready" && i.evidence_required ? "Ready, but the evidence hasn't been accepted yet" : `Currently ${i.status.toLowerCase()}`)
              : s === "excepted" ? "Exception granted" : !i.is_mandatory ? `Optional, ${i.status.toLowerCase()}`
              : i.evidence_required ? "Ready, evidence accepted" : "Ready";
            return (
              <li key={i.id} className={`${s} ${i.is_mandatory ? "" : "optional"}`}>
                <Mark kind={s} />
                <div>
                  <div className="t">{i.title}</div>
                  <div className="why">{why}{i.evidence_required && s === "unmet" ? ", evidence required" : ""}</div>
                  {i.from_lesson && <div className="learned" title={i.from_lesson.title}>Learned from {i.from_lesson.event_code}</div>}
                </div>
                <div>{renderItemAction?.(i, s)}</div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
