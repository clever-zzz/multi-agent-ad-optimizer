"""Critic Agent - reconcile conflicting proposals before a human sees them.

The supervisor pattern only earns its complexity if something owns the global
view. The optimizer fires one proposal per rule, so a campaign that breaches its
CPA ceiling and its burn rate in the same window receives both `pause_campaign`
and `adjust_budget`, and an operator is asked to approve two mutually exclusive
outcomes for one campaign. Multi-iteration runs compound this: the actions
channel accumulates, so the same (campaign, creative, type) proposal reappears
once per iteration under a fresh id.

The critic never invents an action and never executes one. It only decides which
of the proposals already on the table survive, and records why the others were
withheld. Since the optimizer now rehearses every write proposal as a tool-layer
dry run, one more class of proposal can be set aside before a human sees it: the
one the platform already refused. That verdict is attached to the proposal itself
and the critic honours it, so the approval queue only ever holds changes that
could actually be applied. Suppression is a mark rather than a deletion for two reasons: the
`optimization_actions` channel uses the append_list reducer, so no node can
rewrite history, and keeping the record is what lets an operator disagree with
the critic and approve a suppressed proposal anyway. That second promise only
holds because the mark is persisted - the run's graph state dies with the
process, so `_finalise` writes every proposal, withheld ones carrying status
`suppressed`, next to a `critic_findings` row holding the reason. Without those
rows the escape hatch existed only inside the run that created it, which is not
an escape hatch at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.logging import get_logger
from ..domain.enums import ActionType, AgentName, AlertSeverity
from ..orchestrator.state import AgentState
from .base import AgentContext, BaseAgent

logger = get_logger(__name__)

# Proposals that change how much a campaign spends. None of them can coexist
# with a proposal to stop the campaign outright.
SPEND_ACTIONS = frozenset({ActionType.ADJUST_BUDGET.value, ActionType.ADJUST_BID.value})

# (dominant, contradicted, finding kind) at campaign granularity. The dominant
# remedy wins unless a contradicted proposal carries strictly more confidence.
CAMPAIGN_CONFLICTS: tuple[tuple[str, frozenset[str], str], ...] = (
    (ActionType.PAUSE_CAMPAIGN.value, SPEND_ACTIONS, "pause_overrides_spend"),
    (
        ActionType.PAUSE_CAMPAIGN.value,
        frozenset({ActionType.RESUME_CAMPAIGN.value}),
        "campaign_pause_resume_conflict",
    ),
    (
        ActionType.PAUSE_CAMPAIGN.value,
        frozenset({ActionType.START_AB_TEST.value}),
        "experiment_on_paused_campaign",
    ),
)

# Creative granularity. Applied only when both proposals name the same concrete
# creative: a campaign-level `refresh_creative` carries no creative_id and works
# at a different scope, so it does not contradict pausing one specific asset.
CREATIVE_CONFLICTS: tuple[tuple[str, frozenset[str], str], ...] = (
    (
        ActionType.PAUSE_CREATIVE.value,
        frozenset({ActionType.RESUME_CREATIVE.value}),
        "creative_pause_resume_conflict",
    ),
    (
        ActionType.PAUSE_CREATIVE.value,
        frozenset({ActionType.REFRESH_CREATIVE.value}),
        "pause_overrides_refresh",
    ),
)

ConflictRule = tuple[str, frozenset[str], str]

# Confidence is not one scale, and treating it as one is how a well-delivered
# campaign talks its way out of a critical burn-rate pause. `recommend_bid`
# reports how much delivery evidence backs a price (0.35 + 0.45 * evidence +
# 0.2, clamped to 0.98), so any campaign over 50k impressions scores ~0.98.
# An alert-derived proposal instead reports how severe the anomaly was (0.9
# critical, 0.7 warning). A critical anomaly therefore outranks any amount of
# delivery evidence - but only once it is clearly past its threshold, which is
# what DOMINANCE_MARGIN below measures. Confidence separates proposals that
# carry the same standing, including two that are both critical.
CRITICAL = AlertSeverity.CRITICAL.value


def _action_id(action: dict[str, Any]) -> str:
    return str(action.get("id") or "")


def _action_type(action: dict[str, Any]) -> str:
    return str(action.get("action_type") or "")


def _confidence(action: dict[str, Any]) -> float:
    try:
        return float(action.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _iteration_of(action: dict[str, Any]) -> int:
    try:
        return int(action.get("iteration", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _direction(action: dict[str, Any]) -> str:
    """Which way a proposal moves spend: "increase", "decrease" or ""."""
    return str(action.get("direction") or "")


def _reference(action: dict[str, Any]) -> str:
    """The frame a spend proposal was judged against, when it recorded one."""
    basis = action.get("basis")
    return str(basis.get("reference") or "") if isinstance(basis, dict) else ""


def _severity_rank(action: dict[str, Any]) -> int:
    """1 when a fired critical anomaly backs this proposal, else 0."""
    return 1 if str(action.get("severity") or "") == CRITICAL else 0


def _rank(action: dict[str, Any]) -> tuple[int, float]:
    """Ordering key: severity first, confidence only as the tie-break."""
    return (_severity_rank(action), _confidence(action))


# A rule calls itself critical once it is this far past its threshold; the
# detector's bands are cut from the same number.
CRITICAL_GAP = 0.5
# How much further past that line an anomaly has to be before the critic lets it
# outrank evidence outright. Without a margin, "critical" is a cliff: 1.875 of a
# daily budget gets absolute precedence while 1.857 - the same situation to a
# business - can be overruled by a confidence score. With it, a marginal
# critical competes on the evidence like anything else, and only a genuinely
# runaway one is undebatable.
DOMINANCE_MARGIN = 0.10


def _urgency(action: dict[str, Any]) -> float:
    """How far past its threshold the backing anomaly is.

    Falls back to a full-margin 1.0 for a critical that carries only the
    three-valued severity, so a proposal built without the continuous figure is
    still treated as clearly critical rather than silently demoted.
    """
    raw = action.get("urgency")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return 1.0 if _severity_rank(action) else 0.0


def _dominates(action: dict[str, Any]) -> bool:
    """Whether this proposal is clearly critical enough to outrank evidence."""
    return _severity_rank(action) == 1 and _urgency(action) >= CRITICAL_GAP + DOMINANCE_MARGIN


def _outranks(challenger: dict[str, Any], keeper: dict[str, Any]) -> bool:
    """Whether the challenger strictly beats the keeper.

    Strict on purpose: equal standing resolves toward the dominant remedy (the
    keeper), which is what keeps "stopping spend wins a tie" intact.
    """
    challenger_dominates = _dominates(challenger)
    keeper_dominates = _dominates(keeper)
    if challenger_dominates != keeper_dominates:
        return challenger_dominates
    return _confidence(challenger) > _confidence(keeper)


def _standing(action: dict[str, Any]) -> str:
    """Why a proposal won, phrased for the audit trail."""
    backing = "critical alert, confidence " if _severity_rank(action) else "confidence "
    return _label(action) + " (" + backing + format(_confidence(action), ".2f") + ")"


def _label(action: dict[str, Any]) -> str:
    return _action_type(action).replace("_", " ")


def _describe(campaign_id: str, creative_id: str) -> str:
    if creative_id:
        return "campaign " + campaign_id + " / creative " + creative_id
    return "campaign " + campaign_id


def _join(actions: list[dict[str, Any]]) -> str:
    return ", ".join(sorted({_label(action) for action in actions}))


def _preflight(action: dict[str, Any]) -> dict[str, Any]:
    """The optimizer's dry-run verdict, empty when the proposal was never rehearsed."""
    raw = action.get("preflight")
    return raw if isinstance(raw, dict) else {}


