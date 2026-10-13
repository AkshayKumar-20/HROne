# REVIEW.md — Starter review (`app/main.py`)

Reviewed as a teammate PR: ran happy path, then probed concurrency (5 parallel punch-ins), 100k-scale list, and edge inputs (`EMP9999`, `09:40:01`, `2026-02-30`, overnight `22:00-06:00`, legacy docs without `history`). Every row below failed before the fix and passes after. `test_work.py` is `ALL PASS`; all 5 `explain` plans show `IXSCAN`, no `COLLSCAN`.

| # | Where | What is wrong | How noticed (failing input / symptom) | Fix |
|---|---|---|---|---|
| 1 | `GET /health` | Always `200`, never pings Mongo | Stop Mongo → still `200`; readiness meaningless | `client.admin.command("ping")`, `503` on `PyMongoError` |
| 2 | `compute_late_minutes` | `.replace()` on punch datetime, no IST date, no truncation, `int()` not floor, no overnight | `22:00-06:00` punch or `09:40:00.900` → wrong late | Truncate to seconds, shift start from IST attendance date (R1), floor minutes, late only if `>10:00` (R2) |
| 3 | `compute_work_hours` | Python `round()` (banker's), untruncated seconds | `round(2.675,2)=2.67` not `2.68` (R4) | `Decimal.quantize(ROUND_HALF_UP)` on truncated seconds |
| 4 | `compute_overtime` | No 30-min minimum, `int()` not floor, no truncation, overnight end on same day | `+5 min` → `5` not `0`; overnight end wrong day | Floor minutes from shift end (next day if overnight), only if `>=30` (R3) |
| 5 | `POST /employees` | Check-then-insert race | 2 parallel same `emp_code` → two `201` | Unique `emp_code` index; `DuplicateKeyError→409` |
| 6 | `POST /employees` | No validation (`emp_code`/email/shift, `start==end`) | `emp_code="X"`, `09:30-09:30` accepted | Pydantic patterns + explicit `422` for `start==end` |
| 7 | `POST /employees` | `datetime.now()` naive local; returns BSON datetime | `created_at` not epoch-ms int, wrong tz | `now(timezone.utc)`; serialize to epoch ms |
| 8 | `GET /employees` | `skip=page*page_size` off-by-one | `page=1` skips first page | `(page-1)*page_size` |
| 9 | `GET /employees` | `total=count({})` ignores filter (R10) | `?department=Sales` total = whole collection | `count_documents(q)` |
| 10 | `GET /employees` | No `emp_code` sort; no pagination validation | Unsorted; `page_size=1000000` accepted | `.sort(emp_code)`; `Query(ge=1,le=100)` |
| 11 | `POST /punch-in` | No employee lookup → `None["shift"]` → `500` | `emp_code=EMP9999` → `500` not `404` | `_get_employee_or_404` |
| 12 | `POST /punch-in` | Check-then-insert race on `(emp_code,date)` | 5 parallel punch-ins → multiple `201` | Unique `(emp_code,date)`; `DuplicateKeyError→409` |
| 13 | `POST /punch-in` | `fromtimestamp(/1000)` local tz; `ts.date()` not IST; no truncation/overnight | Midnight punch wrong IST date; overnight wrong day | Truncate; `attendance_date_for()` R1 overnight rule |
| 14 | `POST /punch-in` | `status`/`punched_at` unvalidated | `ABSENT` accepted; seconds `1700000000` accepted | Pydantic: presence-only + EpochMillis range → `422` |
| 15 | `POST /punch-in` | Leaks `_id` as `id`; returns datetimes | Response has `id`, non-int times | Contract serializer; never return `_id` |
| 16 | `GET /attendance` | Loads whole collection, sorts/paginates in Python | 100k docs → slow/OOM | DB filter/sort/skip/limit |
| 17 | `GET /attendance` | No secondary sort; `total=len(docs)`; no date check; leaks `id` | `date_from>date_to` not `422` | Sort `(date desc, emp_code)`; `422` on bad range; serializer |
| 18 | Cross-cutting | No indexes, no ms↔datetime boundary, no Pydantic → no `422`s, races everywhere | Parallel tests fail; bad input accepted | Idempotent indexes at startup; conditional updates; full models; boundary helpers |

## Hardening found while building Part B/C (not starter defects)

| # | Where | Symptom before | Fix |
|---|---|---|---|
| 19 | `explain` serializer | All 5 `explain` → `500 Timestamp is not iterable` (`json_util.loads` round-trips BSON) | `json.loads(json_util.dumps(...))` → plain JSON |
| 20 | `trend` explain plan | `DISTINCT_SCAN`, no `IXSCAN` (simplified `$group _id` only) | Explain exact endpoint pipeline |
| 21 | `GET /attendance`, `PATCH …/{date}`, `trend` | `2026-02-30` (passes regex, fails `fromisoformat`) → `500` | `try/except ValueError → 422` |
| 22 | `PATCH` body `=None` | No body → `AttributeError` → `500` | Required body (`=...`) → `422` |
| 23 | `PATCH` concurrency filter | Legacy doc without `history` never matches `{"history":[]}` → spurious `409` | Match `{"history":{$exists:false}}` when missing |
| 24 | R2 boundary | `09:40:01` → `0` not `10` (compared floored minutes `10>10`) | Compare ms `>600000`, then `//60000` |
| 25 | `POST /punch-out` query | `ABSENT` skeleton (`punch_in:null`) matches `$lte` → `dt_to_ms(None)` → `500` | `punch_in:{$ne:null,$lte:…}` + null guard → `404` |
| 26 | Trend implementation | Gap-fill + 7-day avg looped in Python (contract: `$densify` in DB) | Full pipeline: `$group → $densify → $lookup headcount → $setWindowFields → half-up via `floor(x*10000+0.5)/10000`` |
| 27 | `dt_to_ms` | `total_seconds()*1000` float precision loss | Integer arithmetic `(days*86400+seconds)*1000+micros//1000` |
| 28 | Startup | `@on_event` deprecated; `EPOCH` used before definition; dead `_weekday_expr`/`_ist_wallclock` | `lifespan` + `on_event` compat, `EPOCH` first, removed dead code |

## Examined and left alone

- `GET /health 200` when Mongo is up — correct; only missing `503` was the bug.
- `AttendanceRecord`/`Employee` shapes — correct; only serialization leaked.
- Single-file layout — required; kept. Extra fields (derived) are ignored by Pydantic per contract, not rejected.
- `month_bounds`, `working_days_count` (Mon-Fri from `joined_on`), `$rank` leaderboard, `employees+$lookup` summary — verified against hand-computed cases (ties, zero-log, joiner, weekend, overnight).
