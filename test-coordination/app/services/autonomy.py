"""Autonomy policy: when a runbook may run without a person approving that run.

Default: every run needs a person (the activity owner, the test manager, or an agent's accountable person).

The model-risk committee drafts a policy and proposes it; the CISO approves (or rejects) it, with a note, and can
revoke it at any moment, as can the committee. Nobody approves their own proposal. One policy is in force at a time,
and it lapses on its review date. A policy is a list of narrow rules:

    {"name": "Read-only standby checks",
     "integration_id": "...", "job_template_ids": [11],        # exactly which AAP job templates
     "max_criticality": "High",                                 # tests up to this criticality
     "stages": ["Mobilising", "Readiness"],                     # test stages it applies in
     "auto_launch": true,                                       # run when the activity's prerequisites are done
     "agents": ["<agent id>"],                                  # approve these agents' requests automatically
     "max_runs_per_day": 10}                                    # across the organisation, for this rule

Anything no rule covers still needs a person. Every run under the policy records the policy version and rule, so it
traces back to the CISO's approval; the activity's owner and the test manager are told each time.
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import and_, func, insert, select, update

from ..db import t
from ..domain.enums import Role
from ..errors import Conflict, Forbidden, NotFound, Unprocessable
from ..security import Ctx
from . import audit
from .common import now

CRITICALITY = ["Low", "Medium", "High", "Critical"]
STAGES = ["Mobilising", "Readiness", "Ready", "Live"]
VIEWERS = (Role.MODEL_RISK, Role.CISO, Role.ADMIN, Role.RISK, Role.AUDITOR, Role.PROGRAMME_MANAGER)
MAX_REVIEW_DAYS = 366


def _p():
    return t("autonomy_policies")


def _human(ctx: Ctx) -> None:
    if ctx.me.is_agent or ctx.me.is_automation:
        raise Forbidden("Only people govern autonomy")


# ---------------------------------------------------------------------------
# The policy in force, and what it allows
# ---------------------------------------------------------------------------
def in_force(conn) -> dict | None:
    p = _p()
    r = conn.execute(select(p).where(p.c.status == "Approved")).mappings().first()
    if not r:
        return None
    if r["review_by"] < now().date():
        return None                                           # lapsed: back to a person approving every run
    return dict(r)


def permits(conn, *, kind: str, integration_id: UUID, job_template_id: int, event: dict,
            agent_id: UUID | None = None) -> tuple[dict | None, dict | None, str]:
    """(policy, rule, why) for a run without a person's approval. kind is "auto_launch" or "agent_request"."""
    pol = in_force(conn)
    if pol is None:
        return None, None, "No autonomy policy is in force (proposed by the model-risk committee, approved by the CISO)"
    why = "No rule in the autonomy policy covers this"
    for rule in pol["rules"]:
        if str(rule.get("integration_id")) != str(integration_id) or job_template_id not in rule.get("job_template_ids", []):
            continue
        if CRITICALITY.index(event["criticality"]) > CRITICALITY.index(rule["max_criticality"]):
            why = f"The policy allows it only for tests up to {rule['max_criticality']} criticality"
            continue
        if event["lifecycle_status"] not in rule["stages"]:
            why = f"The policy allows it only while a test is {', '.join(rule['stages'])}"
            continue
        if kind == "auto_launch" and not rule.get("auto_launch"):
            why = "The policy doesn't allow this template to start on its own"
            continue
        if kind == "agent_request" and str(agent_id) not in [str(a) for a in rule.get("agents", [])]:
            why = "The policy doesn't let this agent's requests be approved automatically"
            continue
        ru = t("automation_runs")
        used = conn.execute(select(func.count()).where(and_(ru.c.policy_id == pol["id"], ru.c.policy_rule == rule["name"],
                                                            ru.c.launched_at > now() - timedelta(hours=24)))).scalar()
        if used >= rule["max_runs_per_day"]:
            why = f"The rule '{rule['name']}' has reached its limit of {rule['max_runs_per_day']} runs in 24 hours"
            continue
        return pol, rule, "allowed"
    return None, None, why