def _is_unexecutable(action: dict[str, Any]) -> bool:
    return bool(_preflight(action).get("blocking"))


def _verdict(preflight: dict[str, Any]) -> str:
    """Phrase a refusal for the audit trail, keeping the network's own error text."""
    phrase = (
        str(preflight.get("tool") or "the tool layer")
        + " answered "
        + str(preflight.get("outcome") or "refused")
    )
    error = str(preflight.get("error") or "")
    return phrase + " (" + error + ")" if error else phrase


def _brief(action: dict[str, Any]) -> dict[str, Any]:
    """The part of a withheld proposal an operator needs in order to overrule."""
    return {
        "id": _action_id(action),
        "action_type": _action_type(action),
        "confidence": _confidence(action),
        "severity": str(action.get("severity") or ""),
        "direction": _direction(action),
        "iteration": _iteration_of(action),
        "reason": str(action.get("reason") or "")[:300],
        "preflight": _preflight(action) or None,
    }


@dataclass(frozen=True, slots=True)
class Finding:
    """One reconciliation verdict, retained so the decision stays auditable."""

    kind: str
    scope: str
    campaign_id: str
    creative_id: str
    kept_action_id: str
    kept_action_type: str
    kept_confidence: float
    suppressed: tuple[dict[str, Any], ...]
    reason: str
    iteration: int
    # True when the critic refused to pick a winner and handed the decision to a
    # human instead. An escalating finding suppresses nothing; it flags a
    # conflict the critic cannot settle on evidence alone.
    escalate: bool = False

    @property
    def suppressed_action_ids(self) -> tuple[str, ...]:
        return tuple(str(item.get("id") or "") for item in self.suppressed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scope": self.scope,
            "campaign_id": self.campaign_id,
            "creative_id": self.creative_id,
            "kept_action_id": self.kept_action_id,
            "kept_action_type": self.kept_action_type,
            "kept_confidence": self.kept_confidence,
            "suppressed_actions": list(self.suppressed),
            "suppressed_action_ids": list(self.suppressed_action_ids),
            "reason": self.reason,
            "iteration": self.iteration,
            "escalate": self.escalate,
        }


