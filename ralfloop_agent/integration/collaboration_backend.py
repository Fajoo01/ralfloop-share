from __future__ import annotations

import os

from ralfloop_agent.integration.recursive_mas_native import RecursiveMASNativeAdapter
from src.models import CollaborationBackend, JuryPolicy
from src.routing_config import collaboration_backend_config, load_routing_config


def select_collaboration_backend(
    jury_policy: JuryPolicy,
    *,
    style: str,
    style_selection_source: str = "ralfloop_local_policy",
    route_only: bool = True,
) -> CollaborationBackend:
    config = load_routing_config()
    if not jury_policy.enabled:
        single = collaboration_backend_config(config, "single")
        return CollaborationBackend(
            backend_name=single.get("backend_name", "single"),
            implementation_level=single.get("implementation_level", "routing_only"),
            style="single",
            recursion_rounds=0,
            available=True,
            availability_reason="jury_not_required",
            native_latent=False,
            intermediate_decode_policy=single.get("intermediate_decode_policy", "none"),
            style_selection_source="none",
            source="ralfloop",
            requested_backend="single",
            selected_backend="single",
        )

    native_enabled = os.getenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", "0") == "1"
    allow_fallback = os.getenv("RALFLOOP_ALLOW_TEXT_MAS_FALLBACK", "1") == "1"
    requested = "recursive_mas_native" if native_enabled else "text_proxy"
    device = os.getenv("RALFLOOP_RECURSIVE_MAS_DEVICE", "cpu")

    if native_enabled and not route_only:
        adapter = RecursiveMASNativeAdapter()
        probe = adapter.probe(style=style, device=device)
        if probe.available:
            native = collaboration_backend_config(config, "recursive_mas_native")
            return CollaborationBackend(
                backend_name=native.get("backend_name", "RecursiveMAS"),
                implementation_level="native_latent",
                style=style,
                recursion_rounds=int(native.get("recursion_rounds", 3)),
                available=True,
                availability_reason="native_probe_available",
                native_latent=True,
                intermediate_decode_policy=native.get("intermediate_decode_policy", "no_text_decode_until_final_round"),
                style_selection_source=style_selection_source,
                source=native.get("repository_url", "https://github.com/RecursiveMAS/RecursiveMAS"),
                requested_backend=requested,
                selected_backend="recursive_mas_native",
                checkpoints_downloaded=probe.checkpoints_downloaded,
                checkpoint_manifest_complete=probe.checkpoint_manifest_complete,
                offline_ready=probe.offline_ready,
                load_check_passed=probe.load_check_passed,
                native_canary_passed=probe.native_canary_passed,
                native_execution_verified=probe.native_execution_verified,
                native_latent_verified=probe.native_latent_verified,
            )
        fallback_reason = probe.reason
    elif native_enabled and route_only:
        fallback_reason = "route_only_does_not_probe_or_load_native_backend"
    else:
        fallback_reason = "native_backend_disabled_by_default"

    if allow_fallback:
        text = collaboration_backend_config(config, "text_proxy")
        return CollaborationBackend(
            backend_name=text.get("backend_name", "text_mas_proxy"),
            implementation_level="text_proxy",
            style=style,
            recursion_rounds=int(text.get("recursion_rounds", 3)),
            available=True,
            availability_reason=text.get("availability_reason", "text proxy available"),
            native_latent=False,
            intermediate_decode_policy=text.get("intermediate_decode_policy", "compact_text_state"),
            style_selection_source=style_selection_source,
            source="ralfloop_text_proxy",
            requested_backend=requested,
            selected_backend="text_proxy",
            fallback_used=requested != "text_proxy",
            fallback_reason=fallback_reason if requested != "text_proxy" else None,
        )

    return CollaborationBackend(
        backend_name="unavailable",
        implementation_level="routing_only",
        style=style,
        recursion_rounds=0,
        available=False,
        availability_reason=fallback_reason,
        native_latent=False,
        intermediate_decode_policy="none",
        style_selection_source=style_selection_source,
        source="ralfloop",
        requested_backend=requested,
        selected_backend="unavailable",
        fallback_used=False,
        fallback_reason=fallback_reason,
    )
