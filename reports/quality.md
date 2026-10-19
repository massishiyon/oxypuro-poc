# Data quality

| metric | value |
|---|---|
| values | 109862 |
| first ts_utc | 2026-04-26 21:00 |
| last ts_utc | 2026-05-24 22:00 |

## Status

| status | values | share % |
|---|---|---|
| valid | 109855 | 99.99 |
| suspect | 7 | 0.01 |
| invalid | 0 | 0.00 |

## Rules

| rule | PM2.5 | PM10 | NO2 | O3 | SO2 | total |
|---|---|---|---|---|---|---|
| negative | 0 | 0 | 0 | 0 | 0 | 0 |
| implausible_high | 0 | 0 | 0 | 0 | 0 | 0 |
| flatline | 0 | 0 | 0 | 0 | 0 | 0 |
| pm_ratio | 0 | 0 | 0 | 0 | 0 | 0 |
| isolated_spike | 1 | 3 | 0 | 3 | 0 | 7 |
| small_negative | 0 | 0 | 0 | 0 | 0 | 0 |
| regional_event | 0 | 1 | 2 | 22 | 0 | 25 |
| no_neighbours | 0 | 674 | 0 | 0 | 0 | 674 |
| location_suspect | 674 | 674 | 674 | 674 | 0 | 2696 |

## Stations

| location_id | name | flag |
|---|---|---|
| 144799 | Merelbeke-Noord | location_suspect |

| location_id | parameter | flag |
|---|---|---|
| 147855 | pm10 | no_neighbours |

| location_id | name | non-valid % |
|---|---|---|
| 144774 | Blankenberge | 0.10 |
| 147857 | Virton | 0.07 |
| 144710 | Aalst | 0.07 |
| 146740 | Sankt Vith | 0.07 |
| 142815 | Leuven | 0.05 |
| 142892 | Balen | 0.05 |

## Non-valid values

| location_id | name | parameter | ts_utc | value | robust_z | status | reasons |
|---|---|---|---|---|---|---|---|
| 146740 | Sankt Vith | o3 | 2026-05-11 13:00 | 77.3 | 6.3 | suspect | isolated_spike |
| 147857 | Virton | pm25 | 2026-05-11 17:00 | 51.0 | 7.1 | suspect | isolated_spike |
| 142892 | Balen | pm10 | 2026-05-12 18:00 | 159.5 | 6.9 | suspect | isolated_spike |
| 144774 | Blankenberge | o3 | 2026-05-16 12:00 | 108.5 | 7.2 | suspect | isolated_spike |
| 144774 | Blankenberge | o3 | 2026-05-16 14:00 | 103.3 | 6.6 | suspect | isolated_spike |
| 142815 | Leuven | pm10 | 2026-05-20 18:00 | 79.2 | 6.4 | suspect | isolated_spike |
| 144710 | Aalst | pm10 | 2026-05-22 17:00 | 75.1 | 6.4 | suspect | isolated_spike |

| metric | value |
|---|---|
| isolated_spike | 7 |
| regional_event | 25 |

## Fault injection

| metric | value |
|---|---|
| faults | 250 |
| seed | 42 |

| fault | intended rule | injected | detected | recall % | not valid % | precision % |
|---|---|---|---|---|---|---|
| spike | `isolated_spike` | 50 | 43 | 86.0 | 88.0 | 91.6 |
| flatline | `flatline` | 50 | 50 | 100.0 | 100.0 | 99.8 |
| negative | `negative` | 50 | 50 | 100.0 | 100.0 | 100.0 |
| unit_glitch | `implausible_high` | 50 | 50 | 100.0 | 100.0 | 100.0 |
| pm_ratio | `pm_ratio` | 50 | 50 | 100.0 | 100.0 | 93.9 |

| metric | value |
|---|---|
| missed spikes | 7 |
| missed: robust z not above threshold | 6 |
| missed: under absolute floor | 0 |
| missed: taken for regional event | 1 |

| location_id | parameter | ts_utc | local_hour | original | value | robust_z | reason |
|---|---|---|---|---|---|---|---|
| 141002 | o3 | 2026-05-19 01:00:00+00:00 | 3 | 17.4 | 87.0 | 5.2 | z |
| 141002 | pm25 | 2026-05-15 14:00:00+00:00 | 16 | 21.5 | 107.5 | 3.6 | z |
| 141004 | no2 | 2026-05-19 02:00:00+00:00 | 4 | 11.6 | 58.0 | 3.9 | z |
| 143210 | no2 | 2026-05-02 03:00:00+00:00 | 5 | 58.2 | 291.0 | 4.7 | z |
| 144701 | o3 | 2026-05-09 05:00:00+00:00 | 7 | 11.6 | 58.0 | 5.0 | z |
| 144775 | so2 | 2026-05-24 17:00:00+00:00 | 19 | 2.2 | 11.0 | 3.8 | z |
| 144778 | pm10 | 2026-05-22 18:00:00+00:00 | 20 | 40.5 | 202.5 | 44.9 | regional |

| metric | value |
|---|---|
| non-valid in real data too | 7 |
| PM partner of injected PM fault | 7 |
| other side effects of injected faults | 8 |
