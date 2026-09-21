# Teacher Model Pool MCP

Internal MCP router for the Gemma 3 4B CPU fallback pool.
It is not exposed through the 13 student-facing Teacher tools.

Config format is whitespace-separated:

```text
# node pod_ip port capacity
sibilla 10.44.0.20 19106 1
temistocle 10.44.1.20 19106 1
```

The MCP exposes only:
- `pool.acquire(model)` → bounded worker lease + fixed configured endpoint.
- `pool.release(lease, latency_ms)` → releases load and updates latency EMA.
- `pool.health()` → reachability and in-flight counters.

Selection rejects saturated or unreachable workers. Leases expire after 240 s,
so a crashed client cannot permanently consume pool capacity.
