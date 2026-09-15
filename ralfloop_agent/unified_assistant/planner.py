from __future__ import annotations

import hashlib
import re

from .contracts import AssistantPlan, PlanAssignment, PolicyClass
from .email_search import is_email_search_request, plan_email_search
from .registry import UnifiedRegistryFacade


_EMAIL_RE = re.compile(
    r"\b(?:scrivi\s+(?:una\s+mail\s+)?a|prepara\s+(?:una\s+)?(?:mail|email)|"
    r"manda\s+(?:una\s+)?(?:mail|email)|rispondi\s+(?:alla|a\s+questa)\s+mail|"
    r"rispondi\s+a\s+[\wÀ-ÿ][\wÀ-ÿ'. -]{0,80})\b",
    re.I,
)
_HOME_RE = re.compile(
    r"\b(?:accendi|spegni|apri|chiudi|imposta|metti|porta|abbassala|alzala|"
    r"temperatura|quanto\s+fa|fa\s+caldo|fa\s+freddo)\b",
    re.I,
)
_GRANT_RE = re.compile(r"\b(?:band[oi]|grant|contribut[oi]|finanziament[oi]|candidatur[ae])\b", re.I)
_TIREMM_RE = re.compile(r"\b(?:tiremm|associazione|aps|partner|progetto)\b", re.I)
_RELATIONAL_RE = re.compile(r"\b(?:rsc|abc|relazional[ei]|formula\s+loop)\b", re.I)
_INFRA_RE = re.compile(r"\b(?:agentcpm|servizi[oa]?|spazio\s+libero|disco|server|amule)\b", re.I)
_CODE_RE = re.compile(r"\b(?:codice|repository|repo|bug|debug|test|stacktrace)\b", re.I)
_DOCUMENT_RE = re.compile(r"\b(?:pdf|document[oi]|allegat[oi]|estrai)\b", re.I)
_RESEARCH_RE = re.compile(r"\b(?:ricerca|cerca\s+sul\s+web|fonti|deep\s+research)\b", re.I)
_EDITORIAL_RE = re.compile(r"\b(?:volantin[oi]|flyer|locandin[ae]|manifest[oi]|poster)\b", re.I)
_MEDIA_RE = re.compile(r"\b(?:video|audio|immagine|ffmpeg|sottotitol[oi])\b", re.I)
_ATM_RE = re.compile(
    r"\b(?:atm|giromilano|mezzi\s+pubblici|trasporto\s+pubblico)\b"
    r"|\bcome\s+(?:arrivo|vado|posso\s+andare)\b"
    r"|\bportami\s+(?:a|al|alla|all['’]|in)\b"
    r"|\bmezzi\s+(?:per|verso)\b"
    r"|\bpercorso\s+(?:atm|con\s+i\s+mezzi)\b",
    re.I,
)

_METEO_RE = re.compile(
    r"\b(?:meteo|weather|previsioni(?:\s+meteo)?|piove|piover[àa]|pioggia|"
    r"precipitazioni?|temporale|temporali|radar|vento|raffiche|"
    r"che\s+tempo\s+fa|tempo\s+fa)\b"
    r"|\btemperatura\s+(?:a|in|per)\s+",
    re.I,
)

