"use client";

import { useEffect, useRef, useState } from "react";
import { api, apiUpload, sha256Hex } from "@/lib/api";
import { ErrorNotice } from "./ui";
import { PrecheckDetails, openWarnings, type Precheck } from "./Precheck";

const TYPES = [
  { value: "Report", label: "Report" }, { value: "Screenshot", label: "Screenshot" }, { value: "Log", label: "Log" },
  { value: "Approval", label: "Approval" }, { value: "Other", label: "Other" },
];
const MAX_MB = 25;
const fmtSize = (n: number) => (n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`);

export type EvidenceTarget = { kind: "activity" | "readiness"; id: string; label: string };

/**
 * Upload an evidence file (or, for records kept elsewhere, link to them).
 * The file is hashed in the browser and again on the server; a mismatch is rejected.
 */
export function useEvidenceUpload(onDone: (msg: string) => void) {
  const [target, setTarget] = useState<EvidenceTarget | null>(null);
  const node = target ? <EvidenceDialog target={target} onClose={(msg) => { setTarget(null); if (msg) onDone(msg); }} /> : null;
  return { open: setTarget, node };
}

function EvidenceDialog({ target, onClose }: { target: EvidenceTarget; onClose: (msg?: string) => void }) {
  const [mode, setMode] = useState<"file" | "link">("file");
  const [file, setFile] = useState<File | null>(null);
  const [type, setType] = useState("Report");
  const [link, setLink] = useState({ filename: "", storage_reference: "" });
  const [stage, setStage] = useState<"" | "hashing" | "uploading">("");
  const [result, setResult] = useState<{ code: string; name: string; pc: Precheck } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const first = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    first.current?.focus();
    const esc = (e: KeyboardEvent) => e.key === "Escape" && !stage && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose, stage]);

  const base = target.kind === "activity" ? `/test-activities/${target.id}` : `/test-readiness-items/${target.id}`;
  const tooBig = file ? file.size > MAX_MB * 1024 * 1024 : false;
  const ok = mode === "file" ? !!file && !tooBig : !!(link.filename.trim() && link.storage_reference.trim());

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!ok) return;
    setError(null);
    try {
      if (mode === "file" && file) {
        setStage("hashing");
        const digest = await sha256Hex(file);
        const form = new FormData();
        form.append("file", file, file.name);
        form.append("evidence_type", type);
        if (digest) form.append("sha256", digest);
        setStage("uploading");
        const r = await apiUpload(`${base}/evidence/upload`, form);
        if (r?.precheck && openWarnings(r.precheck).length) {
          setResult({ code: r.evidence_code, name: file.name, pc: r.precheck });   // show what to fix before review
          setStage("");
          return;
        }
        onClose(`${file.name} uploaded and sent for review${r?.precheck ? ". Pre-check found no problems." : ""}`);
      } else {
        setStage("uploading");
        await api("POST", `${base}/evidence`, { evidence_type: "ExternalRecord", ...link });
        onClose("Link recorded and sent for review");
      }
    } catch (err) {
      setError(err);
      setStage("");
    }
  }

  if (result) {
    const n = openWarnings(result.pc).length;
    const done = `${result.name} uploaded and sent for review. Pre-check: ${n} thing${n === 1 ? "" : "s"} to check.`;
    return (
      <div className="dialog-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose(done)}>
        <div className="dialog wide" role="dialog" aria-modal="true" aria-labelledby="pc-title">
          <header>
            <h2 id="pc-title">Uploaded as {result.code}. The pre-check found {n} thing{n === 1 ? "" : "s"} to check</h2>
            <p>It has gone to the reviewer as it is. If the file is wrong, upload a corrected one; if a check is mistaken, say so from the Evidence tab so the reviewer sees why.</p>
          </header>
          <div className="body"><PrecheckDetails pc={result.pc} /></div>
          <footer><button type="button" className="btn primary" autoFocus onClick={() => onClose(done)}>Done</button></footer>
        </div>
      </div>
    );
  }

  return (
    <div className="dialog-backdrop" onMouseDown={(e) => e.target === e.currentTarget && !stage && onClose()}>
      <form className="dialog" role="dialog" aria-modal="true" aria-labelledby="ev-title" onSubmit={submit}>
        <header>
          <h2 id="ev-title">Add evidence</h2>
          <p>For {target.label}. A reviewer checks it before it counts toward readiness or closure.</p>
        </header>
        <div className="body">
          <ErrorNotice error={error} />
          <div className="seg" role="group" aria-label="Evidence kind" style={{ marginBottom: 14 }}>
            <button type="button" aria-pressed={mode === "file"} onClick={() => setMode("file")}>Upload a file</button>
            <button type="button" aria-pressed={mode === "link"} onClick={() => setMode("link")}>Link to a record</button>
          </div>
          {mode === "file" ? (
            <>
              <div className="field">
                <label htmlFor="ev-file">File</label>
                <input id="ev-file" ref={first} type="file" className="input"
                  accept=".pdf,.png,.jpg,.jpeg,.gif,.webp,.txt,.log,.csv,.json,.xml,.xlsx,.xls,.docx,.doc,.pptx,.msg,.eml,.zip"
                  onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
                <span className="hint">PDF, images, logs, Office files, email or zip, up to {MAX_MB} MB.</span>
                {file && <span className={`hint ${tooBig ? "overdue" : ""}`}>{file.name}, {fmtSize(file.size)}{tooBig ? `: over the ${MAX_MB} MB limit` : ""}</span>}
              </div>
              <div className="field">
                <label htmlFor="ev-type">What it is</label>
                <select id="ev-type" className="select" value={type} onChange={(e) => setType(e.target.value)}>
                  {TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                </select>
              </div>
              <p className="xs muted">A fingerprint (SHA-256) of the file is taken here and on the server. Every later download is checked against it, so any change to the stored file is detected.</p>
            </>
          ) : (
            <>
              <div className="field"><label htmlFor="ev-name">Record name</label>
                <input id="ev-name" className="input" value={link.filename} onChange={(e) => setLink({ ...link, filename: e.target.value })}
                  placeholder="e.g. Change CHG0041234" /></div>
              <div className="field"><label htmlFor="ev-ref">Link</label>
                <input id="ev-ref" className="input" value={link.storage_reference} onChange={(e) => setLink({ ...link, storage_reference: e.target.value })}
                  placeholder="e.g. a ServiceNow or SharePoint link" />
                <span className="hint">Use this for records that must stay in another system. Links can&apos;t be checksum-verified.</span></div>
            </>
          )}
        </div>
        <footer>
          <button type="button" className="btn" disabled={!!stage} onClick={() => onClose()}>Cancel</button>
          <button type="submit" className="btn primary" disabled={!ok || !!stage}>
            {stage === "hashing" ? "Checking file…" : stage === "uploading" ? "Uploading…" : mode === "file" ? "Upload" : "Add link"}</button>
        </footer>
      </form>
    </div>
  );
}
