from scripts.reconcile_md_goodify_gateway import patch_template


def sample_gateway() -> str:
    return """http {
  upstream tiremm_remote_mcp {
    server 10.44.0.1:31082;
  }
  server {
    location ^~ /account/ {
      proxy_pass http://authentik;
    }
  }
}
"""


def test_adds_md_goodify_relay_before_account_catchall():
    patched, changed = patch_template(sample_gateway())
    assert changed is True
    assert "upstream tiremm_md_goodify" in patched
    assert "server 10.44.0.1:19234;" in patched
    assert "location ^~ /account/md-goodify/" in patched
    assert patched.index("/account/md-goodify/") < patched.index("location ^~ /account/ {")


def test_patch_is_idempotent():
    once, first_changed = patch_template(sample_gateway())
    twice, second_changed = patch_template(once)
    assert first_changed is True
    assert second_changed is False
    assert twice == once
