# Full-suite baseline comparison

Date: 2026-09-03. Baseline: `0242e79`. Current: baseline plus Bottazzi M1-M5 uncommitted changes. Production not contacted.

## Verdict

`GREEN RELATIVE`: current through M6 introduces zero deterministic failures. Exact JUnit failure identity sets are equal (65/65). Current adds twelve passing tests.

| Metric | CURRENT | BASELINE | DELTA |
|---|---:|---:|---:|
| passed | 2195 | 2183 | +12 |
| failed | 65 | 65 | 0 |
| skipped | 17 | 17 | 0 |
| exit code | 139 | 139 | 0 |
| failure identity set | 65 | 65 | equal |
| new regression | 0 | n/a | 0 |

Command was identical in both worktrees:

```bash
uv run --with-requirements requirements.txt --extra dev \
  --with pyyaml --with beautifulsoup4 --with pypdf --with httpx2 \
  --with torch --with transformers \
  pytest -q --tb=line --junitxml=RESULT.xml
```

## Environment

| Component | Version/state |
|---|---|
| Python | 3.12.3 |
| uv | 0.11.6 |
| pytest | 8.4.2 |
| PyTorch | 2.14.0+cu130 |
| PyTorch CUDA build | 13.0 |
| CUDA available | false |
| NVIDIA driver | 535.309.01 |
| NVIDIA runtime CUDA | 12.2 |
| pydantic | 2.13.5 |
| FastAPI | 0.141.1 |
| Starlette | 1.6.0 |
| transformers | 5.16.1 |

PyTorch reports driver too old for its CUDA 13 build. Both runs reproduce namespace registration/internal storage errors and exit 139 after pytest summary. SIGSEGV is therefore baseline-reproducible and isolated to the untouched Torch/recursive-MAS family.

## Failure families

Every listed failure has primary classification `BASELINE_PRESENT`. Secondary diagnosis:

| Family | Count | Secondary classification | First error |
|---|---:|---|---|
| ABC formula/memory drift | 10 | BASELINE_PRESENT | `KeyError: observed_fact:micro_riparazione_privata`; missing `build_current_context` |
| Recursive MAS Torch/provenance | 47 | ENVIRONMENTAL / BASELINE_PRESENT | duplicate `TORCH_LIBRARY triton`, `storage_module` assert, or expected executor SHA drift |
| Runtime/broker/portal lifecycle | 4 | ENVIRONMENTAL / BASELINE_PRESENT | permission denied external audit path; broker reset; router API drift; missing shutdown callback |
| Domain/bando/GPU handoff state | 3 | BASELINE_PRESENT | checksum/registry/source-text expectation drift |
| Legacy routing evidence | 1 | BASELINE_PRESENT | expected tests evidence, got empty list |

Family counts are disjoint and total 65. Exact identity set follows.

## Exact failed tests

All entries: `BASELINE_PRESENT`. `NEW_REGRESSION`: none. `UNKNOWN`: none.

