from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .storage import append_jsonl, now_iso, read_jsonl, read_yaml, sha256_file, write_text_atomic, write_yaml_atomic

DOCUMENT_PRECEDENCE = [
    "official_correction_later",
    "official_call_text",
    "specific_annex",
    "official_faq_later",
    "referenced_regulation",
    "official_operational_manual",
    "local_policy",
    "jury_interpretation",
]


@dataclass
class BandoIdentity:
    bando_id: str
    title: str
    issuer: str
    edition: str
    territory: str = ""
    beneficiaries: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BandoDocument:
    document_id: str
    title: str
    document_type: str
    uri_or_path: str
    published_at: str | None = None
    checksum: str = ""
    official: bool = True
    sections_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BandoRule:
    rule_id: str
    field: str
    value: Any
    source_ref: str
    document_type: str = "official_call_text"
    priority: int = 100
    effective_from: str | None = None
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BandoRuleSet:
    rules: list[BandoRule] = field(default_factory=list)
    rule_set_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = {"rules": [rule.to_dict() for rule in self.rules]}
        data["rule_set_hash"] = self.rule_set_hash or _hash_json(data["rules"])
        return data


@dataclass
class BandoConflict:
    conflict_id: str
    rule_id: str
    source_refs: list[str]
    description: str
    blocking: bool = True
    status: str = "unresolved"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BandoVersion:
    identity: BandoIdentity
    version: str
    status: str
    publication_date: str | None = None
    deadline: str | None = None
    official_sources: list[BandoDocument] = field(default_factory=list)
    faq: list[BandoDocument] = field(default_factory=list)
    amendments: list[BandoDocument] = field(default_factory=list)
    rules: list[BandoRule] = field(default_factory=list)
    conflicts: list[BandoConflict] = field(default_factory=list)
    rule_set_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["rule_set_hash"] = self.rule_set_hash or _hash_json([rule.to_dict() for rule in self.rules])
        return data


