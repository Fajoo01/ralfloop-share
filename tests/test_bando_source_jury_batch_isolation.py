from ralfloop_agent.domains.bando_source_jury_scheduler import SourceJuryBatchScheduler, SourceJuryVramPolicy


def test_hard_gate_failure_is_not_overridden_by_batching():
    def runner(rows, _batch_id):
        return {
            "native_latent_verified": True,
            "fallback": False,
            "deterministic_results": [
                {
                    "candidate_id": row["candidate_id"],
                    "deterministic_assessment": {"deterministic_gate": row["gate"], "authority_level": row["level"]},
                    "jury_assessment": {},
                    "final_assessment": {"recommended_use": "binding", "binding_eligible": True},
                }
                for row in rows
            ],
            "native_result": {"status": "completed"},
        }

    scheduler = SourceJuryBatchScheduler(
        SourceJuryVramPolicy(max_candidates=2, batch_size=2),
        memory_probe=lambda: {"total_vram_bytes": 8, "free_vram_before_bytes": 8},
        cleanup_probe=lambda: [],
    )

    out = scheduler.run(
        [
            {"candidate_id": "official", "gate": True, "level": "A"},
            {"candidate_id": "blog", "gate": False, "level": "D"},
        ],
        runner=runner,
    )

    assert [item["candidate_id"] for item in out.to_dict()["candidates"]] == ["official", "blog"]
    failed = out.to_dict()["candidates"][1]["final_assessment"]
    assert failed["binding_eligible"] is False
    assert failed["recommended_use"] == "reject"
