from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit
from urllib.request import urlopen

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.integration.eyf_browser_executor import (
    EyfBrowserApprovalService,
)


SUPPORT4YOUTH_ORIGIN = "https://support4youth.coe.int"
SUPPORT4YOUTH_HOST = "support4youth.coe.int"
SUPPORT4YOUTH_PROFILE_PATH = "/organization/profile"
FINAL_SUBMIT_TARGET = "workflow:send-updates"


class EyfSupport4YouthError(ValueError):
    """Fail-closed validation or CDP state error."""


@dataclass(frozen=True)
class FieldSpec:
    key: str
    local_name: str
    kind: str
    choices: tuple[str, ...] = ()
    required: bool = True


_FREQUENCY = ("allTime", "fromTimeToTime", "rarely", "never")
_YOUTH_ROLES = (
    "beneficiaries",
    "volunteers",
    "staff",
    "decisionMakingBodies",
    "researchTopic",
    "none",
)


def _field(
    section: str,
    local_name: str,
    kind: str,
    *,
    choices: tuple[str, ...] = (),
    required: bool = True,
) -> FieldSpec:
    return FieldSpec(
        key=f"{section}.{local_name}",
        local_name=local_name,
        kind=kind,
        choices=choices,
        required=required,
    )


FIELD_SPECS: tuple[FieldSpec, ...] = (
    _field("generalInformation", "organizationNameRegistered", "text"),
    _field("organizationInBrief", "primaryMissionYoungPeople", "boolean"),
    _field("organizationInBrief", "registeredInMemberState", "boolean"),
    _field("missionDecisionMaking", "governanceUnder15", "integer"),
    _field("missionDecisionMaking", "governance15To30", "integer"),
    _field("missionDecisionMaking", "governanceOver30", "integer"),
    _field(
        "missionDecisionMaking",
        "youngPeopleInitiatePrograms",
        "frequency",
        choices=_FREQUENCY,
    ),
    _field(
        "missionDecisionMaking",
        "youngPeopleDecidePriorities",
        "frequency",
        choices=_FREQUENCY,
    ),
    _field(
        "missionDecisionMaking",
        "youngPeopleDecideBudget",
        "frequency",
        choices=_FREQUENCY,
    ),
    _field(
        "missionDecisionMaking",
        "youngPeopleConsultedPrograms",
        "frequency",
        choices=_FREQUENCY,
    ),
    _field(
        "missionDecisionMaking",
        "youngPeopleInvitedPrograms",
        "frequency",
        choices=_FREQUENCY,
    ),
    _field(
        "missionDecisionMaking",
        "organiseProjectsForYouth",
        "frequency",
        choices=_FREQUENCY,
    ),
    _field(
        "missionDecisionMaking",
        "staffVolunteersYoung",
        "frequency",
        choices=_FREQUENCY,
    ),
    _field(
        "missionDecisionMaking",
        "researchYoungPeople",
        "frequency",
        choices=_FREQUENCY,
    ),
    _field(
        "missionDecisionMaking",
        "youngPeopleRoles",
        "multi_choice",
        choices=_YOUTH_ROLES,
    ),
    _field("policies", "childSafeguardingPolicy", "boolean"),
    _field("policies", "genderEqualityPolicy", "boolean"),
    _field("policies", "antiDiscriminationPolicy", "boolean"),
    _field("policies", "environmentProtectionPolicy", "boolean"),
    _field("policies", "codeOfEthics", "boolean"),
    _field("policies", "accessibilityInclusionPolicy", "boolean"),
    _field(
        "bankDetails",
        "canReceiveEuroTransfers",
        "boolean",
        required=False,
    ),
)
FIELD_REGISTRY: Mapping[str, FieldSpec] = {
    spec.key: spec for spec in FIELD_SPECS
}


@dataclass(frozen=True)
class CdpPage:
    target_id: str
    title: str
    url: str
    websocket_url: str = ""


class Support4YouthCdpTransport(Protocol):
    def pages(self) -> Sequence[CdpPage]:
        ...

    def call_function(
        self,
        page: CdpPage,
        function: str,
        arguments: tuple[Any, ...],
        *,
        operation: str,
    ) -> Any:
        ...


