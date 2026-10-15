# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
- `employees.emp_code` (unique): Business key uniqueness and lookups.
- `employees.(department, emp_code)`: Department filter and `emp_code` sort.
- `employees.joined_on`: R9 headcount in `department_summary`, preventing COLLSCAN.
- `attendance_logs.(emp_code, date)` (unique): Natural key, prevents duplicate punches, indexes monthly summaries.
- `attendance_logs.(date desc, emp_code asc)`: Paginated attendance sorting and date ranges.
- `attendance_logs.(emp_code, punch_in desc)`: Punch-out open-record lookup.
*Rejected*: `(date, late_minutes)` because `(date desc, emp_code asc)` already provided an IXSCAN.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
Both compute the attendance date and attempt an insert. The unique index on `(emp_code, date)` commits only the first (`201 Created`). The second raises `DuplicateKeyError`, intercepted to return `409 Conflict`.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
The pipeline uses `$setWindowFields` with `$rank` for competition ranking (1, 2, 2, 4). Filtering `rank <= limit` runs after ranking, tie-breaking by `emp_code` ascending. Tied employees at rank 10 with `limit=10` are both returned.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
The pipeline starts on `employees` matching `joined_on <= period_end` (per R9) and `$lookup`s `attendance_logs`. Employees without logs retain an empty array, counting toward headcount while returning `null` for `avg_work_hours`.

5. **One thing you would change** if this had to serve 100x the data.
Pre-aggregate daily and monthly metrics into a rollup collection via background workers rather than running on-demand aggregations over raw attendance logs.
