"use client";

import { api } from "@/lib/api";
import { useEvidenceUpload } from "./EvidenceUpload";
import { useDialog } from "./ui";

/**
 * Actions an activity owner (or reviewer) can take, matching the backend workflow:
 * start → in progress → submit (complete, or wait for review); blocked needs a reason.
 */
export function ActivityActions({ a, meId, canManage, run }: {
  a: any; meId?: string; canManage?: boolean; run: (fn: () => Promise<unknown>, ok?: string) => void;
}) {
  const { ask, node } = useDialog();
  const upload = useEvidenceUpload((msg) => run(async () => undefined, msg));
  const isOwner = a.owner_user_id ? a.owner_user_id === meId : true; // workspace rows are always mine
  const canAct = isOwner || canManage;
  const isReviewer = a.reviewer_user_id && a.reviewer_user_id === meId;
  const patch = (body: object, ok: string) => run(() => api("PATCH", `/test-activities/${a.id}`, body), ok);
  const dep = a.dependency_state;

  async function block() {
    const v = await ask({ title: `Report a blocker on ${a.activity_code}`, confirm: "Report blocker", danger: true,
      description: "Owners of dependent activities and the test manager are notified.",
      fields: [{ name: "reason", label: "What is blocking this?", kind: "textarea", required: true }] });
    if (v) patch({ status: "Blocked", blocked_reason: v.reason }, `${a.activity_code} reported as blocked`);
  }
  function evidence() {
    upload.open({ kind: "activity", id: a.id, label: `${a.activity_code} ${a.title}` });
  }
  async function review(accept: boolean) {
    const v = accept ? {} : await ask({ title: `Send ${a.activity_code} back`, confirm: "Send back", danger: true,
      fields: [{ name: "comment", label: "What needs to change?", kind: "textarea", required: true }] });
    if (v) run(() => api("POST", `/test-activities/${a.id}/review`, { accept, comment: (v as any).comment }),
      accept ? `${a.activity_code} accepted` : `${a.activity_code} sent back to the owner`);
  }

  const buttons: React.ReactNode[] = [];
  if (isReviewer && a.status === "Submitted") {
    buttons.push(<button key="acc" className="btn primary" onClick={() => review(true)}>Accept</button>);
    buttons.push(<button key="rej" className="btn" onClick={() => review(false)}>Send back</button>);
  }
  if (canAct) {
    if (a.status === "Not Started") {
      const blockedUp = dep === "waiting" || dep === "blocked";
      buttons.push(<button key="s" className="btn" disabled={blockedUp}
        title={blockedUp ? "A mandatory prerequisite isn't complete yet" : undefined}
        onClick={() => patch({ status: "In Progress" }, `${a.activity_code} started`)}>Start</button>);
    }
    if (a.status === "In Progress") {
      buttons.push(<button key="d" className="btn primary"
        onClick={() => patch({ status: "Submitted" }, a.reviewer_user_id ? `${a.activity_code} sent for review` : `${a.activity_code} done`)}>
        {a.reviewer_user_id ? "Submit for review" : "Mark done"}</button>);
    }
    if (a.status === "Blocked") {
      buttons.push(<button key="u" className="btn" onClick={() => patch({ status: "In Progress" }, `${a.activity_code} unblocked`)}>Unblock</button>);
    }
    if (a.evidence_required && ["In Progress", "Submitted", "Completed"].includes(a.status)) {
      buttons.push(<button key="e" className="btn quiet" onClick={evidence}>Add evidence</button>);
    }
    if (["Not Started", "In Progress"].includes(a.status)) {
      buttons.push(<button key="b" className="btn quiet" onClick={block}>Report blocker</button>);
    }
  }
  return <div className="task-actions">{buttons}{node}{upload.node}</div>;
}