_BYPASS_RE = re.compile(r"\b(?:ignore previous|ignora (?:le )?regole|bypass|esegui shell)\b", re.I)
_FASTWEB_RE = re.compile(r"\b(?:fastweb|myfastpage)\b", re.I)
_FASTWEB_MUTATION_RE = re.compile(
    r"\b(?:accetta|attiva|cambia|modifica|disdici|disdetta|paga|invia|conferma)\b.*"
    r"\b(?:offerta|contratto|piano|pagamento|iban|metodo|fastweb)\b|"
    r"\b(?:offerta|contratto|piano|pagamento|iban|metodo)\b.*\b(?:accetta|attiva|cambia|modifica|disdici|paga)\b",
    re.I,
)
_FASTWEB_COMPARE_RE = re.compile(
    r"\b(?:confronta|paragona|corrisponde|avvisat[oi])\b.*\b(?:canone|aumento|mail|myfastpage|paghiamo)\b|"
    r"\b(?:mail|comunicat[oa]|avvisat[oi])\b.*\b(?:canone attuale|paghiamo ora|myfastpage)\b",
    re.I,
)
_FASTWEB_PORTAL_RE = re.compile(
    r"\b(?:quanto\s+paghiamo|canone\s+attuale|offerta\s+attiva|che\s+offerta|"
    r"myfastpage|fattur[ae]|decorrenza|entrato\s+in\s+vigore|controlla\s+fastweb)\b",
    re.I,
)
_MAILCHIMP_RE = re.compile(
    r"\bmailchimp\b",
    re.I,
)
_MAILCHIMP_MUTATION_RE = re.compile(
    r"\b(?:invia|manda|send|crea|modifica|aggiorna|elimina|programma|schedula|"
    r"cancella|aggiungi|rimuovi|iscrivi|disiscrivi|subscribe|unsubscribe|"
    r"post|put|patch|delete)\b",
    re.I,
)
_MAILCHIMP_CREATE_CAMPAIGN_RE = re.compile(
    r"\b(?:crea|prepara)\b.*\bcampagna\b.*\bmailchimp\b|"
    r"\bmailchimp\b.*\b(?:crea|prepara)\b.*\bcampagna\b", re.I,
)
_MAILCHIMP_SEND_CAMPAIGN_RE = re.compile(
    r"\b(?:invia|send)\b.*\bcampagna\b.*\bmailchimp\b|"
    r"\bmailchimp\b.*\b(?:invia|send)\b.*\bcampagna\b", re.I,
)
_MAILCHIMP_SUBSCRIBE_MEMBER_RE = re.compile(
    r"\b(?:aggiungi|iscrivi|subscribe)\b.*\b(?:mailchimp|mailing\s+list|newsletter|audience)\b|"
    r"\b(?:mailchimp|mailing\s+list|newsletter|audience)\b.*\b(?:aggiungi|iscrivi|subscribe)\b",
    re.I,
)
_MAILCHIMP_LIST_ALIAS_RE = re.compile(r"\b(?:mailing\s+list|newsletter)\b", re.I)
_MAILCHIMP_EMAIL_RE = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}\b", re.I
)
_MAILCHIMP_AUDIENCE_RE = re.compile(
    r"\b(?:audience|audiences|liste?|pubblico|contatti)\b",
    re.I,
)
_MAILCHIMP_MEMBERS_RE = re.compile(r"\b(?:membri|iscritti|contatti)\b", re.I)
_MAILCHIMP_SEGMENTS_RE = re.compile(r"\bsegment[oi]\b", re.I)
_MAILCHIMP_TAGS_RE = re.compile(r"\btag\b", re.I)
_MAILCHIMP_ANALYSIS_RE = re.compile(
    r"\b(?:chi\s+abbiamo|potrebbe\s+essere\s+interessat|analizza)\b", re.I
)
_MAILCHIMP_LIST_ID_RE = re.compile(
    r"\blist[_ -]?id\s*[:=]?\s*(?P<list_id>[A-Za-z0-9_-]{1,128})\b",
    re.I,
)
_MAILCHIMP_PING_RE = re.compile(
    r"\b(?:ping|stato|health|connessione|funziona|disponibile)\b",
    re.I,
)

_WHATSAPP_RE = re.compile(r"\b(?:whatsapp|whatsapp\s+web|wapp)\b", re.I)
_WHATSAPP_COMPOSE_RE = re.compile(
    r"\b(?:scrivi|manda|invia)\s+(?:un\s+messaggio\s+)?(?:su\s+)?(?:whatsapp|wapp)\s+a\s+"
    r"(?P<target>[\wÀ-ÿ][\wÀ-ÿ'. -]{0,80}?)(?=\s+(?:che|dicendo|per\s+dire)\b|[,:.!?]|$)|"
    r"\b(?:whatsapp|wapp)\s*:\s*(?:scrivi|manda|invia)\s+a\s+"
    r"(?P<target2>[\wÀ-ÿ][\wÀ-ÿ'. -]{0,80}?)(?=\s+(?:che|dicendo|per\s+dire)\b|[,:.!?]|$)",
    re.I,
)
_WHATSAPP_REPLY_RE = re.compile(
    r"\b(?:rispondi)\s+(?:su\s+)?(?:whatsapp|wapp)\s+a\s+"
    r"(?P<target>[\wÀ-ÿ][\wÀ-ÿ'. -]{0,80}?)(?=\s+(?:che|dicendo)\b|[,:.!?]|$)",
    re.I,
)
_WHATSAPP_DENIED_RE = re.compile(
    r"\b(?:cancella|elimina|modifica|inoltra|reagisci|archivia|silenzia|blocca|"
    r"chiama|crea\s+gruppo|aggiungi\s+partecipant|logout|esci)\b",
    re.I,
)
_MULTISOURCE_WHATSAPP_REPLY_RE = re.compile(
    r"(?=.*\b(?:mail|email|gmail)\b)(?=.*\b(?:whatsapp|wapp)\b).*?"
    r"\b(?:rispondi|scrivi)\s+a\s+(?P<target>[\wÀ-ÿ][\wÀ-ÿ'. -]{0,80}?)"
    r"(?=[,:.!?]|\s+(?:che|dicendo)\b|$)",
    re.I,
)


