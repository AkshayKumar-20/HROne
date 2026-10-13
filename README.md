# Employee Attendance & Analytics API

Single-file FastAPI + MongoDB. All application code lives in `app/main.py`.
Contract: `openapi.yaml` (paths, status codes, rules R1–R10). Storage: `DATA_MODEL.md` + `sample_data/`.

## 1. Run

```bash
pip install -r requirements.txt
cp .env.example .env   # set MONGO_URI and MONGO_DB
python sample_seed.py  # optional: loads the 9 sample docs
uvicorn app.main:app --port 8000
```

| Item | Detail |
|---|---|
| Health | `GET /health` → `200 {"status":"ok"}` only if MongoDB answers `ping`, else `503`. Ready in <20s. |
| Docs | `http://localhost:8000/docs` (OpenAPI from code, mirrors `openapi.yaml`) |
| Config | `MONGO_URI` / `MONGO_DB` from real env vars (win over `.env`). Nothing hard-coded. |
| DB | MongoDB 6.0+. All indexes created at startup, idempotently. Seeded DB has only `_id` index — the app adds the rest. |
| Stack | Python 3.11+, FastAPI, PyMongo, `python-dotenv`, `Decimal` half-up (never `round()`). No external network. |

## 2. What is implemented

**Part A (fix starter):** `GET /health`, `POST/GET /employees`, `POST /attendance/punch-in`, `GET /attendance` — ping check, unique-index races, IST dates, truncation, validation, epoch-ms serialization, no `_id` leak, DB-side sort/paginate. See `REVIEW.md`.

**Part B (build):**
- `POST /attendance/punch-out` — closes most-recent open `punch_in<=punched_at` (across midnight), computes R3–R5, conditional update → one `200`, rest `409`.
- `PATCH /attendance/{emp_code}/{date}` — recomputes R2–R5, appends one `history` entry with only changed fields, full `422` matrix, history-equality guard → loser `409`.
- Analytics as pure aggregations reading **stored** derived values: monthly, department summary (`employees+$lookup` so zero-log staff count, avg over records not avg-of-avgs), leaderboard (`$rank` competition, limit after rank), trend (`$densify` gap-fill + `$setWindowFields` 7-day avg fully in DB).

**Part C (prove):** `GET /admin/explain/{attendance_list,employee_monthly,department_summary,late_leaderboard,department_trend}` runs `explain(executionStats)` on the exact endpoint query/pipeline. All 5 return `IXSCAN`, no `COLLSCAN` on 100k docs.

## 3. Rules in one glance (R1–R10)

R1 IST dates + overnight (`shift_end<=shift_start` → punch-in `<shift_end` belongs to prior day; end is next day) + truncate to seconds. R2 late only if `>10:00` after start, then `floor` since start (`09:40:00→0`, `09:40:01→10`). R3 overtime `floor` since end only if `>=30`. R4 `(out-in)/3600` half-up 2-dec, `null` until out. R5 `half_day` when rounded `<4.50` (counts `0.5`). R6 `PRESENT/WFH/ON_DUTY` present. R7 Mon–Fri from `joined_on`; weekends still add late/overtime. R8 2-dec half-up, rates 4-dec. R9 headcount `joined_on<=period-end` incl. zero-log. R10 `page≥1`, `page_size≤100`, filtered `total`.

Epoch-ms `100000000000–4102444800000`; floats/strings/seconds → `422`. `ABSENT/LEAVE` have null times and are never created by punch-in.

## 4. Verify

```bash
python test_work.py      # ALL PASS: employees, punch-in/out, R2, overnight, half-day, regularize, list, monthly, summary, leaderboard, trend, concurrency (1×201+4×409), explain
python check_explain.py  # {"endpoint":...,"explain":{...}} IXSCAN True, COLLSCAN False
```

Status map: `422` validation (bad `emp_code`, `2026-02-30`, seconds-ms, `ABSENT`+times, no-change), `404` unknown employee/record/department or no open punch-in, `409` duplicate/race. `500` is always a bug — none remain (invalid dates, missing body, BSON `Timestamp`, null `punch_in` all guarded).

## 5. Design notes & submission

- See `REVIEW.md` — 28 defects with failing inputs and fixes — and `DECISIONS.md` (186 words: indexes, race, ties, headcount, 100x).
- Performance: list + analytics use covered indexes; trend densifies ≤92 days in DB, never loads 100k docs into Python.
- Do not commit `.env`, secrets, Dockerfiles, venvs, or dumps. Public repo must start clean via the Run block above.
