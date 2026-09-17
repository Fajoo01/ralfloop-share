from scripts.ralf_domotics_health_watchdog import (
    next_reload_candidates, previous_degraded_sets, recovery_eligible_devices,
)


def test_offline_site_is_suppressed_from_global_reload():
    rows = [
        {"device_id": "camper-1", "site": "camper", "entities": ["switch.webasto"]},
        {"device_id": "sede-1", "site": "sede", "entities": ["switch.porte"]},
    ]
    health = {
        "camper": {"status": "offline"},
        "sede": {"status": "degraded"},
    }
    eligible, suppressed = recovery_eligible_devices(rows, health)
    assert eligible == {"device:sede-1": "sede"}
    assert suppressed == ["device:camper-1"]


def test_unassigned_degraded_device_remains_recovery_eligible():
    rows = [{"device_id": "d1", "site": "unassigned", "entities": ["switch.x"]}]
    eligible, suppressed = recovery_eligible_devices(rows, {"unassigned": {"status": "degraded"}})
    assert eligible == {"device:d1": "unassigned"}
    assert suppressed == []


def test_legacy_site_state_reconstructs_suppressed_devices_as_degraded():
    all_degraded, eligible = previous_degraded_sets({
        "degraded_device_keys": ["device:sede"],
        "recovery_suppressed_site_offline": ["device:camper"],
    })
    assert all_degraded == {"device:sede", "device:camper"}
    assert eligible == {"device:sede"}


def test_device_becoming_recovery_eligible_starts_new_persistence_window():
    candidates = next_reload_candidates(
        {"device:camper"}, set(), {}, baseline=False
    )
    assert candidates == {"device:camper": 1}


def test_persistent_candidate_increments_but_completed_recovery_does_not_rearm():
    assert next_reload_candidates(
        {"device:sede"}, {"device:sede"}, {"device:sede": 1}, baseline=False
    ) == {"device:sede": 2}
    assert next_reload_candidates(
        {"device:sede"}, {"device:sede"}, {}, baseline=False
    ) == {}