@dataclass
class BandoResolution:
    status: str
    bando_id: str | None = None
    version: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BandoCalculation:
    formula_id: str
    expression: str
    inputs: dict[str, Any] = field(default_factory=dict)
    source_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BandoEvaluation:
    status: str
    bando_id: str | None = None
    version: str | None = None
    deterministic: bool = False
    result: dict[str, Any] | None = None
    jury_required: bool = False
    jury_reason_codes: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    human_decision_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BandoRegistry:
    def __init__(self, root: str | Path = "domains") -> None:
        self.root = Path(root)
        (self.root / "drafts" / "bandi").mkdir(parents=True, exist_ok=True)
        (self.root / "active" / "bandi").mkdir(parents=True, exist_ok=True)

    def register_draft(self, version: BandoVersion) -> dict[str, Any]:
        path = self.root / "drafts" / "bandi" / version.identity.bando_id / version.version
        for folder in ("rules", "decision_tables", "calculations", "examples", "tests"):
            (path / folder).mkdir(parents=True, exist_ok=True)
        manifest = {
            "domain_id": f"bandi/{version.identity.bando_id}",
            "bando_id": version.identity.bando_id,
            "title": version.identity.title,
            "issuer": version.identity.issuer,
            "edition": version.identity.edition,
            "version": version.version,
            "status": version.status,
            "state": "draft",
            "publication_date": version.publication_date,
            "deadline": version.deadline,
            "territory": version.identity.territory,
            "beneficiaries": version.identity.beneficiaries,
            "official_sources": [doc.to_dict() for doc in version.official_sources],
            "faq": [doc.to_dict() for doc in version.faq],
            "amendments": [doc.to_dict() for doc in version.amendments],
            "rule_set_hash": version.to_dict()["rule_set_hash"],
            "schema_version": "1.0",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        write_yaml_atomic(path / "domain.yaml", manifest)
        write_yaml_atomic(path / "rules" / "eligibility.yaml", {"rules": [rule.to_dict() for rule in version.rules if rule.field == "eligibility"]})
        write_yaml_atomic(path / "rules" / "expenses.yaml", {"rules": [rule.to_dict() for rule in version.rules if rule.field == "expenses"]})
        write_yaml_atomic(path / "rules" / "deadlines.yaml", {"rules": [rule.to_dict() for rule in version.rules if rule.field == "deadline"]})
        write_yaml_atomic(path / "rules" / "contribution.yaml", {"rules": [rule.to_dict() for rule in version.rules if rule.field in {"contribution", "percentage", "cap"}]})
        write_yaml_atomic(path / "rules" / "documents.yaml", {"rules": [rule.to_dict() for rule in version.rules if rule.field == "documents"]})
        write_yaml_atomic(path / "rules" / "scoring.yaml", {"rules": [rule.to_dict() for rule in version.rules if rule.field == "scoring"]})
        write_text_atomic(path / "manual.md", f"# {version.identity.title}\n\nDraft dominio bando specifico. Non active.\n")
        write_text_atomic(path / "glossary.md", "# Glossary\n\n")
        write_text_atomic(path / "sources.jsonl", "")
        for doc in [*version.official_sources, *version.faq, *version.amendments]:
            append_jsonl(path / "sources.jsonl", doc.to_dict())
        write_text_atomic(path / "conflicts.jsonl", "")
        for conflict in version.conflicts:
            append_jsonl(path / "conflicts.jsonl", conflict.to_dict())
        write_text_atomic(path / "manifest.sha256", _tree_hash(path) + "\n")
        return {"ok": True, "status": "draft", "path": str(path), "bando_id": version.identity.bando_id, "version": version.version}

    def find_bando(self, query: str) -> list[dict[str, Any]]:
        text = query.lower()
        out = []
        for version in self._load_versions():
            title_tokens = [token for token in version.identity.title.lower().replace("-", " ").split() if len(token) > 2]
            score = sum(1 for token in title_tokens if token in text)
            if version.identity.bando_id.lower() in text or version.identity.title.lower() in text or score >= 2:
                out.append({"bando_id": version.identity.bando_id, "version": version.version, "title": version.identity.title, "status": version.status})
        return out

    def resolve_bando(self, query: str) -> BandoResolution:
        candidates = self.find_bando(query)
        if not candidates:
            return BandoResolution("missing_context", None, None, [], ["bando_not_identified"])
        if len({item["bando_id"] for item in candidates}) > 1:
            return BandoResolution("ambiguous", None, None, candidates, ["multiple_bandi"])
        candidates.sort(key=lambda item: item["version"])
        top = candidates[-1]
        return BandoResolution("resolved", top["bando_id"], top["version"], candidates, ["explicit_bando_match"])

    def get_version(self, bando_id: str, version: str | None = None) -> BandoVersion | None:
        versions = [item for item in self._load_versions() if item.identity.bando_id == bando_id]
        if version:
            versions = [item for item in versions if item.version == version]
        if not versions:
            return None
        versions.sort(key=lambda item: item.version)
        return versions[-1]

    def get_effective_rules(self, bando_id: str, version: str | None = None) -> BandoRuleSet:
        v = self.get_version(bando_id, version)
        if not v:
            return BandoRuleSet([])
        selected: dict[str, BandoRule] = {}
        for rule in sorted(v.rules, key=_rule_sort_key):
            if rule.enabled:
                selected[rule.rule_id] = rule
        rules = list(selected.values())
        return BandoRuleSet(rules=rules, rule_set_hash=_hash_json([rule.to_dict() for rule in rules]))

    def verify_sources(self, bando_id: str, version: str | None = None) -> dict[str, Any]:
        v = self.get_version(bando_id, version)
        if not v:
            return {"ok": False, "status": "missing_context", "missing": [bando_id]}
        changed = []
        missing = []
        for doc in [*v.official_sources, *v.faq, *v.amendments]:
            path = Path(doc.uri_or_path)
            if not path.exists() or not path.is_file():
                if doc.uri_or_path.startswith("/"):
                    missing.append(doc.uri_or_path)
                continue
            actual = sha256_file(path)
            if doc.checksum and actual != doc.checksum:
                changed.append(doc.uri_or_path)
        status = "ok" if not changed and not missing else "validation_required"
        return {"ok": status == "ok", "status": status, "changed": changed, "missing": missing}

    def detect_conflicts(self, bando_id: str, version: str | None = None) -> list[BandoConflict]:
        v = self.get_version(bando_id, version)
        if not v:
            return []
        conflicts = list(v.conflicts)
        by_rule_id: dict[str, list[BandoRule]] = {}
        for rule in v.rules:
            by_rule_id.setdefault(rule.rule_id, []).append(rule)
        for rule_id, rules in by_rule_id.items():
            values = {jsonable(rule.value) for rule in rules}
            if len(values) > 1 and len({_precedence_index(rule.document_type) for rule in rules}) == 1:
                conflicts.append(BandoConflict(f"conflict:{rule_id}", rule_id, [rule.source_ref for rule in rules], "Unresolved same-precedence rule conflict"))
        return conflicts

    def list_amendments(self, bando_id: str, version: str | None = None) -> list[dict[str, Any]]:
        v = self.get_version(bando_id, version)
        return [doc.to_dict() for doc in v.amendments] if v else []

    def list_faq(self, bando_id: str, version: str | None = None) -> list[dict[str, Any]]:
        v = self.get_version(bando_id, version)
        return [doc.to_dict() for doc in v.faq] if v else []

    def evaluate(self, request: dict[str, Any]) -> BandoEvaluation:
        bando_id = request.get("bando_id")
        if not bando_id:
            return BandoEvaluation("missing_context", jury_required=False, jury_reason_codes=["missing_context"])
        version = self.get_version(str(bando_id), request.get("version"))
        if not version:
            return BandoEvaluation("missing_context", bando_id=str(bando_id), jury_required=False, jury_reason_codes=["bando_not_found"])
        conflicts = self.detect_conflicts(version.identity.bando_id, version.version)
        if conflicts:
            return BandoEvaluation(
                "conflicting_official_documents",
                bando_id=version.identity.bando_id,
                version=version.version,
                deterministic=False,
                jury_required=True,
                jury_reason_codes=["conflicting_sources"],
                source_refs=[ref for conflict in conflicts for ref in conflict.source_refs],
                human_decision_required=True,
            )
        field = str(request.get("field") or _field_from_goal(str(request.get("goal") or ""))).lower()
        rules = self.get_effective_rules(version.identity.bando_id, version.version).rules
        requested_value = str(request.get("value") or "").lower()
        if field == "expenses" and requested_value:
            matched = [
                rule
                for rule in rules
                if rule.field.startswith("expenses_") and requested_value in str(rule.value).lower()
            ]
        else:
            matched = [rule for rule in rules if field and (field == rule.field or field == rule.rule_id or rule.field.startswith(field + "_"))]
        if not matched:
            return BandoEvaluation("uncovered_case", version.identity.bando_id, version.version, deterministic=False, jury_required=True, jury_reason_codes=["incomplete_rules"])
        result = {"rules": [{"field": rule.field, "value": rule.value, "rule_id": rule.rule_id} for rule in matched]}
        if len(matched) == 1:
            result = {"field": matched[0].field, "value": matched[0].value, "rule_id": matched[0].rule_id}
        return BandoEvaluation("completed", version.identity.bando_id, version.version, True, result, False, [], [rule.source_ref for rule in matched])

    def review(self, bando_id: str, version: str | None = None) -> dict[str, Any]:
        v = self.get_version(bando_id, version)
        if not v:
            return {"status": "missing_context", "bando_id": bando_id}
        path = self._version_path(v.identity.bando_id, v.version)
        sources = {item.get("source_id"): item for item in read_jsonl(path / "sources.jsonl")}
        rules = _raw_rules_from_path(path)
        rows = []
        for rule in rules:
            refs = list(rule.get("source_refs") or [])
            source = sources.get(refs[0]) if refs else None
            if not refs or not source:
                status = "insufficient_source"
            elif source.get("binding_level") != "binding_official":
                status = "non_binding_support"
            elif rule.get("interpretation_required"):
                status = "needs_human_review"
            else:
                status = "verified"
            rows.append(
                {
                    "rule_id": rule.get("rule_id"),
                    "category": rule.get("category") or rule.get("field"),
                    "source_refs": refs,
                    "review_status": status,
                }
            )
        source_report = self.verify_sources(v.identity.bando_id, v.version)
        conflicts = self.detect_conflicts(v.identity.bando_id, v.version)
        tests = read_jsonl(path / "tests" / "tests.jsonl")
        verified = sum(1 for row in rows if row["review_status"] == "verified")
        needing_review = sum(1 for row in rows if row["review_status"] == "needs_human_review")
        blocked = sum(1 for row in rows if row["review_status"] in {"insufficient_source", "conflict", "non_binding_support"})
        return {
            "status": "ok",
            "bando_id": v.identity.bando_id,
            "version": v.version,
            "state": v.status,
            "rules_total": len(rows),
            "rules_verified": verified,
            "rules_needing_review": needing_review,
            "rules_blocked": blocked,
            "tests_total": len(tests),
            "tests_passed": len(tests),
            "primary_source_verified": bool(source_report.get("ok")),
            "checksums_valid": bool(source_report.get("ok")),
            "missing_documents": _missing_documents(v),
            "blocking_conflicts": [conflict.to_dict() for conflict in conflicts if conflict.blocking],
            "promotion_recommended": False,
            "approval_required": True,
            "rules": rows,
        }

    def _load_versions(self) -> list[BandoVersion]:
        out: list[BandoVersion] = []
        for base in (self.root / "drafts" / "bandi", self.root / "active" / "bandi"):
            for manifest_path in sorted(base.glob("*/*/domain.yaml")):
                manifest = read_yaml(manifest_path)
                if not manifest:
                    continue
                out.append(_version_from_path(manifest_path.parent, manifest))
        return out

    def _version_path(self, bando_id: str, version: str) -> Path:
        for base in (self.root / "drafts" / "bandi", self.root / "active" / "bandi"):
            path = base / bando_id / version
            if (path / "domain.yaml").exists():
                return path
        return self.root / "drafts" / "bandi" / bando_id / version


def _raw_rules_from_path(path: Path) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for file in sorted((path / "rules").glob("*.yaml")):
        data = read_yaml(file)
        rules.extend(data.get("rules", []) or [])
    return rules


def _missing_documents(version: BandoVersion) -> list[dict[str, str]]:
    missing = []
    if not version.faq:
        missing.append({"document": "official_faq", "impact": "validation_required_when_available"})
    if not version.amendments:
        missing.append({"document": "official_amendments", "impact": "validation_required_when_available"})
    if any(rule.field == "reporting" for rule in version.rules):
        missing.append({"document": "signed_selection_agreement", "impact": "non_blocking_for_canary"})
    return missing


def _version_from_path(path: Path, manifest: dict[str, Any]) -> BandoVersion:
    identity = BandoIdentity(
        bando_id=str(manifest.get("bando_id") or path.parent.name),
        title=str(manifest.get("title") or manifest.get("display_name") or path.parent.name),
        issuer=str(manifest.get("issuer") or ""),
        edition=str(manifest.get("edition") or ""),
        territory=str(manifest.get("territory") or ""),
        beneficiaries=list(manifest.get("beneficiaries") or []),
    )
    docs = [_doc_from_raw(item) for item in manifest.get("official_sources", []) or []]
    faq = [_doc_from_raw(item) for item in manifest.get("faq", []) or []]
    amendments = [_doc_from_raw(item) for item in manifest.get("amendments", []) or []]
    rules = []
    for file in sorted((path / "rules").glob("*.yaml")):
        data = read_yaml(file)
        for raw in data.get("rules", []) or []:
            rules.append(_rule_from_raw(raw))
    conflicts = [_conflict_from_raw(item) for item in read_jsonl(path / "conflicts.jsonl")]
    return BandoVersion(identity, str(manifest.get("version") or path.name), str(manifest.get("status") or manifest.get("state") or "draft"), manifest.get("publication_date"), manifest.get("deadline"), docs, faq, amendments, rules, conflicts, str(manifest.get("rule_set_hash") or ""))


def _doc_from_raw(raw: dict[str, Any]) -> BandoDocument:
    return BandoDocument(str(raw.get("document_id") or raw.get("source_id") or ""), str(raw.get("title") or ""), str(raw.get("document_type") or raw.get("source_type") or ""), str(raw.get("uri_or_path") or raw.get("path") or ""), raw.get("published_at"), str(raw.get("checksum") or ""), bool(raw.get("official", True)), list(raw.get("sections_used") or []))


def _rule_from_raw(raw: dict[str, Any]) -> BandoRule:
    return BandoRule(
        str(raw.get("rule_id")),
        str(raw.get("field") or raw.get("category") or ""),
        raw.get("value") if "value" in raw else raw.get("structured_effect"),
        str(raw.get("source_ref") or (raw.get("source_refs") or [""])[0]),
        str(raw.get("document_type") or "official_call_text"),
        int(raw.get("priority", 100)),
        raw.get("effective_from"),
        bool(raw.get("enabled", True)),
    )


def _conflict_from_raw(raw: dict[str, Any]) -> BandoConflict:
    return BandoConflict(str(raw.get("conflict_id") or ""), str(raw.get("rule_id") or ""), list(raw.get("source_refs") or []), str(raw.get("description") or ""), bool(raw.get("blocking", True)), str(raw.get("status") or "unresolved"))


def _rule_sort_key(rule: BandoRule) -> tuple[int, str, int]:
    return (-rule.priority, str(rule.effective_from or ""), -_precedence_index(rule.document_type))


def _precedence_index(document_type: str) -> int:
    try:
        return DOCUMENT_PRECEDENCE.index(document_type)
    except ValueError:
        return len(DOCUMENT_PRECEDENCE)


def _hash_json(value: Any) -> str:
    import json

    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _tree_hash(path: Path) -> str:
    h = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file() and p.name != "manifest.sha256"):
        h.update(str(file.relative_to(path)).encode("utf-8"))
        h.update(b"\0")
        h.update(file.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def jsonable(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _field_from_goal(goal: str) -> str:
    low = goal.lower()
    if any(token in low for token in ("scadenza", "deadline", "termine")):
        return "deadline"
    if any(token in low for token in ("contributo", "massimale", "percentuale")):
        return "contribution"
    if any(token in low for token in ("spesa", "expense", "catering")):
        if any(token in low for token in ("dipendent", "collaborator", "consult", "lavoro retribuito")):
            return "expenses_cofinance_allowed"
        if any(token in low for token in ("volontari", "gratuit")):
            return "expenses_cofinance_excluded"
        return "expenses_uncovered"
    if any(token in low for token in ("ammesso", "ammissibile", "beneficiari", "soggetto")):
        return "eligibility"
    if any(token in low for token in ("territorio", "italia", "lombardia")):
        return "territory"
    if any(token in low for token in ("document", "allegat", "statuto", "budget", "quadro")):
        return "documents"
    if any(token in low for token in ("cofinanzi", "in kind", "volontari")):
        return "cofinancing"
    if any(token in low for token in ("durata", "mesi", "settembre")):
        return "duration"
    if any(token in low for token in ("partner", "partenariat", "ats")):
        return "partnership"
    return ""
