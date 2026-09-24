#!/usr/bin/env python3
"""Idempotent Authentik OIDC bootstrap for Bot-tazzi."""

import os
from django.core.exceptions import ValidationError
from authentik.core.models import Application, Group
from authentik.crypto.models import CertificateKeyPair
from authentik.flows.models import Flow
from authentik.policies.models import PolicyBinding
from authentik.providers.oauth2.models import OAuth2Provider, RedirectURI, RedirectURIMatchingMode, ScopeMapping

NAME = "Tiremm Bot-tazzi OIDC"
APP_NAME = "Bot-tazzi"
SLUG = "tiremm-bottazzi"
GROUP_NAME = "members-active"
CLIENT_ID = os.environ.get("TIREMM_BOTTAZZI_OIDC_CLIENT_ID", "tiremm-bottazzi")
REDIRECT_URI = os.environ.get("TIREMM_BOTTAZZI_OIDC_REDIRECT_URI", "")
MODE = os.environ.get("TIREMM_OIDC_MODE", "plan")
SIGNING_KEY_NAME = os.environ.get("TIREMM_BOTTAZZI_OIDC_SIGNING_KEY_NAME", "authentik Self-signed Certificate")

def require_one(model, **lookup):
    try:
        return model.objects.get(**lookup)
    except model.DoesNotExist as exc:
        raise RuntimeError(f"required {model.__name__} missing: {lookup}") from exc
    except model.MultipleObjectsReturned as exc:
        raise RuntimeError(f"ambiguous {model.__name__}: {lookup}") from exc

def validate_input():
    if MODE not in {"plan", "apply"}:
        raise RuntimeError("TIREMM_OIDC_MODE must be plan or apply")
    if not REDIRECT_URI.startswith("https://"):
        raise RuntimeError("TIREMM_BOTTAZZI_OIDC_REDIRECT_URI must be an https URL")
    if not REDIRECT_URI.endswith("/oidc/callback"):
        raise RuntimeError("unexpected Bot-tazzi OIDC redirect path")

def load_dependencies():
    auth = require_one(Flow, slug="default-authentication-flow")
    authorize = require_one(Flow, slug="default-provider-authorization-implicit-consent")
    invalidate = require_one(Flow, slug="default-provider-invalidation-flow")
    group = require_one(Group, name=GROUP_NAME)
    signing_key = require_one(CertificateKeyPair, name=SIGNING_KEY_NAME)
    if not signing_key.key_data:
        raise RuntimeError("OIDC signing certificate has no private key")
    mappings = list(ScopeMapping.objects.filter(scope_name__in=["openid", "profile", "email"]))
    if {m.scope_name for m in mappings} != {"openid", "profile", "email"}:
        raise RuntimeError("required OpenID scope mappings are incomplete")
    return auth, authorize, invalidate, group, mappings, signing_key

def reconcile():
    validate_input()
    auth, authorize, invalidate, group, mappings, signing_key = load_dependencies()
    provider = OAuth2Provider.objects.filter(name=NAME).first()
    collision = OAuth2Provider.objects.filter(client_id=CLIENT_ID).exclude(name=NAME).first()
    if collision:
        raise RuntimeError("client_id already belongs to another OAuth2 provider")
    if MODE == "plan":
        print(f"mode=plan provider={'present' if provider else 'absent'}")
        print(f"application={'present' if Application.objects.filter(slug=SLUG).exists() else 'absent'}")
        current_key = provider.signing_key.name if provider and provider.signing_key else "NONE"
        print(f"group={GROUP_NAME} redirect_uri={REDIRECT_URI}")
        print(f"signing_key_current={current_key} signing_key_target={SIGNING_KEY_NAME}")
        return
    created = provider is None
    if created:
        secret = os.environ.get("TIREMM_BOTTAZZI_OIDC_CLIENT_SECRET", "")
        if len(secret) < 32:
            raise RuntimeError("runtime client secret missing or too short")
        provider = OAuth2Provider(name=NAME, client_id=CLIENT_ID, client_secret=secret)
    elif provider.client_id != CLIENT_ID:
        raise RuntimeError("existing provider client_id differs; refusing implicit rotation")
    else:
        supplied = os.environ.get("TIREMM_BOTTAZZI_OIDC_CLIENT_SECRET", "")
        if supplied and provider.client_secret != supplied:
            raise RuntimeError("runtime client secret does not match existing provider; refusing rotation")
    provider.authentication_flow = auth
    provider.authorization_flow = authorize
    provider.invalidation_flow = invalidate
    provider.client_type = "confidential"
    provider.grant_types = ["authorization_code", "refresh_token"]
    provider.include_claims_in_id_token = True
    provider.issuer_mode = "per_provider"
    provider.signing_key = signing_key
    provider.access_token_validity = "minutes=10"
    provider.redirect_uris = [RedirectURI(matching_mode=RedirectURIMatchingMode.STRICT, url=REDIRECT_URI)]
    try:
        provider.full_clean(exclude=["encryption_key", "backchannel_application"])
    except ValidationError as exc:
        raise RuntimeError(f"provider validation failed: {exc}") from exc
    provider.save()
    provider.property_mappings.set(mappings)
    app, app_created = Application.objects.update_or_create(
        slug=SLUG,
        defaults={"name": APP_NAME, "provider": provider, "meta_description": "Bot-tazzi via Portachiavi Tiremm"},
    )
    PolicyBinding.objects.update_or_create(
        target=app,
        group=group,
        defaults={"enabled": True, "order": 0, "negate": False, "failure_result": False},
    )
    print(f"mode=apply provider={'created' if created else 'reused'}")
    print(f"application={'created' if app_created else 'reused'} group_binding={GROUP_NAME}")
    print("client_secret=runtime-only")
    print("access_token_validity=minutes=10")
    print(f"signing_key={SIGNING_KEY_NAME}")

reconcile()
