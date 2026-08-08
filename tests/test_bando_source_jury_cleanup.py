import json

from ralfloop_agent.domains.bando_source_jury_scheduler import SourceJuryBatchScheduler, SourceJuryVramPolicy


def test_cleanup_runs_between_batches_and_writes_audit(tmp_path):
    cleanup_calls = []

    def cleanup_probe():
        cleanup_calls.append("probe")
        return []

    def runner(rows, _batch_id):
        return {
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

    scheduler = SourceJuryBatchScheduler(
        SourceJuryVramPolicy(max_candidates=2, batch_size=1),
        memory_probe=lambda: {"total_vram_bytes": 8, "free_vram_before_bytes": 8},
        cleanup_probe=cleanup_probe,
    )

    out = scheduler.run([{"candidate_id": "a"}, {"candidate_id": "b"}], runner=runner, audit_dir=tmp_path)

    assert out.completed_count == 2
    assert len(cleanup_calls) >= 4
    assert (tmp_path / "batch-01-cleanup.json").exists()
    assert json.loads((tmp_path / "aggregate-result.json").read_text())["completed_count"] == 2
