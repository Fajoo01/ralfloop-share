import pytest

from ralfloop_agent.providers.llm_profiles import LlmProfileError, load_llm_profile


def test_default_is_legacy_and_production_compatible():
    profile = load_llm_profile({})
    assert profile.name == "legacy"
    assert profile.enabled is True
    assert profile.spec_type == "none"


def test_qwen35_is_opt_in_and_requires_explicit_model_path():
    with pytest.raises(LlmProfileError, match="qwen35_model_path_required"):
        load_llm_profile({"BOTTAZZI_LLM_PROFILE": "qwen35"})


def test_selected_qwen35_profile_is_enabled():
    profile = load_llm_profile({
        "BOTTAZZI_LLM_PROFILE": "qwen35",
        "BOTTAZZI_QWEN35_MODEL_PATH": "/models/qwen35.gguf",
    })
    assert profile.enabled is True


def test_ngram_profile_emits_current_upstream_flag():
    profile = load_llm_profile({
        "BOTTAZZI_LLM_PROFILE": "qwen35_ngram",
        "BOTTAZZI_QWEN35_MODEL_PATH": "/models/qwen35.gguf",
    })
    args = profile.server_args()
    assert args[args.index("--spec-type") + 1] == "ngram-mod"
    assert args[args.index("--spec-ngram-mod-n-min") + 1] == "48"
    assert "--cpu-moe" in args
    assert "--cache-reuse" in args
    assert args[args.index("--fit-target") + 1] == "1024"
    assert args[args.index("--parallel") + 1] == "1"


def test_unvalidated_combined_speculation_fails_closed():
    with pytest.raises(LlmProfileError, match="combined_speculation_not_validated"):
        load_llm_profile({
            "BOTTAZZI_LLM_PROFILE": "qwen35_experimental",
            "BOTTAZZI_QWEN35_MODEL_PATH": "/models/qwen35.gguf",
            "BOTTAZZI_QWEN35_SPEC_TYPE": "draft-mtp,ngram-mod",
        })


def test_mtp_requires_verified_layers():
    with pytest.raises(LlmProfileError, match="mtp_layers_not_verified"):
        load_llm_profile({
            "BOTTAZZI_LLM_PROFILE": "qwen35_experimental",
            "BOTTAZZI_QWEN35_MODEL_PATH": "/models/qwen35.gguf",
            "BOTTAZZI_QWEN35_SPEC_TYPE": "draft-mtp",
        })