```text
tests.test_abc_formula_loop::test_formula_uses_merged_context
tests.test_abc_formula_loop::test_llm_evidence_extractor_captures_20260708_family_evening_delta
tests.test_abc_formula_loop::test_next_morning_repair_offsets_late_third_presence_without_erasing_it
tests.test_abc_formula_loop::test_private_repair_delta_scores_as_autonomous_evidence
tests.test_abc_formula_loop::test_private_repair_does_not_cancel_social_exclusion_cap
tests.test_abc_formula_loop::test_social_exclusion_cap_remains_with_bounded_third_delta
tests.test_abc_formula_loop::test_structured_auto_invito_accepts_reported_indirect_quote
tests.test_abc_formula_loop::test_structured_auto_invito_implicito_scores
tests.test_abc_formula_loop::test_structured_food_opening_removes_logistica_pura
tests.test_abc_formula_loop::test_third_presence_with_no_affection_no_overnight_autonomy_is_small_delta
tests.test_capability_runtime_integration::test_openshell_external_action_requires_confirmation_before_runtime
tests.test_domain_capability_mapping::test_mapping_sources_verify_against_declared_checksums
tests.test_first_real_bando_domain::test_registry_tracks_draft_without_active_route
tests.test_llama_cpp_lifecycle_reaper::test_backend_shutdown_invokes_managed_child_reaper
tests.test_mcp_transport_real::test_real_broker_initialize_and_tools_list
tests.test_portal_backend::test_backend_registers_portal_routes
tests.test_ralf_gpu_handoff::test_magnolia_host_runner_loads_existing_safe_approval_configuration
tests.test_recursive_mas_bounded_provenance::test_feature_math_and_telegram_invariants
tests.test_recursive_mas_domain_forensics::test_adapter_modifies_hidden_state_with_finite_statistics
tests.test_recursive_mas_domain_forensics::test_checkpoint_hash_and_opened_path_are_verified
tests.test_recursive_mas_domain_forensics::test_optimizer_contains_parameters_gradients_and_changes_weight
tests.test_recursive_mas_domain_forensics::test_save_reload_preserves_final_weight_not_metadata_only
tests.test_recursive_mas_domain_h2::test_new_outer23_namespace_and_fp32
tests.test_recursive_mas_domain_h2::test_response_only_ce_mask
tests.test_recursive_mas_domain_instruct_training::test_final_decode_loss_backpropagates_through_solver_inner_and_outer23
tests.test_recursive_mas_domain_instruct_training::test_final_decode_rejects_sequence_length_that_cannot_hold_contract
tests.test_recursive_mas_domain_instruct_training::test_final_token_ce_weight_dominates_all_surrogate_losses
tests.test_recursive_mas_domain_instruct_training::test_new_adapters_are_fresh_and_use_instruct_dimensions_without_math_state
tests.test_recursive_mas_domain_instruct_training::test_response_mask_supervises_only_target_tokens
tests.test_recursive_mas_domain_provenance::test_feature_math_and_telegram_invariants
tests.test_recursive_mas_domain_serialization::test_feature_flag_math_profile_and_telegram_gate_remain_invariant
tests.test_recursive_mas_domain_training::test_objective_is_finite_and_zero_for_identical_nonzero_vectors
tests.test_recursive_mas_domain_training::test_one_step_training_and_checkpoint_roundtrip
tests.test_recursive_mas_domain_training::test_ten_step_overfit_reduces_loss
tests.test_recursive_mas_external_benchmark::test_feature_math_and_telegram_invariants
tests.test_recursive_mas_provenance_selector_diagnostics::test_math_profile_and_telegram_gate_unchanged
tests.test_recursive_mas_qwen3_final_benchmark::test_feature_math_and_telegram_invariants
tests.test_recursive_mas_qwen3_heldout_benchmark::test_feature_math_and_telegram_invariants
tests.test_recursive_mas_qwen3_micro_overfit::test_checkpoint_save_reload_preserves_weight_and_hash
tests.test_recursive_mas_qwen3_micro_overfit::test_final_ce_gradients_reach_stage_a_adapters_only_after_detach
tests.test_recursive_mas_qwen3_micro_overfit::test_fresh_adapter_namespace_has_no_outer31
tests.test_recursive_mas_qwen3_micro_overfit::test_hidden_cache_hash_mask_and_namespace
tests.test_recursive_mas_qwen3_micro_overfit::test_math_feature_and_telegram_invariants
tests.test_recursive_mas_qwen3_micro_overfit::test_response_only_loss_mask_masks_prompt_and_padding
tests.test_recursive_mas_qwen3_micro_overfit::test_solver_base_is_frozen
tests.test_recursive_mas_qwen3_micro_overfit::test_stagewise_surrogate_updates_planner_inner_and_outer12
tests.test_recursive_mas_qwen3_solver_eval::test_feature_math_and_telegram_invariants
tests.test_recursive_mas_qwen3_solver_protocol::test_feature_math_and_telegram_invariants
tests.test_recursive_mas_tokenwise_inner::test_assistant_mask_includes_eos_and_excludes_prompt
tests.test_recursive_mas_tokenwise_inner::test_fp32_adapter_optimizer
tests.test_recursive_mas_tokenwise_inner::test_nearest_token_metric
tests.test_recursive_mas_tokenwise_inner::test_no_pooling_and_variable_sequence_length_contract
tests.test_recursive_mas_tokenwise_inner::test_order_preservation
tests.test_recursive_mas_tokenwise_inner::test_padding_is_excluded
tests.test_recursive_mas_tokenwise_inner::test_positionwise_loss_ignores_unselected_values
tests.test_recursive_mas_tokenwise_inner::test_prefix_mismatch_rejected
tests.test_recursive_mas_tokenwise_inner::test_save_reload
tests.test_recursive_mas_tokenwise_inner::test_solver_inner_bypassed_and_outer31_excluded
tests.test_recursive_mas_tokenwise_inner::test_t_to_t_plus_one_shift_and_positionwise_target
tests.test_recursive_mas_tokenwise_real::test_delta_regularization
tests.test_recursive_mas_tokenwise_real::test_raw_teacher_forcing_mask_and_padding
tests.test_recursive_mas_tokenwise_real::test_seen_unseen_metrics
tests.test_recursive_mas_tokenwise_real::test_zero_gate_is_trainable
tests.test_recursive_mas_tokenwise_real::test_zero_gated_residual_initialization_exact_identity
tests.test_routing::test_patch_allowed_mode
```

## Evidence and limitations

- JUnit comparison: `current_failures=65`, `baseline_failures=65`, set equality true, additions/removals empty.
- Current has twelve extra passing Bottazzi tests; total collected tests therefore differs by twelve.
- JUnit/log artifacts were kept outside repository under `/tmp`; they are not project deliverables.
- Historical failures were not modified. Stabilization belongs to a separate workstream.
