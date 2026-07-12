from ralfloop_agent.domains.bando_source_jury_scheduler import SourceJuryBatchScheduler, SourceJuryVramPolicy


def _ok_runner(rows, _batch_id):
    return {
        "status": "jury_sources_completed",
        "native_latent_verified": True,
        "fallback": False,
        "deterministic_results": [
            {
                "candidate_id": row["candidate_id"],
                "deterministic_assessment": {"deterministic_gate": True, "authority_level": "A"},
                "jury_assessment": {},
                "final_assessment": {"recommended_use": "binding", "binding_eligible": True},
            }
            for row in rows
        ],
        "native_result": {"status": "completed"},
    }


def test_oom_3_retries_with_single_candidates():
    calls = []

    def runner(rows, batch_id):
        calls.append((batch_id, len(rows)))
        if len(rows) > 1:
            raise RuntimeError("CUDA out of memory")
        return _ok_runner(rows, batch_id)

    scheduler = SourceJuryBatchScheduler(
        SourceJuryVramPolicy(max_candidates=3, batch_size=3, oom_backoff=True),
        memory_probe=lambda: {"total_vram_bytes": 8, "free_vram_before_bytes": 8},
        cleanup_probe=lambda: [],
    )

    out = scheduler.run([{"candidate_id": f"c{i}"} for i in range(3)], runner=runner)

    assert calls == [("batch-01", 3), ("batch-02", 1), ("batch-03", 1), ("batch-04", 1)]
    assert out.retry_count == 1
    assert out.oom_count == 1
    assert out.completed_count == 3
    assert out.fallback is False


def test_single_candidate_oom_is_partial_and_human_review():
    def runner(_rows, _batch_id):
        raise RuntimeError("CUDA out of memory")

    scheduler = SourceJuryBatchScheduler(
        SourceJuryVramPolicy(max_candidates=1, batch_size=1, oom_backoff=True),
        memory_probe=lambda: {"total_vram_bytes": 8, "free_vram_before_bytes": 8},
        cleanup_probe=lambda: [],
    )

    out = scheduler.run([{"candidate_id": "c0"}], runner=runner)

    assert out.status == "partial"
    assert out.failed_count == 1
    assert out.human_review_count == 1
    assert out.native_latent_verified is False
