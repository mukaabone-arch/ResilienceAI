"use client";

type Node = { id: string; activity_code: string; title: string; status: string; dependency_state: string; owner_name?: string; is_overdue?: boolean };
type Edge = { predecessor_activity_id: string; successor_activity_id: string; is_mandatory: boolean };

const W = 172, H = 64, GX = 44, GY = 16;
const TONE: Record<string, { stroke: string; fill: string; text: string }> = {
  done: { stroke: "#9cc7ad", fill: "#f1f8f3", text: "var(--ready)" },
  actionable: { stroke: "var(--ready)", fill: "#fff", text: "var(--ready)" },
  at_risk: { stroke: "var(--waiting)", fill: "#fff", text: "var(--waiting)" },
  waiting: { stroke: "var(--line-strong)", fill: "#fff", text: "var(--ink-3)" },
  blocked: { stroke: "var(--blocked)", fill: "var(--blocked-tint)", text: "var(--blocked)" },
};
const LABEL: Record<string, string> = { done: "Done", actionable: "Can start", at_risk: "At risk", waiting: "Waiting", blocked: "Blocked" };

/** Layered left-to-right layout: column = longest path from a root. */
export function DependencyMap({ nodes, edges }: { nodes: Node[]; edges: Edge[] }) {
  if (!nodes.length) return <p className="panel-empty">No activities yet.</p>;
  const preds = new Map<string, string[]>();
  edges.forEach((e) => preds.set(e.successor_activity_id, [...(preds.get(e.successor_activity_id) ?? []), e.predecessor_activity_id]));
  const level = new Map<string, number>();
  const depth = (id: string, seen = new Set<string>()): number => {
    if (level.has(id)) return level.get(id)!;
    if (seen.has(id)) return 0;
    seen.add(id);
    const p = preds.get(id) ?? [];
    const l = p.length ? Math.max(...p.map((x) => depth(x, seen) + 1)) : 0;
    level.set(id, l);
    return l;
  };
  nodes.forEach((n) => depth(n.id));
  const cols: Node[][] = [];
  [...nodes].sort((a, b) => a.activity_code.localeCompare(b.activity_code))
    .forEach((n) => (cols[level.get(n.id)!] ??= []).push(n));
  const pos = new Map<string, { x: number; y: number }>();
  const rows = Math.max(...cols.map((c) => c?.length ?? 0));
  cols.forEach((c, ci) => c?.forEach((n, ri) => {
    const offset = ((rows - c.length) * (H + GY)) / 2;
    pos.set(n.id, { x: ci * (W + GX) + 8, y: offset + ri * (H + GY) + 8 });
  }));
  const width = cols.length * (W + GX) - GX + 16;
  const height = rows * (H + GY) - GY + 16;
  const byId = new Map(nodes.map((n) => [n.id, n]));

  return (
    <div className="depmap">
      <svg width={width} height={height} role="img" aria-label="Activity dependency map">
        <defs>
          <marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0 0L8 4L0 8z" fill="var(--line-strong)" />
          </marker>
          <marker id="arr-b" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0 0L8 4L0 8z" fill="var(--blocked)" />
          </marker>
        </defs>
        {edges.map((e) => {
          const a = pos.get(e.predecessor_activity_id), b = pos.get(e.successor_activity_id);
          if (!a || !b) return null;
          const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x - 2, y2 = b.y + H / 2, mx = (x1 + x2) / 2;
          const hot = byId.get(e.predecessor_activity_id)?.dependency_state === "blocked";
          return (
            <path key={e.predecessor_activity_id + e.successor_activity_id}
              d={`M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}`} fill="none"
              stroke={hot ? "var(--blocked)" : "var(--line-strong)"} strokeWidth={hot ? 2 : 1.5}
              strokeDasharray={e.is_mandatory ? undefined : "4 4"} markerEnd={`url(#${hot ? "arr-b" : "arr"})`} />
          );
        })}
        {nodes.map((n) => {
          const p = pos.get(n.id)!;
          const upstream = n.dependency_state === "blocked" && n.status !== "Blocked";
          const tone = upstream ? { stroke: "var(--blocked)", fill: "#fff", text: "var(--blocked)" } : TONE[n.dependency_state] ?? TONE.waiting;
          return (
            <g key={n.id} transform={`translate(${p.x} ${p.y})`}>
              <title>{`${n.activity_code} ${n.title} — ${n.status}`}</title>
              <rect width={W} height={H} rx="6" fill={tone.fill} stroke={tone.stroke} strokeWidth={n.status === "Blocked" ? 2 : 1.2} strokeDasharray={upstream ? "5 3" : undefined} />
              <text x="12" y="20" fontSize="12" fontWeight="700" fill="var(--ink-2)">{n.activity_code}</text>
              <text x={W - 12} y="20" fontSize="11" fontWeight="600" fill={tone.text} textAnchor="end">
                {n.is_overdue && n.dependency_state !== "done" ? "Overdue" : upstream ? "Held upstream" : n.status === "In Progress" ? "In progress" : n.status === "Submitted" ? "Submitted" : LABEL[n.dependency_state]}
              </text>
              <text x="12" y="38" fontSize="12.5" fontWeight="600" fill="var(--ink)">
                {n.title.length > 23 ? n.title.slice(0, 22) + "…" : n.title}
              </text>
              <text x="12" y="54" fontSize="11" fill="var(--ink-3)">{n.owner_name ?? "Unassigned"}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
