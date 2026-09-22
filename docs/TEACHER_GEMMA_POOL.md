# Teacher Gemma 3 4B pool

Goal: use the nine Temistocle-class PCs as independent warm replicas, not as
cross-node tensor/layer sharding. A single warm request remains fastest on
Sibilla; the replicas increase concurrency, failover capacity and tail latency.

## Worker contract

A worker is labeled `ralf-teacher-gemma=ready` only after it has:
- at least 4 CPU cores and about 8 GiB RAM;
- at least 8 GB free disk space;
- `/usr/local/bin/ollama` and `/usr/local/lib/ollama`;
- `gemma3:4b` available locally;
- a Ready k3s node.

Run the preflight from Sibilla:

```bash
python3 tools/prepare_teacher_gemma_worker.py NODE NODE --dry-run
python3 tools/prepare_teacher_gemma_worker.py NODE NODE --pull
```

## Kubernetes layout

`deploy/k8s/teacher-gemma-pool.yaml` is a DaemonSet. It runs one CPU-only
Ollama endpoint on every ready-labeled worker. The model store, Ollama binary
and runtime libraries are mounted read-only from the host, so the pod does not
copy the 3.3 GB model into container storage.

Pods use the k3s overlay network instead of host networking. The headless
Service is for per-replica discovery; the normal ClusterIP Service is a simple
fallback path. No Ollama port needs to be opened on the VPN interface.

Each replica has one inference slot (`OLLAMA_NUM_PARALLEL=1`) and a 4096-token
context. Requests/limits are sized for the measured Temistocle-class hardware.
Do not label a node that is already short on host RAM.

## Routing

`tools/teacher_model_pool_mcp` is the internal C MCP. It uses bounded leases
rather than blind round-robin. An endpoint at capacity is not selected, dead
TCP endpoints are skipped, and observed latency is kept as an EMA tie-breaker.
The production release builder installs it as
`bin/ralf-teacher-model-pool-mcp`.

The Teacher inference daemon keeps the pool opt-in. Without
`RALF_TEACHER_MODEL_POOL_CONFIG` it behaves exactly as before and uses the
loopback `gemma3:4b` fallback. When that variable points to a pool config, the
fallback acquires a lease, sends the same compact/schema-bound Gemma request to
the selected endpoint, records latency on `pool.release`, and always releases
the lease in a `finally` path. Pool saturation, an unhealthy endpoint, a bad
lease, or a failed pooled inference falls back to the existing loopback Ollama
instead of making the student request less reliable.

The pool remains internal and does not add tools to the 13-tool student MCP
surface. A configured worker endpoint must be a plain HTTP IP/localhost
endpoint returned by the trusted pool MCP; arbitrary hostnames, credentials,
paths, query strings and fragments are rejected.

## Canary activation

Do not set `RALF_TEACHER_MODEL_POOL_CONFIG` in the production unit merely
because the code is installed. First label only the verified canary nodes,
apply the DaemonSet, build a temporary config from the resulting pod IPs, and
measure acquire/inference/release latency. The current production inference
unit permits network access only to localhost, so a remote pool also requires a
separate reviewed systemd network-policy change before production activation.
Until that policy and the canary are green, the production daemon must stay on
the local `gemma3:4b` fallback.
