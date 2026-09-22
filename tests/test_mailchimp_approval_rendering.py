from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore


def test_mailchimp_approval_message_surfaces_subject_and_preheader(tmp_path):
    policy = DomainApprovalPolicy(
        enabled=True,
        allowed_user_ids={11},
        allowed_chat_ids={22},
        db_path=str(tmp_path / "approvals.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )
    store = DomainApprovalStore(policy=policy)
    scope = {
        "campaign_id": "campaign_1",
        "subject": "27 settembre · Festa di tesseramento",
        "preheader": "Nel cuore del quartiere",
        "title": "Festa di tesseramento 2026",
    }
    request = store.create_request(
        action="mailchimp_campaign_send",
        bando_id="mailchimp.marketing",
        version="1",
        scope=scope,
    )["request"]
    message = request["telegram_message"]
    assert "Oggetto: 27 settembre · Festa di tesseramento" in message
    assert "Preheader: Nel cuore del quartiere" in message
    assert "Titolo interno: Festa di tesseramento 2026" in message
