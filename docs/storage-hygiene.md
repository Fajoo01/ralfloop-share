# Storage hygiene audit

This repository already contains the capacity checker used by issue #6. The hygiene audit complements it by reporting reclaimable-looking growth before a filesystem becomes critical.

## Safety model

`tools/storage_hygiene_audit.c` is read-only. It never deletes files. It scans only immediate child directories of the configured temporary root, reports directories older than the selected age and larger than the selected size, and optionally measures explicit watched paths such as package caches.

The JSON output always includes `"deletes_files": false`.

## Example

```sh
scripts/run_storage_hygiene_audit.sh \
  --path pip="$HOME/.cache/pip" \
  --path npm="$HOME/.npm/_cacache"
```

Defaults are `/tmp`, two days minimum age and 100 MiB minimum allocated size. Override them with `STORAGE_HYGIENE_TMP_ROOT`, `STORAGE_HYGIENE_AGE_DAYS` and `STORAGE_HYGIENE_MIN_MIB`.

The runner compiles the C helper with the system C compiler when needed and writes the latest report to `$HOME/.local/state/ralfloop-storage-hygiene/latest.json`.

No automatic cleanup is performed. A human or a separately approved maintenance step must decide what can be removed.
