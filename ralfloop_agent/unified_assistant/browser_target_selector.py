from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import re
import unicodedata
import urllib.request
from typing import Any, Mapping, Sequence


_INTERACTIVE_RE = re.compile(
    r'- (?P<role>button|link|textbox|checkbox|combobox|menuitem|radio|searchbox|switch)'
    r'(?: "(?P<label>[^"]*)")?.*?\[ref=(?P<ref>e\d+)\]'
    r'(?:.*?\[box=(?P<x>-?\d+(?:\.\d+)?),(?P<y>-?\d+(?:\.\d+)?),'
    r'(?P<w>\d+(?:\.\d+)?),(?P<h>\d+(?:\.\d+)?)\])?',
    re.I,
)
_STOP = frozenset({
    'il','lo','la','i','gli','le','un','uno','una','di','del','della','dei','delle',
    'a','al','alla','da','dal','in','su','e','o','per','questo','questa','mi','mio',
    'mia','con','fammi',
})
_SYNONYMS: Mapping[str, frozenset[str]] = {
    'invia': frozenset({'invia','send','submit'}),
    'svuota': frozenset({'cancella','clear','reset'}),
    'ricomincia': frozenset({'cancella','reset'}),
    'scrivi': frozenset({'textbox','casella','campo'}),
    'email': frozenset({'email'}),
    'contatta': frozenset({'contatta','contact'}),
    'privacy': frozenset({'privacy'}),
    'segnala': frozenset({'segnala','report'}),
    'entra': frozenset({'accedi','sign','login'}),
    'password': frozenset({'password'}),
    'collegato': frozenset({'ricordami','remember'}),
    'ricordami': frozenset({'ricordami','remember'}),
    'dimenticato': frozenset({'dimenticata','forgot'}),
    'lingua': frozenset({'lingua','language'}),
    'cambio': frozenset({'cambia','change'}),
    'applica': frozenset({'cambia','apply','ok'}),
    'conferma': frozenset({'ok','conferma','confirm'}),
    'cancella': frozenset({'cancella','clear'}),
    'microfono': frozenset({'vocale','voice'}),
    'voce': frozenset({'vocale','voice'}),
    'foto': frozenset({'immagine','image'}),
    'immagine': frozenset({'immagine','image'}),
    'notizie': frozenset({'notizie','news'}),
    'strumenti': frozenset({'strumenti','tools'}),
    'utente': frozenset({'username','utente'}),
    'vpn': frozenset({'sign','login'}),
    'chiudi': frozenset({'chiudi','close','ok'}),
    'condividi': frozenset({'condividi','share'}),
    'nascondi': frozenset({'nascondi','hide'}),
    'cerca': frozenset({'cerca','search'}),
    'ricerca': frozenset({'cerca','search'}),
    'crea': frozenset({'crea','create'}),
    'notifiche': frozenset({'notifiche','notifications'}),
    'avanzate': frozenset({'avanzate','advanced'}),
    'canale': frozenset({'canale','channel'}),
    'canali': frozenset({'canali','channels'}),
}


@dataclass(frozen=True)
class BrowserTargetCandidate:
    ref: str
    role: str
    label: str
    disabled: bool
    visible: bool
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None


@dataclass(frozen=True)
class BrowserTargetSelection:
    target: str | None
    source: str
    shortlist: tuple[str, ...]
    score: float = 0.0
    margin: float = 0.0
    confidence: float | None = None
    reason: str | None = None


def _tokens(value: str) -> list[str]:
    normalized = ''.join(
        ch for ch in unicodedata.normalize('NFKD', value.casefold())
        if not unicodedata.combining(ch)
    )
    return re.findall(r'[a-z0-9]+', normalized)


def parse_browser_candidates(snapshot: str) -> tuple[BrowserTargetCandidate, ...]:
    candidates: list[BrowserTargetCandidate] = []
    for line in snapshot.splitlines():
        match = _INTERACTIVE_RE.search(line)
        if not match:
            continue
        groups = match.groupdict()
        coords = [groups.get(key) for key in ('x','y','w','h')]
        x = float(coords[0]) if coords[0] is not None else None
        y = float(coords[1]) if coords[1] is not None else None
        width = float(coords[2]) if coords[2] is not None else None
        height = float(coords[3]) if coords[3] is not None else None
        visible = True
        if None not in (y, width, height):
            visible = bool(0 <= float(y) < 900 and float(width) > 1 and float(height) > 1)
        candidates.append(BrowserTargetCandidate(
            ref=str(groups['ref']),
            role=str(groups['role']).casefold(),
            label=str(groups.get('label') or ''),
            disabled='[disabled]' in line,
            visible=visible,
            x=x, y=y, width=width, height=height,
        ))
    return tuple(candidates)


def _goal_terms(goal: str) -> list[tuple[str, float]]:
    terms: list[tuple[str, float]] = []
    for token in (item for item in _tokens(goal) if item not in _STOP):
        terms.append((token, 1.0))
        terms.extend((alias, 0.8) for alias in _SYNONYMS.get(token, ()))
    return terms


