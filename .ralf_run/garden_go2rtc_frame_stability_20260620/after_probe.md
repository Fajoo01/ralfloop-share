# Frame Source Probe: after

- generated_at: `2026-06-26T11:07:19.572Z`
- attempts_per_candidate: `80`
- timeout_seconds: `8.0`
- interval_seconds: `0.2`
- json: `after_probe.json`

| source | kind | attempts | ok | errors | timeouts | avg_s | p50_s | p95_s | max_s | valid_jpeg | smear/glitch | smear_rate | file_age_p95_s |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| go2rtc_frame | http | 80 | 70 | 10 | 2 | 4.7374 | 4.7533 | 5.3209 | 8.0091 | 70 | 12 | 0.15 | n/a |
| warmer_cache_file | file | 80 | 80 | 0 | 0 | 0.0001 | 0.0001 | 0.0002 | 0.0003 | 80 | 2 | 0.025 | 8.234 |