_SNAPSHOT_FUNCTION = r"""
function(keys) {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = (node) => {
    if (!node || !(node instanceof Element)) return false;
    const style = getComputedStyle(node);
    return style.display !== 'none' && style.visibility !== 'hidden'
      && node.getClientRects().length > 0;
  };
  const fieldValue = (node) => {
    if (!node) return '';
    const children = Array.from(node.children);
    if (children.some((child) => child.classList.contains('line-through'))) {
      const replacement = children.filter(
        (child) => visible(child)
          && !child.classList.contains('line-through')
      ).map((child) => clean(child.innerText || child.textContent))
        .filter(Boolean);
      if (replacement.length) return clean(replacement.join(' '));
    }
    return clean(node.innerText || node.textContent);
  };
  const fields = keys.map((key) => {
    const label = document.getElementById(`df-${key}-label`);
    const text = document.getElementById(`df-${key}-text`);
    const edit = document.getElementById(`df-${key}-edit`);
    const reply = document.getElementById(`ct-${key}-reply-input`);
    const replySave = document.getElementById(`ct-${key}-reply-save`);
    const thread = (reply && reply.closest('eyf-comment-thread'))
      || (replySave && replySave.closest('eyf-comment-thread'));
    const candidates = thread ? Array.from(
      thread.querySelectorAll('.p-timeline-event-content p.w-full')
    ) : [];
    const comments = [];
    for (const node of candidates) {
      const value = clean(node.innerText || node.textContent);
      if (value && !comments.includes(value)) comments.push(value.slice(0, 2000));
    }
    return {
      key,
      label: clean(label && (label.innerText || label.textContent)).slice(0, 500),
      value: fieldValue(text).slice(0, 2000),
      comments: comments.slice(0, 20),
      reply_value: clean(reply && reply.value).slice(0, 4000),
      dom: {
        label: Boolean(label), text: Boolean(text), edit: Boolean(edit),
        reply_input: Boolean(reply), reply_save: Boolean(replySave)
      }
    };
  });
  const alerts = [];
  const alertNodes = document.querySelectorAll(
    '[role="alert"],p-message,.p-message,.p-toast-message,eyf-info-action-banner'
  );
  for (const node of Array.from(alertNodes).slice(0, 30)) {
    if (!visible(node)) continue;
    const value = clean(node.innerText || node.textContent);
    if (value && !alerts.includes(value)) alerts.push(value.slice(0, 1000));
  }
  const banner = document.querySelector('eyf-info-action-banner');
  const sendActions = banner ? Array.from(
    banner.querySelectorAll('button')
  ).filter((node) => visible(node) && !node.disabled
    && node.getAttribute('aria-disabled') !== 'true'
    && clean(node.innerText || node.textContent) === 'Send updates') : [];
  const bannerText = clean(banner && (banner.innerText || banner.textContent));
  const countMatch = bannerText.match(/\b(\d+)\b/);
  const fieldDomCount = fields.filter(
    (field) => field.dom.label && field.dom.text && field.dom.edit
  ).length;
  const domReady = document.readyState === 'complete'
    && fieldDomCount > 0;
  const clarificationKeys = fields.filter(
    (field) => field.dom.reply_input && field.dom.reply_save
  ).map((field) => field.key);
  const unresolvedClarificationKeys = fields.filter(
    (field) => clarificationKeys.includes(field.key)
      && (!field.value || field.value === '-' || field.comments.length < 2)
  ).map((field) => field.key);
  const pendingUpdatesCount = countMatch
    ? Number(countMatch[1])
    : (domReady && !banner ? 0 : null);
  const declarationHost = document.querySelector('eyf-declaration-modal');
  const declarationDialog = declarationHost && Array.from(
    declarationHost.querySelectorAll('[role="dialog"][aria-modal="true"],.p-dialog')
  ).find(visible);
  const statusValues = [];
  for (const node of Array.from(document.querySelectorAll('[id*="status" i],[data-testid*="status" i]')).slice(0, 20)) {
    const candidate = clean(node.innerText || node.textContent);
    if (/^(draft|submitted|clarification requested|under review|approved|rejected)$/i.test(candidate)) {
      statusValues.push(candidate.toLowerCase());
    }
  }
  const documentKinds = ['registration', 'statute', 'activity', 'proof'];
  const documents = documentKinds.map((kind) => {
    let count = 0;
    for (const node of Array.from(document.querySelectorAll('[id],[name],[data-testid]'))) {
      const identity = clean(`${node.id || ''} ${node.getAttribute('name') || ''} ${node.getAttribute('data-testid') || ''}`).toLowerCase();
      if (identity.includes(kind) && /(document|file|proof|upload|certificate|statut|activit)/.test(identity)) count += 1;
    }
    return {kind, present: count > 0, element_count: Math.min(count, 100)};
  });
  return {
    ok: true,
    url: location.href,
    title: document.title,
    stable: {
      fields,
      alerts,
      documents,
      pendingUpdatesCount,
      finalDeclarationAvailable: Boolean(declarationHost) && sendActions.length === 1,
      workflow: {
        dom_ready: domReady,
        field_dom_count: fieldDomCount,
        clarification_keys: clarificationKeys,
        unresolved_clarification_keys: unresolvedClarificationKeys,
        send_updates_available: sendActions.length === 1,
        pending_updates_count: pendingUpdatesCount,
        registration_status: statusValues[0] || '',
        edit_dialog_open: visible(document.getElementById('edit-field-dialog-body'))
          || visible(document.getElementById('edit-field-save')),
        declaration_modal_open: Boolean(declarationDialog),
        declaration_available: Boolean(declarationHost) && sendActions.length === 1
      }
    }
  };
}
""".strip()


_JS_REGISTRY = json.dumps(
    {
        spec.key: {
            "local": spec.local_name,
            "kind": spec.kind,
            "choices": list(spec.choices),
        }
        for spec in FIELD_SPECS
    },
    sort_keys=True,
    separators=(",", ":"),
)