class CriticAgent(BaseAgent):
    """Reduces the proposal set to one defensible action per intent."""

    name = AgentName.CRITIC

    async def run(self, state: AgentState, context: AgentContext) -> dict[str, Any]:
        iteration = int(state.get("iteration", 0) or 0)
        actions = [a for a in (state.get("optimization_actions") or []) if isinstance(a, dict)]

        if not actions:
            message = self._message("No proposals to review this iteration.", iteration=iteration)
            return {
                "critic_findings": [],
                "current_agent": self.name.value,
                "agent_messages": [message],
                "_summary": {"proposals": 0, "suppressed": 0, "surviving": 0, "by_kind": {}},
            }

        suppressed: set[str] = set()
        findings: list[Finding] = list(self._unexecutable(actions, suppressed, iteration))
        findings.extend(self._repeats(actions, suppressed, iteration))
        findings.extend(self._conflicts(actions, suppressed, iteration, CAMPAIGN_CONFLICTS, False))
        findings.extend(self._conflicts(actions, suppressed, iteration, CREATIVE_CONFLICTS, True))
        findings.extend(self._spend_direction(actions, suppressed, iteration))

        surviving = len(actions) - len(suppressed)
        by_kind = self._by_kind(findings)
        message = self._build_message(len(actions), findings, suppressed, surviving, iteration)

        logger.info(
            "critic_completed",
            run_id=context.run_id,
            proposals=len(actions),
            suppressed=len(suppressed),
            surviving=surviving,
            findings=len(findings),
        )

        return {
            "critic_findings": [finding.to_dict() for finding in findings],
            "current_agent": self.name.value,
            "agent_messages": [message],
            "_summary": {
                "proposals": len(actions),
                "suppressed": len(suppressed),
                "surviving": surviving,
                "by_kind": by_kind,
            },
        }

    def _unexecutable(
        self,
        actions: list[dict[str, Any]],
        suppressed: set[str],
        iteration: int,
    ) -> list[Finding]:
        """Withhold proposals the platform already refused during the dry run.

        This runs ahead of the conflict tables deliberately. A proposal the
        network would reject is not a reason to withhold one it would accept, so
        the unexecutable ones leave the pool first and every later verdict is
        decided among proposals that could actually be applied. Unlike the other
        rules there is no keeper: nothing survives, which is recorded as
        kept_action_type "none" so an operator can still see what was proposed.
        """
        findings: list[Finding] = []
        for action in actions:
            if not _is_unexecutable(action):
                continue
            preflight = _preflight(action)
            campaign_id = str(action.get("campaign_id") or "")
            creative_id = str(action.get("creative_id") or "")
            suppressed.add(_action_id(action))
            findings.append(
                Finding(
                    kind="unexecutable_proposal",
                    scope="creative" if creative_id else "campaign",
                    campaign_id=campaign_id,
                    creative_id=creative_id,
                    kept_action_id="",
                    kept_action_type="none",
                    kept_confidence=0.0,
                    suppressed=(_brief(action),),
                    reason=(
                        _label(action)
                        + " on "
                        + _describe(campaign_id, creative_id)
                        + " was rehearsed before approval and "
                        + _verdict(preflight)
                        + ". Withheld rather than queued: approving it could only"
                        + " fail, and the operator's decision is worth more than"
                        + " that. Nothing was changed by the rehearsal."
                    ),
                    iteration=iteration,
                )
            )
        return findings

    def _repeats(
        self,
        actions: list[dict[str, Any]],
        suppressed: set[str],
        iteration: int,
    ) -> list[Finding]:
        """Collapse the same proposal raised once per iteration into one.

        Accumulation is deliberate on the actions channel - findings from every
        iteration must survive - but an operator should still decide a given
        (campaign, creative, type) intent once, not once per loop.
        """
        groups: dict[tuple[str, str, str], list[tuple[int, dict[str, Any]]]] = {}
        for index, action in enumerate(actions):
            if _action_id(action) in suppressed:
                continue
            key = (
                str(action.get("campaign_id", "")),
                str(action.get("creative_id") or ""),
                _action_type(action),
            )
            groups.setdefault(key, []).append((index, action))

        findings: list[Finding] = []
        for (campaign_id, creative_id, action_type), members in groups.items():
            if len(members) < 2:
                continue
            ranked = sorted(
                members,
                key=lambda item: (_rank(item[1]), _iteration_of(item[1]), -item[0]),
                reverse=True,
            )
            keeper = ranked[0][1]
            losers = [member[1] for member in ranked[1:]]
            for loser in losers:
                suppressed.add(_action_id(loser))
            findings.append(
                Finding(
                    kind="duplicate_proposal",
                    scope="creative" if creative_id else "campaign",
                    campaign_id=campaign_id,
                    creative_id=creative_id,
                    kept_action_id=_action_id(keeper),
                    kept_action_type=action_type,
                    kept_confidence=_confidence(keeper),
                    suppressed=tuple(_brief(loser) for loser in losers),
                    reason=(
                        action_type.replace("_", " ")
                        + " was proposed "
                        + str(len(members))
                        + " times for "
                        + _describe(campaign_id, creative_id)
                        + " across iterations. Kept the strongest proposal ("
                        + format(_confidence(keeper), ".2f")
                        + ") so it only has to be decided once."
                    ),
                    iteration=iteration,
                )
            )
        return findings

    def _conflicts(
        self,
        actions: list[dict[str, Any]],
        suppressed: set[str],
        iteration: int,
        rules: tuple[ConflictRule, ...],
        by_creative: bool,
    ) -> list[Finding]:
        """Apply one conflict table, either per campaign or per creative."""
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for action in actions:
            if _action_id(action) in suppressed:
                continue
            creative_id = str(action.get("creative_id") or "")
            if by_creative and not creative_id:
                continue
            key = (str(action.get("campaign_id", "")), creative_id)
            groups.setdefault(key, []).append(action)

        findings: list[Finding] = []
        for (campaign_id, creative_id), group in groups.items():
            for dominant, contradicted, kind in rules:
                finding = self._resolve(
                    group,
                    dominant=dominant,
                    contradicted=contradicted,
                    kind=kind,
                    campaign_id=campaign_id,
                    creative_id=creative_id,
                    suppressed=suppressed,
                    iteration=iteration,
                )
                if finding is not None:
                    findings.append(finding)
        return findings

    def _spend_direction(
        self,
        actions: list[dict[str, Any]],
        suppressed: set[str],
        iteration: int,
    ) -> list[Finding]:
        """Flag two spend proposals that pull one campaign in opposite directions.

        This is deliberately not another conflict table. The tables above encode
        "these two cannot both be applied", and the critic can settle those
        because stopping spend beats continuing it. A bid raise and a budget cut
        are different: each is defensible against its own reference frame (the
        campaign's target versus the portfolio average), so neither dominates and
        picking one would be the critic inventing a policy the operators never
        agreed to. It escalates instead - both proposals stay in the queue, and
        the finding says why a human has to choose.

        Same-direction proposals are left alone: a bid raise and a budget raise
        are one coherent intent, and a conflict rule here would break that.
        """
        groups: dict[str, list[dict[str, Any]]] = {}
        for action in actions:
            if _action_id(action) in suppressed:
                continue
            if _action_type(action) not in SPEND_ACTIONS:
                continue
            groups.setdefault(str(action.get("campaign_id", "")), []).append(action)

        findings: list[Finding] = []
        for campaign_id, group in groups.items():
            rises = [a for a in group if _direction(a) == "increase"]
            cuts = [a for a in group if _direction(a) == "decrease"]
            if not rises or not cuts:
                continue
            references = sorted({_reference(a) for a in group if _reference(a)})
            frame = " vs ".join(references) if references else "different reference frames"
            findings.append(
                Finding(
                    kind="opposing_spend_intent",
                    scope="campaign",
                    campaign_id=campaign_id,
                    creative_id="",
                    kept_action_id="",
                    kept_action_type="none",
                    kept_confidence=0.0,
                    # Nothing is suppressed: both proposals remain decidable and
                    # the operator, not the critic, settles the contradiction.
                    suppressed=(),
                    escalate=True,
                    reason=(
                        _describe(campaign_id, "")
                        + " received opposing spend proposals: "
                        + _join(rises)
                        + " against "
                        + _join(cuts)
                        + ". They were decided against "
                        + frame
                        + ", so neither outranks the other on evidence and both"
                        + " were left in the queue. Choose one."
                    ),
                    iteration=iteration,
                )
            )
        return findings

    @staticmethod
    def _resolve(
        group: list[dict[str, Any]],
        *,
        dominant: str,
        contradicted: frozenset[str],
        kind: str,
        campaign_id: str,
        creative_id: str,
        suppressed: set[str],
        iteration: int,
    ) -> Finding | None:
        """Pick a winner between two proposals that cannot both be applied."""
        live = [action for action in group if _action_id(action) not in suppressed]
        strong = [action for action in live if _action_type(action) == dominant]
        weak = [action for action in live if _action_type(action) in contradicted]
        if not strong or not weak:
            return None

        keeper = max(strong, key=_rank)
        challenger = max(weak, key=_rank)
        target = _describe(campaign_id, creative_id)

        if _outranks(challenger, keeper):
            losers = strong
            winner = challenger
            reason = (
                _standing(challenger)
                + " outweighs "
                + _standing(keeper)
                + " on "
                + target
                + ", so the contradictory "
                + _join(losers)
                + " proposal was withheld."
            )
        else:
            losers = weak
            winner = keeper
            reason = (
                _standing(keeper)
                + " supersedes "
                + _join(losers)
                + " on "
                + target
                + ": the two cannot both be applied. A clearly critical anomaly"
                + " outranks delivery evidence, and equal standing resolves"
                + " toward stopping spend."
            )

        for loser in losers:
            suppressed.add(_action_id(loser))

        return Finding(
            kind=kind,
            scope="creative" if creative_id else "campaign",
            campaign_id=campaign_id,
            creative_id=creative_id,
            kept_action_id=_action_id(winner),
            kept_action_type=_action_type(winner),
            kept_confidence=_confidence(winner),
            suppressed=tuple(_brief(loser) for loser in losers),
            reason=reason,
            iteration=iteration,
        )

    @staticmethod
    def _by_kind(findings: list[Finding]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in findings:
            counts[finding.kind] = counts.get(finding.kind, 0) + len(finding.suppressed)
        return counts

    def _build_message(
        self,
        proposals: int,
        findings: list[Finding],
        suppressed: set[str],
        surviving: int,
        iteration: int,
    ) -> dict[str, Any]:
        if not findings:
            return self._message(
                "Reviewed " + str(proposals) + " proposal(s); no conflicts found.",
                iteration=iteration,
            )
        detail = ", ".join(
            str(count) + " " + kind.replace("_", " ")
            for kind, count in sorted(self._by_kind(findings).items())
        )
        escalated = sum(1 for finding in findings if finding.escalate)
        # An escalating verdict withholds nothing, so it would be invisible in
        # the withheld-by-kind breakdown above. It still has to be said out loud:
        # it is the one case where the run is waiting on a person.
        tail = " " + str(escalated) + " conflict(s) need a human decision." if escalated else ""
        return self._message(
            "Reviewed "
            + str(proposals)
            + " proposal(s) and withheld "
            + str(len(suppressed))
            + " ("
            + detail
            + "). "
            + str(surviving)
            + " remain for approval."
            + tail,
            iteration=iteration,
        )
