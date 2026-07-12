from ralfloop_agent.domains.bando_source_jury_scheduler import GIB, SourceJuryBatchScheduler, SourceJuryVramPolicy


def _candidates(count):
    return [{"candidate_id": f"c{i}", "url": f"https://official.example/{i}.pdf"} for i in range(count)]


def test_eight_candidates_on_8gb_are_planned_as_3_3_2():
    scheduler = SourceJuryBatchScheduler(
        SourceJuryVramPolicy(max_candidates=8),
        memory_probe=lambda: {"cuda_available": True, "total_vram_bytes": 8 * GIB, "free_vram_before_bytes": 8 * GIB},
    )

    plan = scheduler.plan(_candidates(8)).to_dict()

    assert plan["selected_batch_size"] == 3
    assert [batch["size"] for batch in plan["batches"]] == [3, 3, 2]
    assert plan["batches"][0]["candidate_ids"] == ["c0", "c1", "c2"]


def test_eight_candidates_on_12gb_are_planned_as_4_4():
    scheduler = SourceJuryBatchScheduler(
        SourceJuryVramPolicy(max_candidates=8),
        memory_probe=lambda: {"cuda_available": True, "total_vram_bytes": 12 * GIB, "free_vram_before_bytes": 12 * GIB},
    )

    assert [batch["size"] for batch in scheduler.plan(_candidates(8)).batches] == [4, 4]


def test_eight_candidates_on_24gb_are_single_batch():
    scheduler = SourceJuryBatchScheduler(
        SourceJuryVramPolicy(max_candidates=8),
        memory_probe=lambda: {"cuda_available": True, "total_vram_bytes": 24 * GIB, "free_vram_before_bytes": 24 * GIB},
    )

    assert [batch["size"] for batch in scheduler.plan(_candidates(8)).batches] == [8]