_ACTION_FUNCTION = r"""
async function(action, key, value) {
  const registry = __REGISTRY__;
  const clean = (text) => String(text || '').replace(/\s+/g, ' ').trim();
  const assertProfileLocation = () => {
    const path = location.pathname.replace(/\/$/, '');
    if (location.protocol !== 'https:'
        || location.hostname !== 'support4youth.coe.int'
        || (location.port !== '' && location.port !== '443')
        || path !== '/organization/profile'
        || location.search || location.hash) {
      throw new Error('target_navigated');
    }
  };
  assertProfileLocation();
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const waitFor = async (predicate, error) => {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      const result = predicate();
      if (result) return result;
      await sleep(50);
    }
    throw new Error(error);
  };
  const nativeInput = (node, next) => {
    assertProfileLocation();
    if (!node || !(node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement)) {
      throw new Error('input_missing');
    }
    const prototype = node instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
    setter.call(node, next);
    node.dispatchEvent(new Event('input', {bubbles: true}));
    node.dispatchEvent(new Event('change', {bubbles: true}));
    node.dispatchEvent(new Event('blur', {bubbles: true}));
  };
  const buttonFor = (id, root = document) => {
    assertProfileLocation();
    const host = root === document
      ? document.getElementById(id)
      : Array.from(root.querySelectorAll('[id]')).find(
        (node) => node.id === id
      );
    if (!host) throw new Error(`missing:${id}`);
    const button = host instanceof HTMLButtonElement ? host : host.querySelector('button');
    if (!button || button.disabled || button.getAttribute('aria-disabled') === 'true') {
      throw new Error(`button_unavailable:${id}`);
    }
    return button;
  };
  const nativeChoice = (id, checked = true) => {
    assertProfileLocation();
    const host = document.getElementById(id);
    if (!host) throw new Error(`missing:${id}`);
    const input = host instanceof HTMLInputElement ? host : host.querySelector('input');
    if (!input || input.disabled) throw new Error(`choice_unavailable:${id}`);
    if (Boolean(input.checked) !== checked) {
      assertProfileLocation();
      input.click();
    }
  };
  const spec = registry[key];

  if (action === 'final_submit') {
    if (key || value) throw new Error('final_submit_arguments_invalid');
    const visible = (node) => {
      if (!node || !(node instanceof Element)) return false;
      const style = getComputedStyle(node);
      return style.display !== 'none' && style.visibility !== 'hidden'
        && node.getClientRects().length > 0;
    };
    const banner = document.querySelector('eyf-info-action-banner');
    if (!banner) throw new Error('send_updates_banner_missing');
    const launches = Array.from(banner.querySelectorAll('button')).filter(
      (node) => visible(node) && !node.disabled
        && node.getAttribute('aria-disabled') !== 'true'
        && clean(node.innerText || node.textContent) === 'Send updates'
    );
    if (launches.length !== 1 || launches[0].disabled) {
      throw new Error('send_updates_button_unresolved');
    }
    assertProfileLocation();
    launches[0].click();
    const modal = await waitFor(() => {
      const host = document.querySelector('eyf-declaration-modal');
      if (!host) return null;
      return Array.from(
        host.querySelectorAll('[role="dialog"][aria-modal="true"],.p-dialog')
      ).find(visible) || null;
    },
      'declaration_modal_missing'
    );
    const scrollable = Array.from(modal.querySelectorAll('*')).find(
      (node) => node.scrollHeight > node.clientHeight + 2
    );
    if (scrollable) {
      scrollable.scrollTop = scrollable.scrollHeight;
      scrollable.dispatchEvent(new Event('scroll', {bubbles: true}));
    }
    await waitFor(() => [
      'declaration-modal-accept-terms',
      'declaration-modal-accept-data-processing'
    ].every((id) => {
      const host = document.getElementById(id);
      const input = host instanceof HTMLInputElement
        ? host : host && host.querySelector('input');
      return Boolean(input && !input.disabled);
    }), 'declaration_choices_unavailable');
    nativeChoice('declaration-modal-accept-terms', true);
    nativeChoice('declaration-modal-accept-data-processing', true);
    const confirm = await waitFor(() => {
      const matches = Array.from(modal.querySelectorAll('button')).filter(
        (node) => clean(node.innerText || node.textContent) === 'Send updates'
      );
      return matches.length === 1 && !matches[0].disabled ? matches[0] : null;
    }, 'declaration_submit_unavailable');
    assertProfileLocation();
    confirm.click();
    await waitFor(
      () => {
        const host = document.querySelector('eyf-declaration-modal');
        if (!host) return true;
        return !Array.from(
          host.querySelectorAll('[role="dialog"][aria-modal="true"],.p-dialog')
        ).some(visible);
      },
      'declaration_submit_unconfirmed'
    );
    return {ok: true, action};
  }

  if (!spec) throw new Error('semantic_field_unknown');
  if (action === 'field_edit') {
    assertProfileLocation();
    buttonFor(`df-${key}-edit`).click();
    await waitFor(
      () => document.getElementById(`edit-field-${spec.local}`),
      'edit_dialog_field_missing'
    );
  } else if (action === 'field_fill') {
    const base = `edit-field-${spec.local}`;
    if (spec.kind === 'text' || spec.kind === 'integer') {
      nativeInput(document.getElementById(base), String(value));
    } else if (spec.kind === 'boolean') {
      nativeChoice(`${base}-${value === 'true' ? 0 : 1}`, true);
    } else if (spec.kind === 'frequency') {
      const index = spec.choices.indexOf(value);
      if (index < 0) throw new Error('semantic_choice_unknown');
      nativeChoice(`${base}-${index}`, true);
    } else if (spec.kind === 'multi_choice') {
      const selected = JSON.parse(value);
      for (const choice of spec.choices) {
        nativeChoice(`${base}-${choice}`, selected.includes(choice));
      }
    } else {
      throw new Error('semantic_field_kind_unknown');
    }
  } else if (action === 'field_save') {
    assertProfileLocation();
    const base = `edit-field-${spec.local}`;
    const activeField = document.getElementById(base);
    if (!activeField) throw new Error('edit_dialog_field_missing');
    const dialog = activeField.closest('[role="dialog"],.p-dialog');
    if (!dialog) throw new Error('edit_dialog_missing');
    buttonFor('edit-field-save', dialog).click();
    await waitFor(
      () => !activeField.isConnected || !dialog.isConnected
        || activeField.getClientRects().length === 0,
      'edit_dialog_save_unconfirmed'
    );
  } else if (action === 'reply_fill') {
    nativeInput(document.getElementById(`ct-${key}-reply-input`), String(value));
  } else if (action === 'reply_save') {
    assertProfileLocation();
    buttonFor(`ct-${key}-reply-save`).click();
    await waitFor(() => {
      const input = document.getElementById(`ct-${key}-reply-input`);
      return input && !String(input.value || '').trim();
    }, 'reply_save_unconfirmed');
  } else {
    throw new Error('semantic_action_unknown');
  }
  return {ok: true, action, key};
}
""".replace("__REGISTRY__", _JS_REGISTRY).strip()

