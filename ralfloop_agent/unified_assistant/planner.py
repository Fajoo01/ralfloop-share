from __future__ import annotations

import hashlib
import re

from .contracts import AssistantPlan, PlanAssignment, PolicyClass
from .email_search import is_email_search_request, plan_email_search
from .registry import UnifiedRegistryFacade


_EMAIL_RE = re.compile(
    r"\b(?:scrivi\s+(?:una\s+mail\s+)?a|prepara\s+(?:una\s+)?(?:mail|email)|"
    r"manda\s+(?:una\s+)?(?:mail|email)|"
    r"rispond(?:i|ere)\s+(?:all['’]\s*|alla\s+|a\s+questa\s+)(?:mail|email|appello|comunicazione)|"
    r"rispond(?:i|ere)\s+a\s+[\wÀ-ÿ][\wÀ-ÿ'. -]{0,80})\b",
    re.I,
)
_PEC_RE = re.compile(
    r"\b(?:pec|posta\s+certificata|posta\s+elettronica\s+certificata|webmail\s+pec|casella\s+pec)\b"
    r"|\btiremminnanz@pec\.it\b",
    re.I,
)
_PEC_WRITE_RE = re.compile(r"\b(?:invia|manda|spedisci|rispondi|inoltra)\b", re.I)
_PEC_ADDRESS_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}\b", re.I)
_PEC_SUBJECT_RE = re.compile(r"\boggetto\s*:\s*(?P<value>[^|]+?)(?=\s+testo\s*:|\s+allegat[oi]\s*:|$)", re.I)
_PEC_BODY_RE = re.compile(r"\btesto\s*:\s*(?P<value>.+?)(?=\s+allegat[oi]\s*:|$)", re.I)
_PEC_ATTACHMENTS_RE = re.compile(r"\ballegat[oi]\s*:\s*(?P<value>.+)$", re.I)
_HOME_RE = re.compile(
    r"\b(?:home\s+assistant|domotica|luc[ei]|lampad[ae]|neon|termostat[oi]|clima|climatizzatore|"
    r"tapparell[ae]|serrand[ae]|cancello|pres[ae]|switch|temperatura|fa\s+caldo|fa\s+freddo|abbassala|alzala)\b",
    re.I,
)
_GRANT_RE = re.compile(r"\b(?:band[oi]|grant|contribut[oi]|finanziament[oi]|candidatur[ae]|opportunit[aà])\b", re.I)
_GRANT_DISCOVER_RE = re.compile(r"\b(?:cerca(?:mi|re)?|trova(?:mi|re)?|scopri|ricerca|aggiorna|nuov[ioe]|apert[ioe]|opportunit[aà]|segnala)\b", re.I)
_GRANT_REVIEW_RE = re.compile(r"\b(?:valuta|analizza|verifica|ammissibil|compatibil|conviene|requisit|scaden|budget|cofinanzi)\w*\b", re.I)
_ARCI_GRANT_SOURCE_RE = re.compile(
    r"(?=.*\barci(?:\s+milano)?\b)(?=.*\b(?:band[oi]|appello|comunicazione|circoli)\b)", re.I
)
_TIREMM_RE = re.compile(r"\b(?:tiremm|associazione|aps|partner|progetto)\b", re.I)
_RELATIONAL_RE = re.compile(r"\b(?:rsc|abc|relazional[ei]|formula\s+loop)\b", re.I)
_INFRA_RE = re.compile(r"\b(?:agentcpm|servizi[oa]?|spazio\s+libero|disco|server|amule)\b", re.I)
_CODE_RE = re.compile(r"\b(?:codice|repository|repo|bug|debug|test|stacktrace)\b", re.I)
_FRONTEND_RE = re.compile(
    r"\b(?:front[\s-]?end|ui|ux|responsive|mobile[\s-]?first|interfaccia\s+utente|"
    r"design\s+(?:web|app|mobile)|layout\s+(?:web|app|mobile)|viewport)\b",
    re.I,
)
_FRONTEND_APPLY_RE = re.compile(r"\b(?:applica|implementa|modifica|ridisegna|sistema|correggi|rifai|fix)\b", re.I)
_FRONTEND_WEB_TEST_RE = re.compile(r"\b(?:test\s+web|responsive\s+test|screenshot|viewport)\b", re.I)
_FRONTEND_ANDROID_TEST_RE = re.compile(r"\b(?:apk|test\s+android|android\s+test|emulatore)\b", re.I)
_FRONTEND_PHONE_RE = re.compile(r"\b(?:telefono\s+(?:reale|fisico)|real\s+phone|physical\s+phone)\b", re.I)
_FRONTEND_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?P<path>/(?:[^\s\"']+))")
_FRONTEND_URL_RE = re.compile(r"https?://[^\s\"']+", re.I)
_FRONTEND_PACKAGE_RE = re.compile(r"\b(?:package|pacchetto)\s*[:=]\s*(?P<value>[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)\b", re.I)
_DOCUMENT_RE = re.compile(r"\b(?:pdf|document[oi]|allegat[oi]|estrai)\b", re.I)
_SIGN_RE = re.compile(r"\b(?:firma(?:re|to|ta)?|firmalo|firmala|firmare|firma\s+digitale|digitalmente|arubasign|p7m)\b", re.I)
_SIGN_PATH_RE = re.compile(r"(?P<path>/(?:[^\s\"']|\\ )+\.(?:pdf|docx|odt|p7m))\b", re.I)
_SIGN_APPROVAL_ID_RE = re.compile(r"\b(apr_[A-Z2-9]{8})\b", re.I)
_SIGN_VERIFY_RE = re.compile(r"\b(?:verifica|controlla)\b.*\b(?:firma|firmat[oa]|p7m)\b", re.I)
_SIGN_HANDOFF_RE = re.compile(r"\b(?:apri|avvia|continua|procedi)\b.*\b(?:firma|arubasign)\b", re.I)
_RESEARCH_RE = re.compile(r"\b(?:ricerca|cerca\s+sul\s+web|fonti|deep\s+research)\b", re.I)
_NORMATIVE_ADMIN_TOPIC_RE = re.compile(
    r"\b(?:aps|ets|runts|terzo\s+settore|codice\s+del\s+terzo\s+settore|"
    r"associazion[ei]\s+di\s+promozione\s+sociale|enti?\s+del\s+terzo\s+settore)\b",
    re.I,
)
_NORMATIVE_INFO_RE = re.compile(
    r"\b(?:cos['’]?[eè]|che\s+cos['’]?[eè]|definisci|spiega|normativ[ae]|legge|"
    r"decreto|d\.?\s*lgs\.?|articol[oi]|requisit[oi]|obbligh[oi]|disciplina|"
    r"cosa\s+prevede|chi\s+pu[oò]|come\s+funziona)\b",
    re.I,
)
_EDITORIAL_RE = re.compile(r"\b(?:volantin[oi]|flyer|locandin[ae]|manifest[oi]|poster)\b", re.I)
_MEDIA_RE = re.compile(r"\b(?:video|audio|immagine|ffmpeg|sottotitol[oi])\b", re.I)
_JELLYFIN_RE = re.compile(r"\bjellyfin\b", re.I)
_BROWSER_RE = re.compile(r"\b(?:browser|playwright|pagina\s+web|schede?\s+browser)\b", re.I)
_BROWSER_INTERACTION_RE = re.compile(
    r"\b(?:clicca|click|scrivi|digita|type|compila|fill|carica|upload|"
    r"invia|submit|seleziona|select|trascina|drag|premi|press)\b", re.I
)
_BROWSER_TARGET_RE = re.compile(r"\b(?P<value>e[0-9]+)\b", re.I)
_BROWSER_TYPE_RE = re.compile(r"\b(?:scrivi|digita|type|compila|fill)\b", re.I)
_BROWSER_UPLOAD_RE = re.compile(r"\b(?:carica|upload)\b", re.I)
_BROWSER_SUBMIT_RE = re.compile(r"\b(?:submit|invia|premi\s+invio)\b", re.I)
_BROWSER_TEXT_RE = re.compile(
    r"\b(?:testo|text)\s*[:=]\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<raw>\S.*))",
    re.I,
)
_BROWSER_FILE_RE = re.compile(
    r"\b(?:file|path)\s*[:=]\s*(?:\"(?P<dq>[^\"]+)\"|'(?P<sq>[^']+)'|(?P<raw>\S+))",
    re.I,
)
_JELLYFIN_MUTATION_RE = re.compile(
    r"\b(?:applica|modifica|aggiorna|refresh|deduplica|elimina|rimuovi|correggi)\b", re.I
)
_JELLYFIN_ITEM_ID_RE = re.compile(r"\b(?:item[_ -]?id|jellyfin[_ -]?id)\s*[:=]\s*(?P<value>[a-f0-9]{32,64})\b", re.I)
_JELLYFIN_PROVIDER_RE = re.compile(r"\bprovider\s*[:=]\s*(?P<value>tmdb|imdb)\b", re.I)
_JELLYFIN_PROVIDER_ID_RE = re.compile(r"\bprovider[_ -]?id\s*[:=]\s*(?P<value>[A-Za-z0-9_-]{1,64})\b", re.I)
_JELLYFIN_YEAR_RE = re.compile(r"\byear\s*[:=]\s*(?P<value>18\d{2}|19\d{2}|20\d{2})\b", re.I)


def _jellyfin_apply_args(goal: str) -> dict[str, object]:
    args: dict[str, object] = {}
    for key, pattern in (("item_id", _JELLYFIN_ITEM_ID_RE), ("provider", _JELLYFIN_PROVIDER_RE), ("provider_id", _JELLYFIN_PROVIDER_ID_RE)):
        match = pattern.search(goal)
        if match:
            args[key] = match.group("value")
    year = _JELLYFIN_YEAR_RE.search(goal)
    if year:
        args["year"] = int(year.group("value"))
    return args


def _pec_write_args(goal: str) -> dict[str, object]:
    args: dict[str, object] = {}
    addresses = [m.group(0) for m in _PEC_ADDRESS_RE.finditer(goal)]
    external = [item for item in addresses if item.casefold() != "tiremminnanz@pec.it"]
    if external:
        args["recipient"] = external[0]
    subject = _PEC_SUBJECT_RE.search(goal)
    if subject:
        args["subject"] = " ".join(subject.group("value").split())
    body = _PEC_BODY_RE.search(goal)
    if body:
        args["body"] = body.group("value").strip()
    attachments = _PEC_ATTACHMENTS_RE.search(goal)
    if attachments:
        args["attachment_paths"] = [item.strip().strip('"\'') for item in attachments.group("value").split(",") if item.strip()]
    return args


def _frontend_args(goal: str) -> dict[str, object]:
    args: dict[str, object] = {}
    lowered = goal.casefold()
    path_match = _FRONTEND_PATH_RE.search(goal)
    url_match = _FRONTEND_URL_RE.search(goal)
    package_match = _FRONTEND_PACKAGE_RE.search(goal)
    if path_match:
        args["workdir"] = path_match.group("path").rstrip(".,;:")
    if url_match:
        args["url"] = url_match.group(0).rstrip(".,;:")
    if package_match:
        args["package"] = package_match.group("value")
    if _FRONTEND_WEB_TEST_RE.search(goal):
        args["operation"] = "test_web"
    elif _FRONTEND_ANDROID_TEST_RE.search(goal):
        args["operation"] = "test_android"
    elif _FRONTEND_APPLY_RE.search(goal):
        args["operation"] = "apply"
    else:
        args["operation"] = "contract"
    if "android" in lowered or "apk" in lowered or "emulatore" in lowered:
        args["target"] = "android"
    elif "mobile" in lowered or "telefono" in lowered or "smartphone" in lowered:
        args["target"] = "mobile"
    elif "web" in lowered or "responsive" in lowered or "viewport" in lowered:
        args["target"] = "web"
    else:
        args["target"] = "auto"
    if _FRONTEND_PHONE_RE.search(goal):
        args["use_phone"] = True
    return args


def _browser_interact_args(goal: str) -> dict[str, object]:
    args: dict[str, object] = {}
    target = _BROWSER_TARGET_RE.search(goal)
    if target:
        args["target"] = target.group("value").casefold()
    if _BROWSER_UPLOAD_RE.search(goal):
        args["logical_action"] = "upload"
        paths = []
        for match in _BROWSER_FILE_RE.finditer(goal):
            value = match.group("dq") or match.group("sq") or match.group("raw") or ""
            if value:
                paths.append(value.strip())
        if paths:
            args["paths"] = paths
    elif _BROWSER_TYPE_RE.search(goal):
        args["logical_action"] = "type"
        text = _BROWSER_TEXT_RE.search(goal)
        if text:
            value = text.group("dq") or text.group("sq") or text.group("raw") or ""
            args["text"] = value.strip()
    elif _BROWSER_SUBMIT_RE.search(goal):
        args["logical_action"] = "submit"
    else:
        args["logical_action"] = "click"
    return args

_ARCI_RE = re.compile(r"\barci\b", re.I)
_ARCI_MUTATION_RE = re.compile(
    r"\b(?:modifica|aggiorna|elimina|rimuovi|aggiungi|iscrivi|crea|invia)\b", re.I
)
_ATM_RE = re.compile(
    r"\b(?:atm|giromilano|mezzi\s+pubblici|trasporto\s+pubblico)\b"
    r"|\bcome\s+(?:arrivo|vado|posso\s+andare)\b"
    r"|\b(?:devo|voglio|vorrei)\s+(?:andare|arrivare)(?:\s+(?:da|dal|dalla|dallo|dai|dagli|dalle|a|ad|al|alla|allo|ai|agli|alle|all['’]|in)\b|\s*$)"
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

_GITHUB_RE = re.compile(r"\b(?:github|pull\s+request|github\s+issue|issue\s*#)\b", re.I)
_GITHUB_REPO_RE = re.compile(r"\b(?P<repo>[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})\b")
_GITHUB_ISSUE_RE = re.compile(r"\bissue\s*#?\s*(?P<number>[0-9]{1,10})\b", re.I)
_GITHUB_PR_RE = re.compile(r"\b(?:pr|pull\s+request)\s*#?\s*(?P<number>[0-9]{1,10})\b", re.I)
_GITHUB_LIST_ISSUES_RE = re.compile(r"\b(?:lista|elenca|mostra|leggi|controlla)\b.*\bissues?\b", re.I)
_GITHUB_LIST_PRS_RE = re.compile(r"\b(?:lista|elenca|mostra|leggi|controlla)\b.*\b(?:pr|pull\s+request)\b", re.I)
_GITHUB_MUTATION_RE = re.compile(r"\b(?:crea|apri|commenta|scrivi|merge|chiudi|modifica|elimina)\b", re.I)

_ANDROID_DEVICE_RE = re.compile(
    r"\b(?:redmi|xiaomi|hyperos|telefono|cellulare|smartphone|dispositivo\s+android|android\s+device)\b",
    re.I,
)
_ANDROID_LAUNCH_RE = re.compile(
    r"\b(?:apri|avvia|lancia)\s+(?P<app>baffoflix|grande\s+timoniere|bot[- ]?tazzi)\b",
    re.I,
)
_ANDROID_TAP_RE = re.compile(
    r"\b(?:clicca|tocca|tap|premi)(?:\s+(?:su|il|la|lo|l['’]))?\s+(?P<target>[^.!?]{1,120})",
    re.I,
)
_ANDROID_KEY_RE = re.compile(
    r"\b(?:tasto|premi)\s+(?P<key>home|indietro|back|invio|enter|tab|menu|volume\s+su|volume\s+giu|volume\s+gi[uù])\b",
    re.I,
)
_ANDROID_READ_RE = re.compile(
    r"\b(?:controlla|guarda|ispeziona|leggi|mostra|snapshot|schermo|foreground|app\s+aperta|stato)\b",
    re.I,
)
_ANDROID_FOREGROUND_RE = re.compile(r"\b(?:foreground|app\s+aperta|app\s+in\s+primo\s+piano)\b", re.I)
_ANDROID_APPS_RE = re.compile(r"\b(?:lista|elenca|mostra)\b.*\bapps?\b", re.I)

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

        if _ANDROID_DEVICE_RE.search(goal):
            launch_match = _ANDROID_LAUNCH_RE.search(goal)
            key_match = _ANDROID_KEY_RE.search(goal)
            tap_match = _ANDROID_TAP_RE.search(goal)
            if launch_match:
                app = " ".join(launch_match.group("app").casefold().replace("-", " ").split())
                package = {
                    "baffoflix": "org.tiremminnanz.baffoflix",
                    "grande timoniere": "org.tiremminnanz.remoteagent",
                    "bot tazzi": "org.tiremminnanz.remoteagent",
                    "bottazzi": "org.tiremminnanz.remoteagent",
                }.get(app)
                if package is None:
                    return self._clarification("android_mobile_app_not_allowed")
                return AssistantPlan(
                    intent="android.mobile.control",
                    domains=("infrastructure",),
                    assignments=(self._assignment(
                        domain="infrastructure", skill="android.mobile.control", objective=goal,
                        input_refs=("user.goal",), output_ref="artifact.android_mobile_action",
                        policy=PolicyClass.AUTO_WRITE,
                        arguments={"operation": "launch", "package": package},
                    ),),
                )
            if key_match:
                raw_key = " ".join(key_match.group("key").casefold().split())
                key = {
                    "indietro": "back",
                    "back": "back",
                    "home": "home",
                    "invio": "enter",
                    "enter": "enter",
                    "tab": "tab",
                    "menu": "menu",
                    "volume su": "volume_up",
                    "volume giu": "volume_down",
                    "volume giù": "volume_down",
                }[raw_key]
                return AssistantPlan(
                    intent="android.mobile.control",
                    domains=("infrastructure",),
                    assignments=(self._assignment(
                        domain="infrastructure", skill="android.mobile.control", objective=goal,
                        input_refs=("user.goal",), output_ref="artifact.android_mobile_action",
                        policy=PolicyClass.AUTO_WRITE,
                        arguments={"operation": "key", "key": key},
                    ),),
                )
            if tap_match:
                target = " ".join(tap_match.group("target").strip(" ,:;\"'’").split())
                if not target:
                    return self._clarification("android_mobile_target_required")
                return AssistantPlan(
                    intent="android.mobile.control",
                    domains=("infrastructure",),
                    assignments=(self._assignment(
                        domain="infrastructure", skill="android.mobile.control", objective=goal,
                        input_refs=("user.goal",), output_ref="artifact.android_mobile_action",
                        policy=PolicyClass.AUTO_WRITE,
                        arguments={"operation": "tap", "target_text": target},
                    ),),
                )
            if _ANDROID_READ_RE.search(goal):
                operation = (
                    "foreground" if _ANDROID_FOREGROUND_RE.search(goal)
                    else "apps" if _ANDROID_APPS_RE.search(goal)
                    else "snapshot"
                )
                return AssistantPlan(
                    intent="android.mobile.read",
                    domains=("infrastructure",),
                    assignments=(self._assignment(
                        domain="infrastructure", skill="android.mobile.read", objective=goal,
                        input_refs=("user.goal",), output_ref="artifact.android_mobile_read",
                        policy=PolicyClass.READ,
                        arguments={"operation": operation},
                    ),),
                )

        if _GITHUB_RE.search(goal):
            if _GITHUB_MUTATION_RE.search(goal):
                return self._denied("github_mutation_requires_approval_workflow")
            repo_match = _GITHUB_REPO_RE.search(goal)
            issue_match = _GITHUB_ISSUE_RE.search(goal)
            pr_match = _GITHUB_PR_RE.search(goal)
            operation = (
                "issue" if issue_match
                else "pr" if pr_match
                else "issues" if _GITHUB_LIST_ISSUES_RE.search(goal)
                else "prs" if _GITHUB_LIST_PRS_RE.search(goal)
                else "repo"
            )
            number_match = issue_match or pr_match
            return AssistantPlan(
                intent="github.read",
                domains=("code",),
                assignments=(self._assignment(
                    domain="code", skill="github.read", objective=goal,
                    input_refs=("user.goal",), output_ref="artifact.github",
                    policy=PolicyClass.READ,
                    arguments={
                        "operation": operation,
                        **({"repo": repo_match.group("repo")} if repo_match else {}),
                        **({"number": int(number_match.group("number"))} if number_match else {}),
                    },
                ),),
            )

        # Email payload is data. Embedded home/tool words cannot add assignments.
        if email and grant:
            return self._grant_email_plan(
                goal, include_tiremm=tiremm,
                include_email_source=bool(_ARCI_GRANT_SOURCE_RE.search(goal)),
            )
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
        if _JELLYFIN_RE.search(goal) and _JELLYFIN_MUTATION_RE.search(goal):
            return self._single(
                goal, "jellyfin", "jellyfin.apply_identity", PolicyClass.PROTECTED,
                arguments=_jellyfin_apply_args(goal),
            )
        if _ARCI_RE.search(goal) and _ARCI_MUTATION_RE.search(goal):
            return self._denied("arci_mutation_not_available")
        if _BROWSER_RE.search(goal) and _BROWSER_INTERACTION_RE.search(goal):
            return self._single(
                goal, "browser", "browser.interact", PolicyClass.CONFIRM_WRITE,
                arguments=_browser_interact_args(goal),
            )
        if _SIGN_RE.search(goal):
            path_match = _SIGN_PATH_RE.search(goal)
            approval_match = _SIGN_APPROVAL_ID_RE.search(goal)
            arguments: dict[str, object] = {
                "operation": (
                    "verify" if _SIGN_VERIFY_RE.search(goal)
                    else "handoff" if _SIGN_HANDOFF_RE.search(goal)
                    else "prepare"
                )
            }
            if path_match:
                key = "signed_path" if str(arguments["operation"]) == "verify" else "source_path"
                arguments[key] = path_match.group("path").replace("\\ ", " ")
            if approval_match:
                arguments["approval_request_id"] = approval_match.group(1).upper().replace("APR_", "apr_")
            return self._single(
                goal, "documents", "documents.sign", PolicyClass.CONFIRM_WRITE,
                arguments=arguments,
            )
        if _NORMATIVE_ADMIN_TOPIC_RE.search(goal) and _NORMATIVE_INFO_RE.search(goal):
            return self._single(
                goal, "research", "research.deep", PolicyClass.READ,
                arguments={
                    "query": goal,
                    "profile": "italy_third_sector_normative",
                },
            )
        if _PEC_RE.search(goal) and _PEC_WRITE_RE.search(goal) and "runts" not in goal.casefold():
            write_args = _pec_write_args(goal)
            required = {"recipient", "subject", "body"}
            if required.issubset(write_args):
                return self._single(
                    goal, "pec", "pec.prepare_send", PolicyClass.CONFIRM_WRITE,
                    arguments=write_args,
                )
            source = self._assignment(
                domain="pec", skill="pec.read", objective=goal,
                input_refs=("user.goal",), output_ref="artifact.pec_source",
                policy=PolicyClass.READ,
            )
            prepare = self._assignment(
                domain="pec", skill="pec.prepare_send", objective=goal,
                input_refs=("user.goal", source.output_ref),
                output_ref="artifact.pec_write_request",
                depends_on=(source.task_id,), policy=PolicyClass.CONFIRM_WRITE,
                arguments=write_args,
            )
            return AssistantPlan(
                intent="pec.prepare_send", domains=("pec",),
                assignments=(source, prepare),
            )
        if self.capability_router is not None:
            proposal = self.capability_router.route(goal)
            if proposal is not None and self.capability_router.index.is_auto_route_skill(str(proposal.get("skill") or "")):
                skill = str(proposal["skill"])
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
                domain = str(proposal.get("domain") or "general_assistant")
                return self._single(goal, domain, skill, PolicyClass.READ)
        # Deterministic fallbacks for runtimes where capability retrieval is
        # unavailable or returns no strong candidate. PEC remains independent
        # from RUNTS, and Home requires actual device/domain vocabulary.
        if _PEC_RE.search(goal):
            return self._single(goal, "pec", "pec.read", PolicyClass.READ)
        if _HOME_RE.search(goal):
            skill = "home.read" if re.search(r"\b(?:temperatura|fa\s+caldo|fa\s+freddo|stato|quanto)\b", goal, re.I) and not re.search(r"\b(?:accendi|spegni|apri|chiudi|imposta|metti|porta)\b", goal, re.I) else "home.control"
            return self._single(goal, "home", skill, PolicyClass.READ if skill == "home.read" else PolicyClass.AUTO_WRITE)
        if _RELATIONAL_RE.search(goal):
            return self._single(goal, "personal_relational", "personal_relational.analyze", PolicyClass.READ)
        if grant:
            skill = (
                "bandi.research"
                if _GRANT_DISCOVER_RE.search(goal)
                else "bandi.eligibility"
                if tiremm and _GRANT_REVIEW_RE.search(goal)
                else "bandi.read"
            )
            return self._single(goal, "bandi", skill, PolicyClass.READ)
        if _INFRA_RE.search(goal):
            return self._single(goal, "infrastructure", "infrastructure.inspect", PolicyClass.READ)
        # Prefer a concrete document artifact over generic research cues such as "fonti".
        if _DOCUMENT_RE.search(goal):
            return self._single(goal, "documents", "documents.extract", PolicyClass.READ)
        if _RESEARCH_RE.search(goal):
            return self._single(goal, "research", "research.deep", PolicyClass.READ)
        if _FRONTEND_RE.search(goal):
            return self._single(
                goal, "code", "frontend.design", PolicyClass.AUTO_WRITE,
                arguments=_frontend_args(goal),
            )
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

    def _grant_email_plan(
        self, goal: str, *, include_tiremm: bool, include_email_source: bool = False
    ) -> AssistantPlan:
        assignments: list[PlanAssignment] = []
        previous: str | None = None
        grant_inputs: tuple[str, ...] = ("user.goal",)
        if include_email_source:
            source = self._assignment(
                domain="tiremm", skill="email.search",
                objective="Cerca la comunicazione di ARCI Milano relativa al bando/appello ai circoli.",
                input_refs=("user.goal",), output_ref="artifact.grant_source_email",
                policy=PolicyClass.READ,
                arguments={"organization": "ARCI Milano", "concept": "grant_notice"},
            )
            assignments.append(source)
            previous = source.task_id
            grant_inputs = ("user.goal", source.output_ref)
        grant = self._assignment(
            domain="bandi", skill="bandi.read", objective=goal,
            input_refs=grant_inputs, output_ref="artifact.grant_evidence", policy=PolicyClass.READ,
            depends_on=((previous,) if previous else ()),
        )
        assignments.append(grant)
        previous = grant.task_id
        if include_tiremm:
            assignments.append(self._assignment(
                domain="bandi", skill="bandi.eligibility",
                objective=goal,
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
        domains = ("bandi", "tiremm", "email") if (include_tiremm or include_email_source) else ("bandi", "email")
        return AssistantPlan(
            intent="bandi.review_and_email", domains=domains, assignments=tuple(assignments),
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

    def _single(
        self, goal: str, domain: str, skill: str, policy: PolicyClass,
        *, arguments: dict | None = None,
    ) -> AssistantPlan:
        return AssistantPlan(
            intent=skill,
            domains=(domain,),
            assignments=(self._assignment(
                domain=domain, skill=skill, objective=goal, input_refs=("user.goal",),
                output_ref=f"artifact.{domain}", policy=policy, arguments=arguments,
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
        # PlanAssignment intentionally bounds objective to 1000 chars. Preserve the full
        # user goal outside the assignment, and keep both the beginning and the end here so
        # long agent requests cannot turn a validation constraint into an HTTP 500.
        bounded_objective = objective
        if len(bounded_objective) > 1000:
            bounded_objective = (
                bounded_objective[:680]
                + "\n[... objective compacted ...]\n"
                + bounded_objective[-280:]
            )
        return PlanAssignment(
            task_id=f"task.{digest}",
            domain=domain,
            skill=skill,
            objective=bounded_objective,
            input_refs=input_refs,
            output_ref=output_ref,
            depends_on=depends_on,
            policy=policy,
            arguments=dict(arguments or {}),
            content_is_data=True,
        )


__all__ = ["UnifiedPlanner"]
