# Schema check (peek sample)

## Counts

| Metric | Value |
|---|---|
| Generated | 2026-10-13T18:19:07Z |
| Messages requested | 200 |
| Messages read | 200 |
| Records in those messages | 200 |
| Records parsed into a Measurement | 200 |
| Parse failures | 0 |
| Records per message (count: messages) | 1: 200 |
| Distinct `locationId` | 49 |
| Distinct natural keys | 197 |
| Natural keys seen more than once | 3 |
| Min `date.utc` | 2026-04-22T01:00:00Z |
| Max `date.utc` | 2026-05-03T08:00:00Z |
| Min SQS `SentTimestamp` | 2026-09-29T18:31:47Z |
| Max SQS `SentTimestamp` | 2026-10-11T01:31:48Z |
| `ApproximateReceiveCount` (value: messages) | 1: 198, 2: 2 |

## Fields

| Field | Contract | Records missing it |
|---|---|---|
| `locationId` | required | 0 |
| `parameter` | required | 0 |
| `value` | required | 0 |
| `unit` | required | 0 |
| `date.utc` | required | 0 |
| `location` | optional | 0 |
| `city` | optional | 0 |
| `country` | optional | 0 |
| `coordinates.latitude` | optional | 0 |
| `coordinates.longitude` | optional | 0 |
| `date.local` | optional | 0 |
| `entity` | optional | 0 |
| `sensorType` | optional | 0 |
| `isMobile` | optional | 0 |
| `isAnalysis` | optional | 0 |

## Distinct values

### `parameter` (normalised)

| Value | Records |
|---|---|
| `pm25` | 54 |
| `pm10` | 51 |
| `no2` | 48 |
| `o3` | 33 |
| `so2` | 14 |

### `unit`

| Raw | Normalised | Status | Records |
|---|---|---|---|
| `µg/m³` | `μg/m3` | ok | 200 |

### `entity`

| Value | Records |
|---|---|
| `'Governmental Organization'` | 200 |

### `sensorType`

| Value | Records |
|---|---|
| `'reference grade'` | 151 |
| `'low-cost'` | 49 |

### `isMobile`

| Value | Records |
|---|---|
| `False` | 200 |

### `isAnalysis`

| Value | Records |
|---|---|
| `False` | 200 |

### `country`

| Value | Records |
|---|---|
| `'BE'` | 200 |

## Raw values per parameter

| Parameter | n | Min | Median | Max | Negative |
|---|---|---|---|---|---|
| `no2` | 48 | 16.7 | 48.6 | 74.8 | 0 |
| `o3` | 33 | 4.4 | 9 | 31.1 | 0 |
| `pm10` | 51 | 15.8 | 65.9 | 121.5 | 0 |
| `pm25` | 54 | 9.4 | 41.65 | 80.5 | 0 |
| `so2` | 14 | 10.6 | 39.2 | 50.5 | 0 |

## Station names

| Metric | Value |
|---|---|
| `locationId` with more than one `location` name | 0 |
| `location` names shared by more than one `locationId` | 0 |