# ---------------------------------------------------------------------------
# Drafting, proposing, deciding, revoking
# ---------------------------------------------------------------------------
def _validate(ctx: Ctx, rules: list[dict], review_by: date) -> list[dict]:
    if not isinstance(rules, list) or not rules:
        raise Unprocessable("A policy needs at least one rule")
    if review_by <= now().date() or review_by > now().date() + timedelta(days=MAX_REVIEW_DAYS):
        raise Unprocessable("Set a review date within the next 12 months")
    i, ag = t("automation_integrations"), t("agent_identities")
    names, out = set(), []
    for n, r in enumerate(rules, start=1):
        name = str(r.get("name") or "").strip()
        if not name or name in names:
            raise Unprocessable(f"Rule {n} needs a name, different from the other rules")
        names.add(name)
        try:
            integ = UUID(str(r.get("integration_id")))
        except ValueError as e:
            raise Unprocessable(f"Rule '{name}': choose the automation platform") from e
        if not ctx.conn.execute(select(i.c.id).where(and_(i.c.id == integ, i.c.disabled_at.is_(None)))).first():
            raise Unprocessable(f"Rule '{name}': that automation platform isn't available")
        tpls = r.get("job_template_ids") or []
        if not tpls or not all(isinstance(x, int) and x > 0 for x in tpls) or len(tpls) > 50:
            raise Unprocessable(f"Rule '{name}': list the job templates it covers (by number)")
        if r.get("max_criticality") not in CRITICALITY:
            raise Unprocessable(f"Rule '{name}': choose the highest test criticality it covers")
        stages = r.get("stages") or []
        if not stages or any(s not in STAGES for s in stages):
            raise Unprocessable(f"Rule '{name}': choose the test stages it covers ({', '.join(STAGES)})")
        agents = [str(a) for a in (r.get("agents") or [])]
        for a in agents:
            if not ctx.conn.execute(select(ag.c.id).where(ag.c.id == UUID(a))).first():
                raise Unprocessable(f"Rule '{name}': unknown agent")
        auto = bool(r.get("auto_launch"))
        if not auto and not agents:
            raise Unprocessable(f"Rule '{name}' allows nothing: let it start on its own, or name agents whose requests it approves")
        cap = r.get("max_runs_per_day")
        if not isinstance(cap, int) or not 1 <= cap <= 100:
            raise Unprocessable(f"Rule '{name}': set a daily limit between 1 and 100 runs")
        out.append({"name": name[:120], "integration_id": str(integ), "job_template_ids": sorted(set(tpls)),
                    "max_criticality": r["max_criticality"], "stages": [s for s in STAGES if s in stages],
                    "auto_launch": auto, "agents": agents, "max_runs_per_day": cap,
                    "note": str(r.get("note") or "")[:500]})
    return out


def draft(ctx: Ctx, rules: list[dict], rationale: str, review_by: date, policy_id: UUID | None = None) -> dict:
    _human(ctx)
    if not ctx.me.has(Role.MODEL_RISK):
        raise Forbidden("Only the model-risk committee drafts the autonomy policy")
    clean = _validate(ctx, rules, review_by)
    p = _p()
    if policy_id:
        cur = ctx.conn.execute(select(p).where(p.c.id == policy_id).with_for_update()).mappings().first()
        if not cur:
            raise NotFound("Policy not found")
        if cur["status"] != "Draft":
            raise Conflict("Only a draft can be edited; withdraw a proposal to change it")
        new = dict(ctx.conn.execute(update(p).where(p.c.id == policy_id).values(
            rules=clean, rationale=(rationale or "").strip(), review_by=review_by).returning(p)).mappings().one())
        audit.record(ctx, "autonomy.draft_updated", "AutonomyPolicy", policy_id, new={"version": new["version"], "rules": clean})
        return new
    version = (ctx.conn.execute(select(func.max(p.c.version))).scalar() or 0) + 1
    new = dict(ctx.conn.execute(insert(p).values(tenant_id=ctx.me.tenant_id, version=version, rules=clean,
                                                 rationale=(rationale or "").strip(), review_by=review_by,
                                                 drafted_by=ctx.me.user_id).returning(p)).mappings().one())
    audit.record(ctx, "autonomy.drafted", "AutonomyPolicy", new["id"], new={"version": version, "rules": clean})
    return new


