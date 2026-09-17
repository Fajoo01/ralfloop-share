from scripts.ralf_domotics_health_watchdog import recovery_eligible_devices


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
