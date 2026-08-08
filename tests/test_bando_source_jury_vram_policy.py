from ralfloop_agent.domains.bando_source_jury_scheduler import GIB, SourceJuryVramPolicy


def test_vram_policy_selects_expected_auto_sizes():
    policy = SourceJuryVramPolicy(max_candidates=8)

    assert policy.selected_batch_size({"total_vram_bytes": 8 * GIB, "free_vram_before_bytes": 8 * GIB}) == 3
    assert policy.selected_batch_size({"total_vram_bytes": 12 * GIB, "free_vram_before_bytes": 12 * GIB}) == 4
    assert policy.selected_batch_size({"total_vram_bytes": 24 * GIB, "free_vram_before_bytes": 24 * GIB}) == 8


def test_vram_policy_honors_manual_override():
    policy = SourceJuryVramPolicy(max_candidates=8, batch_size=2)

    assert policy.selected_batch_size({"total_vram_bytes": 24 * GIB, "free_vram_before_bytes": 24 * GIB}) == 2