def _score_candidates(goal: str, candidates: Sequence[BrowserTargetCandidate]) -> list[tuple[float, BrowserTargetCandidate]]:
    usable = [item for item in candidates if item.visible and not item.disabled]
    docs = [set(_tokens(item.label)) | {item.role} for item in usable]
    n_docs = max(1, len(docs))
    terms = _goal_terms(goal)
    goal_tokens = set(_tokens(goal))
    ranked: list[tuple[float, BrowserTargetCandidate]] = []
    for item, doc in zip(usable, docs):
        value = 0.0
        for term, weight in terms:
            df = sum(term in other for other in docs)
            if term in doc:
                value += weight * (math.log((n_docs + 1) / (df + 1)) + 1.0)
        for term, _weight in terms:
            if len(term) >= 5 and any(len(word) >= 5 and term[:5] == word[:5] for word in doc):
                value += 0.45
        if goal_tokens & {'scrivi','digita','compila'} and item.role in {'textbox','combobox'}:
            value += 1.2
        if goal_tokens & {'vai','apri'} and item.role in {'link','menuitem'}:
            value += 0.45
        if goal_tokens & {'invia','conferma','chiudi','cancella','crea','avvia'} and item.role == 'button':
            value += 0.55
        value /= 1.0 + 0.035 * max(0, len(_tokens(item.label)) - 4)
        ranked.append((value, item))
    ranked.sort(key=lambda pair: (pair[0], -(pair[1].y or 0.0)), reverse=True)
    return ranked


def shortlist_browser_targets(goal: str, snapshot: str, *, limit: int = 3) -> tuple[BrowserTargetCandidate, ...]:
    if limit < 1 or limit > 26:
        raise ValueError('browser_target_shortlist_limit')
    ranked = _score_candidates(goal, parse_browser_candidates(snapshot))
    return tuple(item for _score, item in ranked[:limit])


class RizzoTargetClient:
    def __init__(self, endpoint: str | None = None, *, timeout: float = 2.5) -> None:
        self.endpoint = endpoint or os.getenv('RALFLOOP_BROWSER_RIZZO_ENDPOINT', 'http://127.0.0.1:18017/v1/decisions')
        self.timeout = float(timeout)

    def choose(self, goal: str, candidates: Sequence[BrowserTargetCandidate]) -> tuple[str, float]:
        options = [
            {
                'id': item.ref,
                'description': f'{item.role} | {item.label or "(senza testo)"}',
            }
            for item in candidates
        ]
        request_body = {
            'state': {'goal': goal, 'source': 'browser_accessibility_snapshot'},
            'questions': {
                'target': {
                    'type': 'choice',
                    'instructions': 'Il target esiste tra queste opzioni. Scegli esattamente quale elemento UI soddisfa il goal.',
                    'policy': {'allow_abstain': False},
                    'options': options,
                }
            },
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(request_body, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.load(response)
        answer = payload['answers']['target']
        choice = str(answer.get('choice') or '')
        probabilities = answer.get('probabilities') or {}
        confidence = float(probabilities.get(choice) or 0.0)
        return choice, confidence


class BrowserTargetSelector:
    """Pure target resolver. It never performs browser writes."""

    def __init__(
        self,
        *,
        rizzo_enabled: bool | None = None,
        rizzo_client: Any | None = None,
        shortlist_size: int = 3,
        deterministic_min_score: float = 4.0,
        deterministic_min_margin: float = 5.0,
    ) -> None:
        self.rizzo_enabled = (
            os.getenv('RALFLOOP_BROWSER_RIZZO_TARGETING', '0') == '1'
            if rizzo_enabled is None else bool(rizzo_enabled)
        )
        self.rizzo_client = rizzo_client or RizzoTargetClient()
        self.shortlist_size = int(shortlist_size)
        self.deterministic_min_score = float(deterministic_min_score)
        self.deterministic_min_margin = float(deterministic_min_margin)

    def select(self, goal: str, snapshot: str) -> BrowserTargetSelection:
        candidates = parse_browser_candidates(snapshot)
        ranked = _score_candidates(goal, candidates)
        if not ranked:
            return BrowserTargetSelection(None, 'none', (), reason='browser_target_no_candidate')
        shortlist = tuple(item for _score, item in ranked[:self.shortlist_size])
        refs = tuple(item.ref for item in shortlist)
        top_score = float(ranked[0][0])
        next_score = float(ranked[1][0]) if len(ranked) > 1 else 0.0
        margin = top_score - next_score
        if top_score >= self.deterministic_min_score and margin >= self.deterministic_min_margin:
            return BrowserTargetSelection(ranked[0][1].ref, 'deterministic', refs, top_score, margin, 1.0)
        if not self.rizzo_enabled:
            return BrowserTargetSelection(None, 'fallback', refs, top_score, margin, reason='browser_rizzo_targeting_disabled')
        try:
            choice, confidence = self.rizzo_client.choose(goal, shortlist)
        except Exception as exc:
            return BrowserTargetSelection(None, 'fallback', refs, top_score, margin, reason=f'browser_rizzo_unavailable:{type(exc).__name__}')
        if choice not in refs:
            return BrowserTargetSelection(None, 'fallback', refs, top_score, margin, confidence, 'browser_rizzo_invalid_choice')
        return BrowserTargetSelection(choice, 'rizzo', refs, top_score, margin, confidence)


__all__ = [
    'BrowserTargetCandidate', 'BrowserTargetSelection', 'BrowserTargetSelector',
    'RizzoTargetClient', 'parse_browser_candidates', 'shortlist_browser_targets',
]