class UnifiedPlanner:
    """Deterministic domain/skill plan. Model output is never an executor."""

    def __init__(self, registry: UnifiedRegistryFacade, capability_router=None) -> None:
        self.registry = registry
        self.capability_router = capability_router

    def plan(self, user_goal: str) -> AssistantPlan:
        goal = " ".join(user_goal.split())
        if not goal:
            return self._clarification("empty_goal")

        email = bool(_EMAIL_RE.search(goal))
        grant = bool(_GRANT_RE.search(goal))
        tiremm = bool(_TIREMM_RE.search(goal))

        multisource_reply = _MULTISOURCE_WHATSAPP_REPLY_RE.search(goal)
        if multisource_reply:
            target = " ".join(multisource_reply.group("target").split())
            return self._gmail_whatsapp_reply_plan(goal, target=target)

        whatsapp_compose = _WHATSAPP_COMPOSE_RE.search(goal)
        whatsapp_reply = _WHATSAPP_REPLY_RE.search(goal)
        if _WHATSAPP_RE.search(goal) and _WHATSAPP_DENIED_RE.search(goal):
            return self._denied("whatsapp_mutation_denied")
        if whatsapp_compose or whatsapp_reply:
            match = whatsapp_reply or whatsapp_compose
            assert match is not None
            target = " ".join((match.groupdict().get("target") or match.groupdict().get("target2") or "").split())
            if not target:
                return self._clarification("whatsapp_target_required")
            skill = "whatsapp.reply" if whatsapp_reply else "whatsapp.compose"
            return AssistantPlan(
                intent=skill,
                domains=("whatsapp",),
                assignments=(self._assignment(
                    domain="whatsapp", skill=skill, objective=goal,
                    input_refs=("user.goal", "memory.tiremm"),
                    output_ref="artifact.whatsapp_draft",
                    policy=PolicyClass.CONFIRM_WRITE,
                    arguments={"target": target},
                ),),
            )

        if _FASTWEB_RE.search(goal) and _FASTWEB_MUTATION_RE.search(goal):
            return self._denied("fastweb_portal_mutation_denied")
        if _FASTWEB_RE.search(goal) and _FASTWEB_COMPARE_RE.search(goal):
            return self._fastweb_compare_plan(goal)

        mailchimp_subscribe = _MAILCHIMP_SUBSCRIBE_MEMBER_RE.search(goal)
        email_match = _MAILCHIMP_EMAIL_RE.search(goal)
        if mailchimp_subscribe and email_match is not None and (
            _MAILCHIMP_RE.search(goal) or _MAILCHIMP_LIST_ALIAS_RE.search(goal)
        ):
            list_id_match = _MAILCHIMP_LIST_ID_RE.search(goal)
            return AssistantPlan(
                intent="mailchimp.member.subscribe",
                domains=("mailchimp",),
                assignments=(self._assignment(
                    domain="mailchimp", skill="mailchimp.member.subscribe",
                    objective=goal, input_refs=("user.goal",),
                    output_ref="artifact.mailchimp_member_subscribe_approval",
                    policy=PolicyClass.CONFIRM_WRITE,
                    arguments={
                        "action": "mailchimp_member_subscribe",
                        "email_address": email_match.group(0).casefold(),
                        **(
                            {"list_id": list_id_match.group("list_id")}
                            if list_id_match else {}
                        ),
                    },
                ),),
            )

        if _MAILCHIMP_RE.search(goal):
            if _MAILCHIMP_CREATE_CAMPAIGN_RE.search(goal):
                return AssistantPlan(
                    intent="mailchimp.campaign.create", domains=("mailchimp",),
                    assignments=(self._assignment(
                        domain="mailchimp", skill="mailchimp.campaign.create",
                        objective=goal, input_refs=("user.goal", "artifact.mailchimp_campaign_draft"),
                        output_ref="artifact.mailchimp_campaign_create_approval",
                        policy=PolicyClass.CONFIRM_WRITE,
                        arguments={"action": "mailchimp_campaign_create"},
                    ),),
                )
            if _MAILCHIMP_SEND_CAMPAIGN_RE.search(goal):
                return AssistantPlan(
                    intent="mailchimp.campaign.send", domains=("mailchimp",),
                    assignments=(self._assignment(
                        domain="mailchimp", skill="mailchimp.campaign.send",
                        objective=goal, input_refs=("user.goal", "artifact.mailchimp_campaign_verified"),
                        output_ref="artifact.mailchimp_campaign_send_approval",
                        policy=PolicyClass.CONFIRM_WRITE,
                        arguments={"action": "mailchimp_campaign_send"},
                    ),),
                )
            if _MAILCHIMP_MUTATION_RE.search(goal):
                return self._denied("mailchimp_mutation_not_available")

            list_id_match = _MAILCHIMP_LIST_ID_RE.search(goal)
            operation = (
                "audience_analysis"
                if _MAILCHIMP_ANALYSIS_RE.search(goal)
                else "segments"
                if _MAILCHIMP_SEGMENTS_RE.search(goal)
                else "tags"
                if _MAILCHIMP_TAGS_RE.search(goal)
                else "members"
                if _MAILCHIMP_MEMBERS_RE.search(goal)
                else "audiences"
                if _MAILCHIMP_AUDIENCE_RE.search(goal)
                else "ping"
                if _MAILCHIMP_PING_RE.search(goal)
                else "campaigns"
            )

            return AssistantPlan(
                intent="mailchimp.read",
                domains=("mailchimp",),
                assignments=(self._assignment(
                    domain="mailchimp",
                    skill="mailchimp.read",
                    objective=goal,
                    input_refs=("user.goal",),
                    output_ref="artifact.mailchimp",
                    policy=PolicyClass.READ,
                    arguments={
                        "operation": operation,
                        "count": 10,
                        "offset": 0,
                        **(
                            {"list_id": list_id_match.group("list_id")}
                            if list_id_match else {}
                        ),
                    },
                ),),
            )

        # Email payload is data. Embedded home/tool words cannot add assignments.
        if email and grant:
            return self._grant_email_plan(goal, include_tiremm=tiremm)
        if email:
            email_skill = (
                "email.reply"
                if re.match(r"^\s*rispondi\b", goal, re.I)
                else "email.compose"
            )
            return AssistantPlan(
                intent=email_skill,
                domains=("email",),
                assignments=(self._assignment(
                    domain="email", skill=email_skill, objective=goal,
                    input_refs=("user.goal",), output_ref="artifact.email_draft",
                    policy=PolicyClass.CONFIRM_WRITE,
                ),),
            )
        if _WHATSAPP_RE.search(goal):
            return self._single(goal, "whatsapp", "whatsapp.read", PolicyClass.READ)
        if is_email_search_request(goal):
            search = plan_email_search(goal)
            if search is None:
                return self._clarification("email_search_arguments_unresolved")
            return AssistantPlan(
                intent="email.search",
                domains=("tiremm",),
                assignments=(self._assignment(
                    domain="tiremm", skill="email.search", objective=goal,
                    input_refs=("user.goal",), output_ref="artifact.email_search",
                    policy=PolicyClass.READ,
                    arguments={
                        "organization": search.organization,
                        "concept": search.concept,
                        "queries": list(search.queries),
                    },
                ),),
            )
        if _FASTWEB_RE.search(goal) and _FASTWEB_PORTAL_RE.search(goal):
            return self._single(goal, "tiremm", "fastweb.portal.read", PolicyClass.READ)
        if _BYPASS_RE.search(goal):
            return AssistantPlan(
                intent="assistant.reject",
                domains=("general_assistant",),
                assignments=(self._assignment(
                    domain="general_assistant", skill="assistant.clarify",
                    objective="Reject policy or capability bypass.", input_refs=("user.goal",),
                    output_ref="artifact.safe_rejection", policy=PolicyClass.DENY,
                ),),
            )
        if _METEO_RE.search(goal):
            return self._single(
                goal,
                "general_assistant",
                "meteo.read",
                PolicyClass.READ,
            )
        if _ATM_RE.search(goal):
            return self._single(
                goal,
                "general_assistant",
                "atm.route",
                PolicyClass.READ,
            )
        if _HOME_RE.search(goal):
            skill = "home.read" if re.search(r"\b(?:temperatura|fa\s+caldo|fa\s+freddo|stato|quanto)\b", goal, re.I) and not re.search(r"\b(?:accendi|spegni|apri|chiudi|imposta|metti|porta)\b", goal, re.I) else "home.control"
            return self._single(goal, "home", skill, PolicyClass.READ if skill == "home.read" else PolicyClass.AUTO_WRITE)
        if self.capability_router is not None:
            proposal = self.capability_router.route(goal)
            if proposal is not None and proposal.get("skill") in {
                "atm.route", "meteo.read", "email.search", "whatsapp.read",
                "mailchimp.read", "fastweb.portal.read", "home.read",
            }:
                skill = proposal["skill"]
                if skill == "email.search":
                    search = plan_email_search(goal)
                    if search is None:
                        return self._clarification("email_search_arguments_unresolved")
                    return AssistantPlan(
                        intent=skill, domains=("tiremm",),
                        assignments=(self._assignment(
                            domain="tiremm", skill=skill, objective=goal,
                            input_refs=("user.goal",), output_ref="artifact.email_search",
                            policy=PolicyClass.READ,
                            arguments={"organization": search.organization, "concept": search.concept, "queries": list(search.queries)},
                        ),),
                    )
                domain = "home" if skill == "home.read" else "general_assistant"
                return self._single(goal, domain, skill, PolicyClass.READ)
        if _RELATIONAL_RE.search(goal):
            return self._single(goal, "personal_relational", "personal_relational.analyze", PolicyClass.READ)
        if grant:
            return self._single(goal, "bandi", "bandi.eligibility" if tiremm else "bandi.read", PolicyClass.READ)
        if _INFRA_RE.search(goal):
            return self._single(goal, "infrastructure", "infrastructure.inspect", PolicyClass.READ)
        if _RESEARCH_RE.search(goal):
            return self._single(goal, "research", "research.deep", PolicyClass.READ)
        if _DOCUMENT_RE.search(goal):
            return self._single(goal, "documents", "documents.extract", PolicyClass.READ)
        if _CODE_RE.search(goal):
            return self._single(goal, "code", "code.inspect", PolicyClass.READ)
        if _EDITORIAL_RE.search(goal):
            return self._single(goal, "editorial", "editorial.flyer", PolicyClass.AUTO_WRITE)
        if _MEDIA_RE.search(goal):
            return self._single(goal, "media", "media.compose", PolicyClass.AUTO_WRITE)
        return self._clarification("domain_unresolved")

    def validate(self, plan: AssistantPlan) -> AssistantPlan:
        task_ids = {item.task_id for item in plan.assignments}
        if len(task_ids) != len(plan.assignments):
            raise ValueError("duplicate_assignment")
        seen: set[str] = set()
        for item in plan.assignments:
            if item.domain not in self.registry.domains:
                raise ValueError("plan_domain_unregistered")
            skill = self.registry.skill(item.skill)
            if item.domain not in skill.domains:
                raise ValueError("plan_skill_domain_mismatch")
            if set(item.depends_on) - seen:
                raise ValueError("plan_dependency_not_ready")
            seen.add(item.task_id)
        return plan

    def _grant_email_plan(self, goal: str, *, include_tiremm: bool) -> AssistantPlan:
        assignments = [self._assignment(
            domain="bandi", skill="bandi.read", objective="Retrieve and validate grant requirements.",
            input_refs=("user.goal",), output_ref="artifact.grant_evidence", policy=PolicyClass.READ,
        )]
        previous = assignments[-1].task_id
        if include_tiremm:
            assignments.append(self._assignment(
                domain="bandi", skill="bandi.eligibility",
                objective="Compare verified Tiremm facts with grant requirements.",
                input_refs=("artifact.grant_evidence", "memory.tiremm"),
                output_ref="artifact.eligibility", depends_on=(previous,), policy=PolicyClass.READ,
            ))
            previous = assignments[-1].task_id
        assignments.append(self._assignment(
            domain="email", skill="email.compose", objective=goal,
            input_refs=(("artifact.eligibility",) if include_tiremm else ("artifact.grant_evidence",)),
            output_ref="artifact.email_draft", depends_on=(previous,),
            policy=PolicyClass.CONFIRM_WRITE,
        ))
        return AssistantPlan(
            intent="bandi.review_and_email",
            domains=("bandi", "tiremm", "email") if include_tiremm else ("bandi", "email"),
            assignments=tuple(assignments),
        )

    def _gmail_whatsapp_reply_plan(self, goal: str, *, target: str) -> AssistantPlan:
        email = self._assignment(
            domain="tiremm", skill="email.search",
            objective=f"Cerca le mail di {target} riguardo la richiesta corrente.",
            input_refs=("user.goal",), output_ref="artifact.gmail_context",
            policy=PolicyClass.READ,
            arguments={"organization": target, "concept": "communications"},
        )
        whatsapp = self._assignment(
            domain="whatsapp", skill="whatsapp.read",
            objective=f"Cerca nella chat con {target}: messaggi pertinenti.",
            input_refs=("user.goal",), output_ref="artifact.whatsapp_context",
            policy=PolicyClass.READ, arguments={"target": target},
        )
        reply = self._assignment(
            domain="whatsapp", skill="whatsapp.reply", objective=goal,
            input_refs=(email.output_ref, whatsapp.output_ref, "memory.tiremm"),
            output_ref="artifact.whatsapp_draft", policy=PolicyClass.CONFIRM_WRITE,
            depends_on=(email.task_id, whatsapp.task_id), arguments={"target": target},
        )
        return AssistantPlan(
            intent="whatsapp.multisource_reply",
            domains=("tiremm", "whatsapp"), assignments=(email, whatsapp, reply),
        )

    def _single(self, goal: str, domain: str, skill: str, policy: PolicyClass) -> AssistantPlan:
        return AssistantPlan(
            intent=skill,
            domains=(domain,),
            assignments=(self._assignment(
                domain=domain, skill=skill, objective=goal, input_refs=("user.goal",),
                output_ref=f"artifact.{domain}", policy=policy,
            ),),
        )

    def _clarification(self, reason: str) -> AssistantPlan:
        return AssistantPlan(
            intent="assistant.clarify",
            domains=("general_assistant",),
            assignments=(self._assignment(
                domain="general_assistant", skill="assistant.clarify",
                objective="Ask for the missing objective or domain.", input_refs=("user.goal",),
                output_ref="artifact.clarification", policy=PolicyClass.READ,
            ),),
            requires_clarification=True,
            clarification_reason=reason,
        )

    def _denied(self, reason: str) -> AssistantPlan:
        plan = self._clarification(reason)
        assignment = plan.assignments[0].model_copy(update={
            "objective": "Reject unsupported or protected external mutation.",
            "policy": PolicyClass.DENY,
        })
        return plan.model_copy(update={"intent": "assistant.reject", "assignments": (assignment,)})

    def _fastweb_compare_plan(self, goal: str) -> AssistantPlan:
        search = plan_email_search("Controlla se Fastweb ha comunicato un aumento")
        assert search is not None
        email = self._assignment(
            domain="tiremm", skill="email.search", objective=goal,
            input_refs=("user.goal",), output_ref="artifact.fastweb_email",
            policy=PolicyClass.READ,
            arguments={
                "organization": search.organization,
                "concept": search.concept,
                "queries": list(search.queries),
            },
        )
        portal = self._assignment(
            domain="tiremm", skill="fastweb.portal.read", objective=goal,
            input_refs=("user.goal",), output_ref="artifact.fastweb_portal",
            policy=PolicyClass.READ,
        )
        compare = self._assignment(
            domain="tiremm", skill="fastweb.compare", objective=goal,
            input_refs=("artifact.fastweb_email", "artifact.fastweb_portal"),
            output_ref="artifact.fastweb_comparison", policy=PolicyClass.READ,
            depends_on=(email.task_id, portal.task_id),
        )
        return AssistantPlan(
            intent="fastweb.compare",
            domains=("tiremm",),
            assignments=(email, portal, compare),
        )

    @staticmethod
    def _assignment(
        *,
        domain: str,
        skill: str,
        objective: str,
        input_refs: tuple[str, ...],
        output_ref: str,
        policy: PolicyClass,
        depends_on: tuple[str, ...] = (),
        arguments: dict | None = None,
    ) -> PlanAssignment:
        digest = hashlib.sha256(
            (domain + "\x00" + skill + "\x00" + objective + "\x00" + "|".join(input_refs)).encode()
        ).hexdigest()[:16]
        return PlanAssignment(
            task_id=f"task.{digest}",
            domain=domain,
            skill=skill,
            objective=objective,
            input_refs=input_refs,
            output_ref=output_ref,
            depends_on=depends_on,
            policy=policy,
            arguments=dict(arguments or {}),
            content_is_data=True,
        )


__all__ = ["UnifiedPlanner"]