def _load(ctx: Ctx, policy_id: UUID) -> dict:
    p = _p()
    r = ctx.conn.execute(select(p).where(p.c.id == policy_id).with_for_update()).mappings().first()
    if not r:
        raise NotFound("Policy not found")
    return dict(r)


def propose(ctx: Ctx, policy_id: UUID) -> dict:
    _human(ctx)
    if not ctx.me.has(Role.MODEL_RISK):
        raise Forbidden("Only the model-risk committee proposes the autonomy policy")
    pol = _load(ctx, policy_id)
    if pol["status"] != "Draft":
        raise Conflict(f"This policy is {pol['status'].lower()}, not a draft")
    if len(pol["rationale"]) < 30:
        raise Unprocessable("Give the committee's rationale (at least a few sentences): why these runs are safe without "
                            "a person approving each one")
    p = _p()
    ctx.conn.execute(update(p).where(and_(p.c.status == "Proposed")).values(status="Withdrawn"))
    new = dict(ctx.conn.execute(update(p).where(p.c.id == policy_id).values(
        status="Proposed", proposed_by=ctx.me.user_id, proposed_at=now()).returning(p)).mappings().one())
    audit.record(ctx, "autonomy.proposed", "AutonomyPolicy", policy_id, new={"version": new["version"], "rationale": new["rationale"]})
    _tell(ctx, Role.CISO, f"Autonomy policy v{new['version']} proposed for your approval",
          f"{ctx.me.display_name} (model-risk committee) proposed it. Review it on the Governance page.")
    return new


def decide(ctx: Ctx, policy_id: UUID, approve: bool, note: str) -> dict:
    _human(ctx)
    if not ctx.me.has(Role.CISO):
        raise Forbidden("Only the CISO approves or rejects the autonomy policy")
    pol = _load(ctx, policy_id)
    if pol["status"] != "Proposed":
        raise Conflict(f"This policy is {pol['status'].lower()}, not awaiting a decision")
    if pol["proposed_by"] == ctx.me.user_id:
        raise Forbidden("You proposed this policy, so someone else must decide it")
    if not (note or "").strip():
        raise Unprocessable("Record the reason for your decision")
    if approve and pol["review_by"] < now().date():
        raise Conflict("Its review date has passed; ask the committee for a new proposal")
    p = _p()
    if approve:
        ctx.conn.execute(update(p).where(p.c.status == "Approved").values(status="Superseded"))
    new = dict(ctx.conn.execute(update(p).where(p.c.id == policy_id).values(
        status="Approved" if approve else "Rejected", decided_by=ctx.me.user_id, decided_at=now(),
        decision_note=note.strip()).returning(p)).mappings().one())
    audit.record(ctx, "autonomy.approved" if approve else "autonomy.rejected", "AutonomyPolicy", policy_id,
                 new={"version": new["version"], "note": note, "rules": new["rules"] if approve else None})
    _tell(ctx, Role.MODEL_RISK, f"Autonomy policy v{new['version']} {'approved' if approve else 'rejected'} by the CISO", note)
    return new


def revoke(ctx: Ctx, policy_id: UUID, reason: str) -> dict:
    _human(ctx)
    if not ctx.me.has(Role.CISO, Role.MODEL_RISK):
        raise Forbidden("Only the CISO or the model-risk committee can revoke the autonomy policy")
    pol = _load(ctx, policy_id)
    if pol["status"] != "Approved":
        raise Conflict("Only the policy in force can be revoked")
    if not (reason or "").strip():
        raise Unprocessable("Say why it's being revoked")
    p = _p()
    new = dict(ctx.conn.execute(update(p).where(p.c.id == policy_id).values(
        status="Revoked", revoked_by=ctx.me.user_id, revoked_at=now(), revoke_reason=reason.strip()).returning(p)).mappings().one())
    audit.record(ctx, "autonomy.revoked", "AutonomyPolicy", policy_id, new={"version": new["version"], "reason": reason})
    _tell(ctx, Role.CISO, f"Autonomy policy v{new['version']} revoked", f"Revoked by {ctx.me.display_name}: {reason}. "
          "Every run now needs a person's approval again.")
    _tell(ctx, Role.MODEL_RISK, f"Autonomy policy v{new['version']} revoked", f"Revoked by {ctx.me.display_name}: {reason}.")
    return new


