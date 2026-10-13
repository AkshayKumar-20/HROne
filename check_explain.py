"""
Print MongoDB explain scan-stage indicators for the five explain endpoints.

Run the API first, then:
    python check_explain.py

For meaningful index-vs-collection-scan conclusions, run against a representative
large dataset (the assignment README mentions 100k documents). A tiny sample
database may legitimately choose COLLSCAN because it is cheaper.
"""
import json
import os
import urllib.error
import urllib.request

BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
EMP_CODE = os.getenv("EXPLAIN_EMP_CODE", "EMP0001")
MONTH = os.getenv("EXPLAIN_MONTH", "2026-10")
DEPARTMENT = os.getenv("EXPLAIN_DEPARTMENT", "Engineering")

CASES = [
    ("attendance_list", f"/admin/explain/attendance_list?emp_code={EMP_CODE}&page=1&page_size=20"),
    ("employee_monthly", f"/admin/explain/employee_monthly?emp_code={EMP_CODE}&month={MONTH}"),
    ("department_summary", f"/admin/explain/department_summary?month={MONTH}&department={DEPARTMENT}"),
    ("late_leaderboard", f"/admin/explain/late_leaderboard?month={MONTH}&department={DEPARTMENT}&limit=10"),
    ("department_trend", f"/admin/explain/department_trend?department={DEPARTMENT}&from=2026-10-01&to=2026-10-07"),
]


def get_json(path):
    req = urllib.request.Request(BASE_URL + path, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def find_stage_names(value, found):
    """Recursively collect stage names from nested explain JSON."""
    if isinstance(value, dict):
        stage = value.get("stage")
        if isinstance(stage, str):
            found.add(stage.upper())
        for child in value.values():
            find_stage_names(child, found)
    elif isinstance(value, list):
        for child in value:
            find_stage_names(child, found)


def main():
    failures = 0
    for name, path in CASES:
        status, payload = get_json(path)
        if status != 200:
            failures += 1
            print(f"{name}: HTTP {status} — {payload}")
            continue

        explain = payload.get("explain", payload)
        stages = set()
        find_stage_names(explain, stages)
        has_ixscan = "IXSCAN" in stages
        has_collscan = "COLLSCAN" in stages
        print(json.dumps({
            "endpoint": name,
            "status": status,
            "IXSCAN": has_ixscan,
            "COLLSCAN": has_collscan,
            "stages_found": sorted(stages),
        }))

    if failures:
        raise SystemExit(f"{failures} explain endpoint(s) failed.")
    print("\nExplain requests completed.")
    print("Review IXSCAN/COLLSCAN against a representative large dataset; do not infer performance from a tiny sample.")


if __name__ == "__main__":
    main()
