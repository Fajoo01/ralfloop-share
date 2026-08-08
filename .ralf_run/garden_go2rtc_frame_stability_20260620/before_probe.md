# Frame Source Probe: before

- generated_at: `2026-06-26T10:55:44.007Z`
- attempts_per_candidate: `80`
- timeout_seconds: `8.0`
- interval_seconds: `0.2`
- json: `before_probe.json`

| source | kind | attempts | ok | errors | timeouts | avg_s | p50_s | p95_s | max_s | valid_jpeg | smear/glitch | smear_rate | file_age_p95_s |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| go2rtc_frame | http | 80 | 74 | 6 | 1 | 4.6321 | 4.6488 | 4.9632 | 8.0065 | 74 | 9 | 0.1125 | n/a |
| warmer_cache_file | file | 80 | 80 | 0 | 0 | 0.0001 | 0.0001 | 0.0002 | 0.0003 | 80 | 5 | 0.0625 | 5.232 |