_ALLOWED_FUNCTIONS = frozenset({_SNAPSHOT_FUNCTION, _ACTION_FUNCTION})


class LoopbackCdpTransport:
    """Small fixed-function CDP transport; endpoint and websocket stay loopback."""

    def __init__(self, endpoint: str, *, timeout_s: float = 10.0) -> None:
        parsed = urlsplit(endpoint.rstrip("/"))
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise EyfSupport4YouthError("cdp_endpoint_invalid")
        try:
            if not ip_address(parsed.hostname).is_loopback:
                raise EyfSupport4YouthError("cdp_endpoint_must_be_loopback")
        except ValueError as exc:
            raise EyfSupport4YouthError("cdp_endpoint_must_be_loopback") from exc
        self.endpoint = endpoint.rstrip("/")
        self.timeout_s = timeout_s

    def pages(self) -> tuple[CdpPage, ...]:
        try:
            with urlopen(
                self.endpoint + "/json/list",
                timeout=self.timeout_s,
            ) as response:
                payload = json.load(response)
        except Exception as exc:
            raise EyfSupport4YouthError("cdp_inventory_unavailable") from exc
        if not isinstance(payload, list):
            raise EyfSupport4YouthError("cdp_inventory_invalid")
        pages: list[CdpPage] = []
        for item in payload:
            if not isinstance(item, Mapping) or item.get("type") != "page":
                continue
            target_id = str(item.get("id") or "")
            websocket_url = str(item.get("webSocketDebuggerUrl") or "")
            if not target_id or not self._safe_websocket(websocket_url):
                continue
            pages.append(
                CdpPage(
                    target_id=target_id,
                    title=str(item.get("title") or ""),
                    url=str(item.get("url") or ""),
                    websocket_url=websocket_url,
                )
            )
        return tuple(pages)

    def call_function(
        self,
        page: CdpPage,
        function: str,
        arguments: tuple[Any, ...],
        *,
        operation: str,
    ) -> Any:
        del operation
        if function not in _ALLOWED_FUNCTIONS:
            raise EyfSupport4YouthError("cdp_function_denied")
        if not self._safe_websocket(page.websocket_url):
            raise EyfSupport4YouthError("cdp_websocket_forbidden")
        try:
            import websocket

            socket = websocket.create_connection(
                page.websocket_url,
                timeout=self.timeout_s,
            )
            try:
                document = self._round_trip(
                    socket,
                    1,
                    "Runtime.evaluate",
                    {"expression": "document", "returnByValue": False},
                )
                object_id = (
                    document.get("result", {})
                    .get("result", {})
                    .get("objectId")
                )
                if not object_id:
                    raise EyfSupport4YouthError("cdp_document_unavailable")
                response = self._round_trip(
                    socket,
                    2,
                    "Runtime.callFunctionOn",
                    {
                        "functionDeclaration": function,
                        "objectId": object_id,
                        "arguments": [{"value": item} for item in arguments],
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                )
            finally:
                socket.close()
        except EyfSupport4YouthError:
            raise
        except Exception as exc:
            raise EyfSupport4YouthError("cdp_operation_failed") from exc
        remote = response.get("result", {})
        if remote.get("exceptionDetails"):
            raise EyfSupport4YouthError("cdp_function_failed")
        result = remote.get("result", {})
        if result.get("subtype") == "error":
            raise EyfSupport4YouthError("cdp_function_failed")
        return result.get("value")

    @staticmethod
    def _round_trip(
        socket: Any,
        call_id: int,
        method: str,
        params: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        socket.send(
            json.dumps(
                {"id": call_id, "method": method, "params": dict(params)},
                separators=(",", ":"),
            )
        )
        while True:
            response = json.loads(socket.recv())
            if response.get("id") != call_id:
                continue
            if response.get("error"):
                raise EyfSupport4YouthError("cdp_operation_failed")
            return response

    @staticmethod
    def _safe_websocket(raw: str) -> bool:
        parsed = urlsplit(raw)
        if (
            parsed.scheme != "ws"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        try:
            return ip_address(parsed.hostname).is_loopback
        except ValueError:
            return False


def _is_profile_url(raw: str) -> bool:
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == SUPPORT4YOUTH_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") == SUPPORT4YOUTH_PROFILE_PATH
        and not parsed.query
        and not parsed.fragment
    )


class EyfSupport4YouthCdpAdapter:
    """Pinned profile adapter. Public mutation inputs are semantic IDs only."""

    def __init__(
        self,
        transport: Support4YouthCdpTransport,
        *,
        expected_target_id: str | None = None,
    ) -> None:
        if expected_target_id is not None and (
            not expected_target_id.strip() or len(expected_target_id) > 128
        ):
            raise EyfSupport4YouthError("target_id_invalid")
        self.transport = transport
        self.expected_target_id = (
            expected_target_id.strip() if expected_target_id else None
        )
        self._bound_target_id: str | None = None

    def snapshot(self) -> Mapping[str, Any]:
        page = self._page()
        value = self.transport.call_function(
            page,
            _SNAPSHOT_FUNCTION,
            (tuple(spec.key for spec in FIELD_SPECS),),
            operation="eyf.support4youth.snapshot",
        )
        if not isinstance(value, Mapping) or value.get("ok") is not True:
            raise EyfSupport4YouthError("snapshot_invalid")
        url = str(value.get("url") or "")
        if not _is_profile_url(url):
            raise EyfSupport4YouthError("target_navigated")
        stable = value.get("stable")
        if not isinstance(stable, Mapping):
            raise EyfSupport4YouthError("snapshot_invalid")
        fields = stable.get("fields")
        if not isinstance(fields, list) or [
            str(item.get("key") or "") if isinstance(item, Mapping) else ""
            for item in fields
        ] != [spec.key for spec in FIELD_SPECS]:
            raise EyfSupport4YouthError("snapshot_fields_invalid")
        return {
            "url": url,
            "target_id": page.target_id,
            "title": str(value.get("title") or page.title),
            "stable": {
                "target_id": page.target_id,
                "fields": fields,
                "alerts": list(stable.get("alerts") or []),
                "documents": list(stable.get("documents") or []),
                "pendingUpdatesCount": stable.get("pendingUpdatesCount"),
                "finalDeclarationAvailable": bool(
                    stable.get("finalDeclarationAvailable")
                ),
                "workflow": dict(stable.get("workflow") or {}),
            },
        }

    def validate_operation(self, operation: Mapping[str, Any]) -> None:
        kind = str(operation.get("kind") or "")
        target = str(operation.get("target") or "")
        if kind == "fill":
            value = operation.get("value")
            if not isinstance(value, str):
                raise EyfSupport4YouthError("fill_value_invalid")
            prefix, key = self._split_fill_target(target)
            if prefix == "field":
                self._validate_field_value(FIELD_REGISTRY[key], value)
            else:
                if not value.strip() or len(value) > 2000:
                    raise EyfSupport4YouthError("reply_value_invalid")
        elif kind == "click":
            self._parse_click_target(target)
        elif kind == "submit":
            if target != FINAL_SUBMIT_TARGET:
                raise EyfSupport4YouthError("submit_target_forbidden")
        elif kind == "upload":
            raise EyfSupport4YouthError("upload_forbidden")
        else:
            raise EyfSupport4YouthError("operation_kind_forbidden")

    def validate_batch(self, operations: Sequence[Mapping[str, Any]]) -> None:
        if (
            len(operations) == 1
            and str(operations[0].get("kind") or "") == "submit"
            and str(operations[0].get("target") or "") == FINAL_SUBMIT_TARGET
        ):
            return
        index = 0
        while index < len(operations):
            operation = operations[index]
            kind = str(operation.get("kind") or "")
            target = str(operation.get("target") or "")
            if kind == "click" and target.startswith("field:") and target.endswith(":edit"):
                key = target[len("field:") : -len(":edit")]
                expected = (
                    ("fill", f"field:{key}"),
                    ("click", f"field:{key}:save"),
                )
                if index + 2 >= len(operations) or any(
                    str(operations[index + offset].get("kind") or "") != pair[0]
                    or str(operations[index + offset].get("target") or "") != pair[1]
                    for offset, pair in enumerate(expected, start=1)
                ):
                    raise EyfSupport4YouthError("field_state_machine_invalid")
                index += 3
                continue
            if kind == "fill" and target.startswith("reply:"):
                key = target[len("reply:") :]
                if (
                    index + 1 >= len(operations)
                    or str(operations[index + 1].get("kind") or "") != "click"
                    or str(operations[index + 1].get("target") or "")
                    != f"reply:{key}:save"
                ):
                    raise EyfSupport4YouthError("reply_state_machine_invalid")
                index += 2
                continue
            raise EyfSupport4YouthError("save_batch_state_machine_invalid")

    def validate_snapshot_for_operations(
        self,
        operations: Sequence[Mapping[str, Any]],
        state: Mapping[str, Any],
    ) -> None:
        fields = {
            str(item.get("key") or ""): item
            for item in list(state.get("fields") or [])
            if isinstance(item, Mapping)
        }
        workflow = state.get("workflow")
        if not isinstance(workflow, Mapping) or workflow.get("dom_ready") is not True:
            raise EyfSupport4YouthError("profile_dom_not_ready")
        if (
            workflow.get("edit_dialog_open") is True
            or workflow.get("declaration_modal_open") is True
        ):
            raise EyfSupport4YouthError("portal_modal_open")

        if (
            len(operations) == 1
            and str(operations[0].get("kind") or "") == "submit"
            and str(operations[0].get("target") or "")
            == FINAL_SUBMIT_TARGET
        ):
            if (
                not isinstance(workflow.get("clarification_keys"), list)
                or not workflow.get("clarification_keys")
                or workflow.get("unresolved_clarification_keys") != []
                or workflow.get("send_updates_available") is not True
                or not isinstance(workflow.get("pending_updates_count"), int)
                or int(workflow["pending_updates_count"]) <= 0
                or state.get("finalDeclarationAvailable") is not True
            ):
                raise EyfSupport4YouthError("final_submission_not_ready")
            return

        for operation in operations:
            target = str(operation.get("target") or "")
            if target.startswith("field:"):
                key = target[len("field:") :].removesuffix(":edit").removesuffix(":save")
                dom = fields.get(key, {}).get("dom")
                if not isinstance(dom, Mapping) or not all(
                    dom.get(name) is True for name in ("label", "text", "edit")
                ):
                    raise EyfSupport4YouthError("field_dom_missing")
            elif target.startswith("reply:"):
                key = target[len("reply:") :].removesuffix(":save")
                dom = fields.get(key, {}).get("dom")
                if not isinstance(dom, Mapping) or not all(
                    dom.get(name) is True
                    for name in ("reply_input", "reply_save")
                ):
                    raise EyfSupport4YouthError("reply_dom_missing")

    def verify_postconditions(
        self,
        operations: Sequence[Mapping[str, Any]],
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        before_workflow = before.get("workflow")
        after_workflow = after.get("workflow")
        if not isinstance(before_workflow, Mapping) or not isinstance(
            after_workflow, Mapping
        ):
            return {"ok": False, "reason": "workflow_state_missing"}
        if (
            len(operations) == 1
            and str(operations[0].get("kind") or "") == "submit"
            and str(operations[0].get("target") or "") == FINAL_SUBMIT_TARGET
        ):
            before_count = before_workflow.get("pending_updates_count")
            after_count = after_workflow.get("pending_updates_count")
            before_status = str(
                before_workflow.get("registration_status") or ""
            ).casefold()
            after_status = str(
                after_workflow.get("registration_status") or ""
            ).casefold()
            status_changed = (
                before_status in {"clarification requested", "draft"}
                and after_status in {"submitted", "under review"}
            )
            cleared = (
                isinstance(before_count, int)
                and before_count > 0
                and after_count == 0
            )
            send_gone = after_workflow.get("send_updates_available") is False
            return {
                "ok": bool(
                    before_workflow.get("dom_ready") is True
                    and after_workflow.get("dom_ready") is True
                    and (cleared or status_changed)
                ),
                "phase": "final_submission",
                "pending_updates_cleared": cleared,
                "status_changed": status_changed,
                "send_updates_gone": send_gone,
                "before_pending_known": isinstance(before_count, int),
            }

        post_fields = {
            str(item.get("key") or ""): item
            for item in list(after.get("fields") or [])
            if isinstance(item, Mapping)
        }
        expected_fields: dict[str, str] = {}
        expected_replies: dict[str, str] = {}
        for operation in operations:
            if str(operation.get("kind") or "") != "fill":
                continue
            target = str(operation.get("target") or "")
            prefix, key = self._split_fill_target(target)
            if prefix == "field":
                expected_fields[key] = str(operation.get("value") or "")
            else:
                expected_replies[key] = str(operation.get("value") or "")
        mismatches = [
            key
            for key, expected in expected_fields.items()
            if key not in post_fields
            or not self._display_matches(
                FIELD_REGISTRY[key],
                expected,
                str(post_fields[key].get("value") or ""),
            )
        ]
        unverified_replies = []
        for key, expected in expected_replies.items():
            post = post_fields.get(key)
            if not isinstance(post, Mapping) or str(
                post.get("reply_value") or ""
            ).strip():
                unverified_replies.append(key)
                continue
            post_comments = list(post.get("comments") or [])
            normalized_expected = " ".join(expected.casefold().split())
            reply_visible = any(
                normalized_expected == " ".join(str(item).casefold().split())
                for item in post_comments
            )
            if not reply_visible:
                unverified_replies.append(key)
        staged = (
            after_workflow.get("send_updates_available") is True
            or (
                isinstance(after_workflow.get("pending_updates_count"), int)
                and int(after_workflow["pending_updates_count"]) > 0
            )
        )
        return {
            "ok": bool(
                after_workflow.get("dom_ready") is True
                and not mismatches
                and not unverified_replies
            ),
            "phase": "save_batch",
            "verified_field_count": len(expected_fields) - len(mismatches),
            "verified_reply_count": len(expected_replies) - len(unverified_replies),
            "pending_updates_visible": staged,
            "mismatch_keys": mismatches,
            "unverified_reply_keys": unverified_replies,
        }

    def fill(self, target: str, value: str) -> Mapping[str, Any]:
        operation = {"kind": "fill", "target": target, "value": value}
        self.validate_operation(operation)
        prefix, key = self._split_fill_target(target)
        semantic_value = value
        if prefix == "field" and FIELD_REGISTRY[key].kind == "multi_choice":
            selected = json.loads(value)
            semantic_value = json.dumps(selected, separators=(",", ":"))
        return self._action(f"{prefix}_fill", key, semantic_value)

    def upload(self, target: str, path: str) -> Mapping[str, Any]:
        del target, path
        raise EyfSupport4YouthError("upload_forbidden")

    def click(self, target: str) -> Mapping[str, Any]:
        action, key = self._parse_click_target(target)
        return self._action(action, key, "")

    def submit(self, target: str) -> Mapping[str, Any]:
        self.validate_operation({"kind": "submit", "target": target})
        return self._action("final_submit", "", "")

    def _action(self, action: str, key: str, value: str) -> Mapping[str, Any]:
        page = self._page()
        result = self.transport.call_function(
            page,
            _ACTION_FUNCTION,
            (action, key, value),
            operation=f"eyf.support4youth.{action}",
        )
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise EyfSupport4YouthError("browser_action_unconfirmed")
        return dict(result)

    def _page(self) -> CdpPage:
        pages = tuple(self.transport.pages())
        by_id = {page.target_id: page for page in pages}
        wanted = self._bound_target_id or self.expected_target_id
        if wanted:
            page = by_id.get(wanted)
            if page is None:
                reason = (
                    "target_stale" if self._bound_target_id else "target_missing"
                )
                raise EyfSupport4YouthError(reason)
            if not _is_profile_url(page.url):
                reason = (
                    "target_stale" if self._bound_target_id else "target_forbidden"
                )
                raise EyfSupport4YouthError(reason)
        else:
            matches = [page for page in pages if _is_profile_url(page.url)]
            if len(matches) != 1:
                raise EyfSupport4YouthError(
                    "target_missing" if not matches else "target_ambiguous"
                )
            page = matches[0]
        if not page.target_id:
            raise EyfSupport4YouthError("target_id_invalid")
        if self._bound_target_id and page.target_id != self._bound_target_id:
            raise EyfSupport4YouthError("target_stale")
        self._bound_target_id = page.target_id
        return page

    @staticmethod
    def _split_fill_target(target: str) -> tuple[str, str]:
        prefix, separator, key = target.partition(":")
        if (
            not separator
            or prefix not in {"field", "reply"}
            or key not in FIELD_REGISTRY
        ):
            raise EyfSupport4YouthError("semantic_target_forbidden")
        return prefix, key

    @staticmethod
    def _parse_click_target(target: str) -> tuple[str, str]:
        parts = target.split(":")
        if len(parts) != 3 or parts[1] not in FIELD_REGISTRY:
            raise EyfSupport4YouthError("semantic_target_forbidden")
        prefix, key, verb = parts
        action = {
            ("field", "edit"): "field_edit",
            ("field", "save"): "field_save",
            ("reply", "save"): "reply_save",
        }.get((prefix, verb))
        if action is None:
            raise EyfSupport4YouthError("semantic_target_forbidden")
        return action, key

    @staticmethod
    def _validate_field_value(spec: FieldSpec, value: str) -> None:
        if spec.kind == "text":
            if not 3 <= len(value) <= 256:
                raise EyfSupport4YouthError("field_value_invalid")
            allowed = {"'", ".", ",", "-"}
            if any(
                character not in allowed
                and not character.isspace()
                and unicodedata.category(character)[0] not in {"L", "N"}
                for character in value
            ):
                raise EyfSupport4YouthError("field_value_invalid")
        elif spec.kind == "integer":
            if not value.isascii() or not value.isdigit():
                raise EyfSupport4YouthError("field_value_invalid")
            if not 0 <= int(value) <= 9999:
                raise EyfSupport4YouthError("field_value_invalid")
        elif spec.kind == "boolean":
            if value not in {"true", "false"}:
                raise EyfSupport4YouthError("field_value_invalid")
        elif spec.kind == "frequency":
            if value not in spec.choices:
                raise EyfSupport4YouthError("field_value_invalid")
        elif spec.kind == "multi_choice":
            try:
                selected = json.loads(value)
            except json.JSONDecodeError as exc:
                raise EyfSupport4YouthError("field_value_invalid") from exc
            if (
                not isinstance(selected, list)
                or not selected
                or any(not isinstance(item, str) for item in selected)
                or len(selected) != len(set(selected))
                or any(item not in spec.choices for item in selected)
                or ("none" in selected and len(selected) != 1)
            ):
                raise EyfSupport4YouthError("field_value_invalid")
        else:
            raise EyfSupport4YouthError("field_kind_invalid")

    @staticmethod
    def _display_matches(spec: FieldSpec, semantic: str, displayed: str) -> bool:
        actual = " ".join(displayed.casefold().split())
        if spec.kind in {"text", "integer"}:
            return actual == " ".join(semantic.casefold().split())
        if spec.kind == "boolean":
            return actual == ("yes" if semantic == "true" else "no")
        frequency = {
            "allTime": "all the time",
            "fromTimeToTime": "from time to time",
            "rarely": "rarely",
            "never": "never",
        }
        if spec.kind == "frequency":
            return actual == frequency[semantic]
        if spec.kind == "multi_choice":
            labels = {
                "beneficiaries": "beneficiaries",
                "volunteers": "volunteers",
                "staff": "staff",
                "decisionMakingBodies": "decision-making bodies",
                "researchTopic": "research topic",
                "none": "none",
            }
            selected = set(json.loads(semantic))
            return all(
                (labels[item] in actual) == (item in selected)
                for item in spec.choices
            )
        return False


def make_eyf_support4youth_service(
    *,
    policy: DomainApprovalPolicy | None = None,
    store: DomainApprovalStore | None = None,
    transport: Support4YouthCdpTransport | None = None,
    cdp_endpoint: str | None = None,
    expected_target_id: str | None = None,
    approval_outbox: str | os.PathLike[str] | None = None,
) -> EyfBrowserApprovalService:
    policy = policy or DomainApprovalPolicy.from_env()
    store = store or DomainApprovalStore(policy=policy)
    if transport is None:
        endpoint = cdp_endpoint or os.environ.get(
            "RALF_EYF_CDP_ENDPOINT",
            "http://127.0.0.1:9236",
        )
        transport = LoopbackCdpTransport(endpoint)
    adapter = EyfSupport4YouthCdpAdapter(
        transport,
        expected_target_id=(
            expected_target_id
            if expected_target_id is not None
            else os.environ.get("RALF_EYF_TARGET_ID")
        ),
    )
    return EyfBrowserApprovalService(
        store,
        policy=policy,
        browser=adapter,
        allowed_uploads=(),
        approval_outbox=approval_outbox,
    )


def register_eyf_support4youth_routes(
    app: Any,
    *,
    service: EyfBrowserApprovalService | None = None,
) -> EyfBrowserApprovalService:
    service = service or make_eyf_support4youth_service()

    @app.get("/portals/support4youth/snapshot")
    async def eyf_support4youth_snapshot() -> Mapping[str, Any]:
        return service.browser.snapshot()

    @app.post("/portals/support4youth/preview")
    async def eyf_support4youth_preview(
        payload: dict[str, Any],
    ) -> Mapping[str, Any]:
        operations = payload.get("operations")
        if not isinstance(operations, list):
            return {"status": "operations_invalid", "executed": False}
        return service.preview(operations)

    @app.post("/portals/support4youth/requests")
    async def eyf_support4youth_request(
        payload: dict[str, Any],
    ) -> Mapping[str, Any]:
        operations = payload.get("operations")
        if not isinstance(operations, list):
            return {"status": "operations_invalid", "executed": False}
        return service.request(
            operations,
            requested_by=str(payload.get("requested_by") or "ralf"),
        )

    @app.get("/portals/support4youth/send-updates/preview")
    async def eyf_support4youth_final_preview() -> Mapping[str, Any]:
        return service.preview(
            ({"kind": "submit", "target": FINAL_SUBMIT_TARGET},)
        )

    @app.post("/portals/support4youth/send-updates/requests")
    async def eyf_support4youth_final_request(
        payload: dict[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        return service.request(
            ({"kind": "submit", "target": FINAL_SUBMIT_TARGET},),
            requested_by=str((payload or {}).get("requested_by") or "ralf"),
        )

    @app.post("/portals/support4youth/requests/{request_id}/apply")
    async def eyf_support4youth_apply(request_id: str) -> Mapping[str, Any]:
        return service.execute(request_id)

    return service


__all__ = [
    "CdpPage",
    "EyfSupport4YouthCdpAdapter",
    "EyfSupport4YouthError",
    "FIELD_REGISTRY",
    "FIELD_SPECS",
    "FINAL_SUBMIT_TARGET",
    "FieldSpec",
    "LoopbackCdpTransport",
    "SUPPORT4YOUTH_HOST",
    "SUPPORT4YOUTH_ORIGIN",
    "SUPPORT4YOUTH_PROFILE_PATH",
    "Support4YouthCdpTransport",
    "make_eyf_support4youth_service",
    "register_eyf_support4youth_routes",
]
