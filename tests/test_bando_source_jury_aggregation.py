from ralfloop_agent.domains.bando_source_jury_scheduler import SourceJuryBatchScheduler, SourceJuryVramPolicy


def test_aggregate_counts_final_uses_and_native_flags():
    uses = ["binding", "supporting", "discovery_only", "reject"]

    def runner(rows, _batch_id):
        return {
            "native_latent_verified": True,
            "fallback": False,
            "deterministic_results": [
                {
                    "candidate_id": row["candidate_id"],
                    "deterministic_assessment": {"deterministic_gate": True, "authority_level": "A"},
                    "jury_assessment": {},
                    "final_assessment": {"recommended_use": row["use"], "binding_eligible": row["use"] == "binding"},
                }
                for row in rows
            ],
            "native_result": {"status": "completed"},
        }

    scheduler = SourceJuryBatchScheduler(
        SourceJuryVramPolicy(max_candidates=4, batch_size=2),
        memory_probe=lambda: {"total_vram_bytes": 8, "free_vram_before_bytes": 8},
        cleanup_probe=lambda: [],
    )
    candidates = [{"candidate_id": f"c{i}", "use": use} for i, use in enumerate(uses)]

    out = scheduler.run(candidates, runner=runner)

    assert out.completed_count == 4
    assert out.batch_count == 2
    assert out.native_latent_verified_all_batches is True
    assert out.binding_count == 1
    assert out.supporting_count == 1
    assert out.discovery_only_count == 1
    assert out.rejected_count == 1
    assert out.fallback is False
