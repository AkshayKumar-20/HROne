
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
IST = timezone(timedelta(hours=5, minutes=30))
RUN_ID = str(int(time.time()))[-6:]
EMP = f"EMP{RUN_ID}"
EMP_RACE = f"EMP{(int(RUN_ID) + 1) % 1_000_000:06d}"
DEPT = f"TestDept{RUN_ID}"


def epoch_ms(local_dt):
    """Convert an IST datetime to integer epoch milliseconds."""
    return int(local_dt.astimezone(timezone.utc).timestamp() * 1000)


def request(method, path, payload=None, expected=None):
    url = BASE_URL + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            status = response.status
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8")
    except Exception as exc:
        raise AssertionError(
            f"Could not call {url}. Is the API running? Details: {exc}"
        ) from exc

    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = raw

    if expected is not None and status not in expected:
        raise AssertionError(
            f"{method} {path}: expected HTTP {expected}, got {status}: {body}"
        )
    return status, body


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAIL: {name}. {detail}")
    print(f"PASS: {name}")


def create_employee(code, name):
    status, body = request("POST", "/employees", {
        "emp_code": code,
        "name": name,
        "email": f"{code.lower()}@example.com",
        "department": DEPT,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }, expected={201})
    check(f"create employee {code}", body.get("emp_code") == code)
    return body


def main():
    # Connectivity / health
    status, body = request("GET", "/health", expected={200})
    check("health endpoint", body.get("status") == "ok")

    # Create test employees in a unique department
    create_employee(EMP, "API Test Employee")
    create_employee(EMP_RACE, "API Race Employee")

    # Duplicate employee should be rejected
    status, _ = request("POST", "/employees", {
        "emp_code": EMP,
        "name": "Duplicate",
        "email": f"duplicate-{RUN_ID}@example.com",
        "department": DEPT,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })
    check("duplicate employee returns 409", status == 409, f"got {status}")

    # Punch in at 09:40:01 IST on 12 October 2026
    punch_in_ms = epoch_ms(datetime(2026, 10, 12, 9, 40, 1, tzinfo=IST))
    status, attendance = request("POST", "/attendance/punch-in", {
        "emp_code": EMP,
        "punched_at": punch_in_ms,
        "status": "PRESENT",
    }, expected={201})
    check("punch-in creates attendance", attendance.get("emp_code") == EMP)
    check("punch-in date is correct", attendance.get("date") == "2026-10-12",
          f"got {attendance.get('date')}")
    check("punch-in time is serialized as epoch milliseconds",
          isinstance(attendance.get("punch_in"), int))

    # Duplicate punch-in for same employee/date should return 409
    status, _ = request("POST", "/attendance/punch-in", {
        "emp_code": EMP,
        "punched_at": punch_in_ms,
        "status": "PRESENT",
    })
    check("duplicate punch-in returns 409", status == 409, f"got {status}")

    # Punch out after the shift
    punch_out_ms = epoch_ms(datetime(2026, 10, 12, 18, 45, 0, tzinfo=IST))
    status, closed = request("POST", "/attendance/punch-out", {
        "emp_code": EMP,
        "punched_at": punch_out_ms,
    }, expected={200})
    check("punch-out closes attendance", closed.get("punch_out") == punch_out_ms)
    check("work hours are calculated", isinstance(closed.get("work_hours"), (int, float)))
    check("completed attendance has history field", isinstance(closed.get("history"), list))

    # Attendance listing and pagination
    status, listing = request(
        "GET", f"/attendance?emp_code={EMP}&page=1&page_size=10", expected={200}
    )
    check("attendance list contains created record",
          listing.get("total", 0) >= 1 and len(listing.get("items", [])) >= 1)

    # Monthly analytics
    status, monthly = request(
        "GET", f"/analytics/employees/{EMP}/monthly?month=2026-10", expected={200}
    )
    check("employee monthly analytics responds with items",
          isinstance(monthly.get("items"), list))

    # Department summary / leaderboard
    status, summary = request(
        "GET", f"/analytics/departments/summary?month=2026-10&department={DEPT}",
        expected={200}
    )
    check("department summary responds with items", isinstance(summary.get("items"), list))

    status, leaderboard = request(
        "GET", f"/analytics/leaderboard/late?month=2026-10&limit=10&department={DEPT}",
        expected={200}
    )
    check("late leaderboard responds with items", isinstance(leaderboard.get("items"), list))

    # Trend range has an attendance record on its first day; verify date densification.
    status, trend = request(
        "GET",
        f"/analytics/departments/{DEPT}/trend?from=2026-10-12&to=2026-10-14",
        expected={200},
    )
    dates = [row.get("date") for row in trend.get("items", [])]
    check("department trend returns three dates",
          dates == ["2026-10-12", "2026-10-13", "2026-10-14"],
          f"got {dates}")

    # Concurrent punch-ins: exactly one request should win and one should conflict.
    race_ms = epoch_ms(datetime(2026, 10, 13, 9, 30, 0, tzinfo=IST))
    def race_call():
        return request("POST", "/attendance/punch-in", {
            "emp_code": EMP_RACE,
            "punched_at": race_ms,
            "status": "PRESENT",
        })[0]

    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(lambda _: race_call(), range(5)))
    check("concurrent punch-in has one 201 and four 409",
          statuses.count(201) == 1 and statuses.count(409) == 4,
          f"got statuses {statuses}")

    # Validation: epoch seconds are not accepted as epoch milliseconds.
    status, _ = request("POST", "/attendance/punch-in", {
        "emp_code": EMP,
        "punched_at": 1_791_800_000,
        "status": "PRESENT",
    })
    check("seconds-like timestamp is rejected", status == 422, f"got {status}")

    print("\nALL INCLUDED SMOKE TESTS PASSED.")
    print("Note: this script does not replace the assignment's full hidden-test coverage.")


if __name__ == "__main__":
    main()
