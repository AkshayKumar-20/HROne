# DECISIONS.md

**1. Indexes.** employees: unique emp_code (identity + race-safe create), department and joined_on for analytics filtering. attendance_logs: unique (emp_code, date) — the natural key that makes concurrent punch-ins safe — plus (date desc, emp_code) for the list sort, and (emp_code, date, status)/(date, status) for analytics. A single date index was rejected: the list sorts on (date, emp_code), so the compound serves filter and sort together.

**2. Punch-in race.** Both requests truncate to seconds and compute the same IST date, then insert_one the same (emp_code, date). The unique index admits one; the loser gets DuplicateKeyError mapped to 409. No check-then-insert window exists.

**3. Ties.** Leaderboard uses $rank (competition ranking) on total_late_minutes desc. Limit applies as rank<=limit after ranking, so ties at the cutoff all return (more than limit rows possible), ordered by total desc, emp_code asc.

**4. Headcount.** Summary starts from employees with joined_on<=month-end and $lookups logs, so zero-log staff count, post-month joiners do not. avg_work_hours divides summed work_hours by record count, never averages averages.

**5. 100x data.** Shard or time-series on date, keep current indexes, pre-aggregate monthly summaries and cache leaderboard/trend, paginate with covered queries.
