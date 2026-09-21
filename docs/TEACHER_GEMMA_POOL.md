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
The Teacher should acquire a lease, call the selected Ollama endpoint, and
release the lease in a `finally` path.
