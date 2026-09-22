#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

EXPECTED_MCP_VERSION = "0.0.82"
DEFAULT_ROOT = Path("/home/bandi/.local/share/ralf-playwright-mcp")

ORIGINAL = '''              const resolved = await locator2.normalize();
              return { locator: locator2, resolved: resolved.toString(), selector: locatorSelector(resolved) };
'''

PATCHED = '''              const resolved = await locator2.normalize();
              return { locator: resolved, resolved: resolved.toString(), selector: locatorSelector(resolved) };
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    package = args.root / "node_modules" / "@playwright" / "mcp" / "package.json"
    bundle = args.root / "node_modules" / "playwright-core" / "lib" / "coreBundle.js"
    version = str(json.loads(package.read_text(encoding="utf-8"))["version"])
    if version != EXPECTED_MCP_VERSION:
        raise SystemExit(f"unsupported_playwright_mcp_version:{version}")

    text = bundle.read_text(encoding="utf-8")
    if PATCHED in text:
        print(f"playwright_mcp_normalized_locator_patch=already_applied version={version}")
        return 0
    if ORIGINAL not in text:
        raise SystemExit("unsupported_playwright_core_layout")

    updated = text.replace(ORIGINAL, PATCHED, 1)
    bundle.write_text(updated, encoding="utf-8")
    print(f"playwright_mcp_normalized_locator_patch=applied version={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
