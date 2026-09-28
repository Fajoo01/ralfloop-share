#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys

NAMESPACE = "tiremm-identity"
CONFIGMAP = "tiremm-account-gateway"
DEPLOYMENT = "tiremm-account-gateway"
KEY = "nginx.conf.template"
UPSTREAM = """  upstream tiremm_md_goodify {
    server 10.44.0.1:19234;
  }

"""
LOCATION = """    location = /account/md-goodify {
      return 308 /account/md-goodify/;
    }

    location ^~ /account/md-goodify/ {
      rewrite ^/account/md-goodify/?(.*)$ /$1 break;
      proxy_pass http://tiremm_md_goodify;
      proxy_http_version 1.1;
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-Host $http_host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-Forwarded-Port 9443;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_request_buffering off;
      proxy_buffering off;
    }

"""


def patch_template(template: str) -> tuple[str, bool]:
    changed = False
    if "upstream tiremm_md_goodify" not in template:
        marker = "  upstream tiremm_remote_mcp {\n"
        pos = template.find(marker)
        if pos < 0:
            raise RuntimeError("remote_mcp_upstream_marker_missing")
        template = template[:pos] + UPSTREAM + template[pos:]
        changed = True
    if "location ^~ /account/md-goodify/" not in template:
        marker = "    location ^~ /account/ {\n"
        pos = template.find(marker)
        if pos < 0:
            raise RuntimeError("account_location_marker_missing")
        template = template[:pos] + LOCATION + template[pos:]
        changed = True
    return template, changed


def kubectl(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/local/bin/k3s", "kubectl", *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )


def main() -> int:
    raw = kubectl("get", "configmap", "-n", NAMESPACE, CONFIGMAP, "-o", "json").stdout
    document = json.loads(raw)
    template = document.get("data", {}).get(KEY)
    if not isinstance(template, str):
        raise RuntimeError("gateway_template_missing")
    patched, changed = patch_template(template)
    if not changed:
        print("md_goodify_gateway_ok")
        return 0
    document["data"][KEY] = patched
    document.pop("status", None)
    kubectl("replace", "-f", "-", input_text=json.dumps(document))
    kubectl("rollout", "restart", "deployment", "-n", NAMESPACE, DEPLOYMENT)
    kubectl("rollout", "status", "deployment", "-n", NAMESPACE, DEPLOYMENT, "--timeout=90s")
    print("md_goodify_gateway_restored")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "kubectl_failed").strip().splitlines()[-1]
        print(f"md_goodify_gateway_error:{detail}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(f"md_goodify_gateway_error:{exc}", file=sys.stderr)
        raise SystemExit(3)
