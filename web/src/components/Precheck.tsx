"use client";

import { useState } from "react";
import { fmtDateTime } from "@/lib/format";

/**
 * Evidence pre-check: what the automatic checks (and, when enabled, the AI model) found in a file,
 * against the activity it was submitted for. It informs the reviewer; it never decides.
 */
export type Finding = {
  key: string; level: "ok" | "warn" | "info"; title: string; detail?: string | null; by?: string;
  cites: { line: number | null; text: string }[];
};
export type Precheck = {
  id: string; status: string; provider: string; model?: string | null; fallback_reason?: string | null;
  created_at: string; feedback?: string | null; headline: string; ai_status: string;
  summary: { ok: number; warn: number; info: number }; findings: Finding[];
  briefing: { by: string; text: string; shows?: string[]; missing?: string[] };
  final_content?: { review?: string; agreed?: boolean; finding_feedback?: Record<string, { verdict: string; note?: string; by: string; at: string }> } | null;
};

const MARK = { ok: "✓", warn: "!", info: "i" } as const;
const MARK_LABEL = { ok: "Checked, fine", warn: "Check this", info: "Note" } as const;

export function openWarnings(pc?: Precheck | null): Finding[] {
  if (!pc) return [];
  const fb = pc.final_content?.finding_feedback ?? {};
  return pc.findings.filter((f) => f.level === "warn" && fb[f.key]?.verdict !== "wrong");
}

/** One line under the file name: the headline, with the counts. */
export function PrecheckLine({ pc, onOpen, open }: { pc?: Precheck | null; onOpen?: () => void; open?: boolean }) {
  if (!pc) return null;
  const warns = openWarnings(pc).length;
  const cls = warns ? "warn" : pc.summary.ok ? "ok" : "info";
  return (
    <div className={`pc-line ${cls}`}>
      <span className="pc-tag">Pre-check</span>
      <span className="pc-head">{warns ? `${warns} to check` : pc.summary.ok ? "No problems found" : "Nothing checked automatically"}</span>
      {pc.ai_status === "queued" && <span className="pc-soft">AI review in progress</span>}
      {onOpen && <button type="button" className="linkish" aria-expanded={open} onClick={onOpen}>{open ? "Hide" : "Details"}</button>}
    </div>
  );
}

export function PrecheckDetails({ pc, tz, canAct, onFeedback, onRerun }: {
  pc: Precheck; tz?: string; canAct?: boolean;
  onFeedback?: (f: Finding, verdict: "wrong" | "right") => void; onRerun?: () => void;
}) {
  const fb = pc.final_content?.finding_feedback ?? {};
  const closed = pc.status !== "Proposed";
  return (
    <div className="pc-box">
      <ul className="pc-list">
        {pc.findings.map((f) => {
          const disputed = fb[f.key];
          return (
            <li key={f.key} className={`pc-item ${f.level}${disputed?.verdict === "wrong" ? " disputed" : ""}`}>
              <span className="pc-mark" aria-label={MARK_LABEL[f.level]} title={MARK_LABEL[f.level]}>{MARK[f.level]}</span>
              <div className="pc-body">
                <div className="pc-title">{f.title}{f.by === "model" && <span className="pc-by">AI</span>}</div>
                {f.detail && <div className="pc-detail">{f.detail}</div>}
                {f.cites.length > 0 && (
                  <div className="pc-cites">
                    {f.cites.map((c, i) => (
                      <div key={i} className="pc-cite"><span className="pc-ln">{c.line ? `line ${c.line}` : "file"}</span><code>{c.text}</code></div>
                    ))}
                  </div>
                )}
                {disputed && (
                  <div className="pc-disputed">
                    {disputed.verdict === "wrong" ? "Marked not right" : "Confirmed"} by {disputed.by}{disputed.note ? `: ${disputed.note}` : ""}
                  </div>
                )}
                {canAct && !closed && onFeedback && f.level !== "ok" && !disputed && (
                  <button type="button" className="linkish xs" onClick={() => onFeedback(f, "wrong")}>Not right?</button>
                )}
              </div>
            </li>
          );
        })}
      </ul>
      <div className="pc-brief">
        <div className="pc-brief-h">For the reviewer</div>
        {pc.briefing.shows?.length || pc.briefing.missing?.length ? (
          <div className="pc-cols">
            {!!pc.briefing.shows?.length && <div><div className="pc-sub">What it shows</div><ul>{pc.briefing.shows.map((s) => <li key={s}>{s}</li>)}</ul></div>}
            {!!pc.briefing.missing?.length && <div><div className="pc-sub">What seems to be missing</div><ul>{pc.briefing.missing.map((s) => <li key={s}>{s}</li>)}</ul></div>}
          </div>
        ) : <p>{pc.briefing.text}</p>}
      </div>
      <div className="pc-foot">
        <span>
          {pc.provider === "anthropic" ? `Rule checks plus AI model (${pc.model})` : "Rule checks, run on this platform"}
          {" · "}{fmtDateTime(pc.created_at, tz)}
          {pc.fallback_reason && pc.ai_status === "fallback" && <> · AI view not used: {pc.fallback_reason}</>}
        </span>
        {closed && pc.final_content?.review && (
          <span className={pc.final_content.agreed ? "pc-agree" : "pc-disagree"}>
            Reviewer {pc.final_content.review.toLowerCase()} the file{pc.final_content.agreed ? ", in line with the pre-check" : pc.final_content.review === "Accepted" ? " despite the warnings" : " although nothing was flagged"}
          </span>
        )}
        {canAct && !closed && onRerun && <button type="button" className="linkish" onClick={onRerun}>Run again</button>}
      </div>
      <p className="pc-note">The pre-check never accepts or rejects evidence. The reviewer decides, and their decision is logged against it.</p>
    </div>
  );
}
