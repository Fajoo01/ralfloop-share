from ralfloop_agent.core.capability_router import route_task


def test_mailchimp_read_is_not_external_action():
    route = route_task("controlla le campagne Mailchimp")

    assert route.mcp_connectors == ["mailchimp.marketing"]
    assert route.task_mode == "check_only"
    assert route.write_policy == "no_write"
    assert route.needs_human_confirmation is False
    assert "execute_read_only_mcp" in route.workflow
    assert "write_external_state" in route.blocked_actions


def test_mailchimp_mutation_stays_confirmation_gated():
    route = route_task("invia campagna Mailchimp")

    assert route.mcp_connectors == ["mailchimp.marketing"]
    assert route.task_mode == "external_action"
    assert route.write_policy == "external_side_effect_requires_confirmation"
    assert route.needs_human_confirmation is True
    assert "request_human_confirmation" in route.workflow


def test_existing_gmail_connector_remains_confirmation_gated():
    route = route_task("controlla Gmail")

    assert "google_workspace.gmail" in route.mcp_connectors
    assert route.task_mode == "external_action"
    assert route.needs_human_confirmation is True