def withdraw(ctx: Ctx, policy_id: UUID) -> dict:
    """The committee takes a proposal back (to edit it as a new draft)."""
    _human(ctx)
    if not ctx.me.has(Role.MODEL_RISK):
        raise Forbidden("Only the model-risk committee withdraws a proposal")
    pol = _load(ctx, policy_id)
    if pol["status"] != "Proposed":
        raise Conflict("Only a proposal awaiting the CISO can be withdrawn")
    p = _p()
    new = dict(ctx.conn.execute(update(p).where(p.c.id == policy_id).values(status="Draft", proposed_by=None, proposed_at=None)
                                .returning(p)).mappings().one())
    audit.record(ctx, "autonomy.withdrawn", "AutonomyPolicy", policy_id, new={"version": new["version"]})
    return new


def _tell(ctx: Ctx, role: Role, subject: str, body: str) -> None:
    """Policy changes are logged at WARNING for the SIEM (notifications hang off a test, and a policy has none);
    the Governance page shows proposals waiting for the CISO."""
    import logging
    logging.getLogger("resilienceai.autonomy").warning("%s [for %s]: %s", subject, role.value, body)


# ---------------------------------------------------------------------------
# For the Governance page
# ---------------------------------------------------------------------------
def overview(ctx: Ctx) -> dict:
    if not ctx.me.has(*VIEWERS) or ctx.me.is_agent:
        raise Forbidden("The autonomy policy is for the model-risk committee, the CISO, risk, audit and programme management")
    p, u = _p(), t("users")
    names = {r.id: r.display_name for r in ctx.conn.execute(select(u.c.id, u.c.display_name))}
    rows = [dict(r) for r in ctx.conn.execute(select(p).order_by(p.c.version.desc())).mappings()]
    ru = t("automation_runs")
    usage = {(r.policy_id, r.policy_rule): r.n for r in ctx.conn.execute(
        select(ru.c.policy_id, ru.c.policy_rule, func.count().label("n")).where(ru.c.policy_id.isnot(None))
        .group_by(ru.c.policy_id, ru.c.policy_rule))}
    i, ag = t("automation_integrations"), t("agent_identities")
    integ = {str(r.id): r.name for r in ctx.conn.execute(select(i.c.id, i.c.name))}
    agents = {str(r.id): r.name for r in ctx.conn.execute(select(ag.c.id, ag.c.name))}
    for r in rows:
        for k in ("drafted_by", "proposed_by", "decided_by", "revoked_by"):
            r[k + "_name"] = names.get(r[k])
        r["lapsed"] = r["status"] == "Approved" and r["review_by"] < now().date()
        for rule in r["rules"]:
            rule["integration"] = integ.get(rule["integration_id"], "?")
            rule["agent_names"] = [agents.get(a, "?") for a in rule.get("agents", [])]
            rule["runs"] = usage.get((r["id"], rule["name"]), 0)
    force = in_force(ctx.conn)
    return {"in_force": next((r for r in rows if force and r["id"] == force["id"]), None),
            "policies": rows,
            "can_draft": ctx.me.has(Role.MODEL_RISK), "can_decide": ctx.me.has(Role.CISO),
            "can_revoke": ctx.me.has(Role.CISO, Role.MODEL_RISK),
            "integrations": [{"id": k, "name": v} for k, v in integ.items()],
            "agents": [{"id": k, "name": v} for k, v in agents.items()],
            "stages": STAGES, "criticality": CRITICALITY}


def allows_auto_launch(conn, integration_id: UUID, job_template_id: int, criticality: str) -> tuple[bool, str]:
    """When linking: could this template ever start on its own for this test under the policy in force?"""
    pol = in_force(conn)
    if pol is None:
        return False, ("No autonomy policy is in force (proposed by the model-risk committee and approved by the CISO), "
                       "so every run needs a person's approval")
    for rule in pol["rules"]:
        if (str(rule["integration_id"]) == str(integration_id) and job_template_id in rule["job_template_ids"]
                and rule.get("auto_launch") and CRITICALITY.index(criticality) <= CRITICALITY.index(rule["max_criticality"])):
            return True, rule["name"]
    return False, (f"Autonomy policy v{pol['version']} doesn't let this job template start on its own for a "
                   f"{criticality}-criticality test")
