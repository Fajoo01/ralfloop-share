# Garden go2rtc frame source comparison

| source | metric | before | after | delta |
|---|---|---:|---:|---:|
| go2rtc_frame | attempts | 80 | 80 | 0 |
| go2rtc_frame | ok | 74 | 70 | -4 |
| go2rtc_frame | errors | 6 | 10 | 4 |
| go2rtc_frame | timeouts | 1 | 2 | 1 |
| go2rtc_frame | avg_time_s | 4.6321 | 4.7374 | 0.1053 |
| go2rtc_frame | p95_time_s | 4.9632 | 5.3209 | 0.3577 |
| go2rtc_frame | max_time_s | 8.0065 | 8.0091 | 0.0026 |
| go2rtc_frame | valid_jpeg | 74 | 70 | -4 |
| go2rtc_frame | smear_glitch | 9 | 12 | 3 |
| go2rtc_frame | smear_rate | 0.1125 | 0.15 | 0.0375 |
| go2rtc_frame | file_age_p95_s | n/a | n/a | n/a |
| go2rtc_frame | file_age_max_s | n/a | n/a | n/a |
| warmer_cache_file | attempts | 80 | 80 | 0 |
| warmer_cache_file | ok | 80 | 80 | 0 |
| warmer_cache_file | errors | 0 | 0 | 0 |
| warmer_cache_file | timeouts | 0 | 0 | 0 |
| warmer_cache_file | avg_time_s | 0.0001 | 0.0001 | 0.0 |
| warmer_cache_file | p95_time_s | 0.0002 | 0.0002 | 0.0 |
| warmer_cache_file | max_time_s | 0.0003 | 0.0003 | 0.0 |
| warmer_cache_file | valid_jpeg | 80 | 80 | 0 |
| warmer_cache_file | smear_glitch | 5 | 2 | -3 |
| warmer_cache_file | smear_rate | 0.0625 | 0.025 | -0.0375 |
| warmer_cache_file | file_age_p95_s | 5.232 | 8.234 | 3.002 |
| warmer_cache_file | file_age_max_s | 9.906 | 10.378 | 0.472 |
