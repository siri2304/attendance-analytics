# REVIEW.md

## Defects Found in the Starter Code

The following defects were identified by reviewing the original `app/main.py` against the API contract and MongoDB data model.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes()` | The helper calculates minutes after shift start but does not clearly handle the IST timezone convention. | Use a timestamp close to the shift start and compare the result with the expected IST time. | Normalize the punch timestamp to IST before comparing it with the employee's shift start. Apply the specified 10-minute grace period. |
| 2 | `compute_work_hours()` | The helper does not explicitly define how fractional hours are rounded. | Test durations that produce more than two decimal places. | Calculate elapsed seconds and round the result to two decimal places using the contract's required rounding rule. |
| 3 | `compute_overtime()` | The helper constructs the shift-end instant without correctly accounting for overnight shifts and consistent UTC/IST handling. | Test a shift that starts before midnight and ends the following morning. | Calculate the shift end from the attendance date and employee shift times, advancing to the next day for overnight shifts. Apply the required overtime threshold. |
| 4 | `create_employee()` | The duplicate employee-code check is not sufficient to protect against simultaneous requests. | Send two concurrent requests with the same `emp_code`. | Create a unique index on `employees.emp_code` and handle duplicate-key errors. |
| 5 | `create_employee()` | The creation timestamp uses naive `datetime.now()` rather than a consistent UTC instant. | Inspect `created_at` in MongoDB and compare it with the API timestamp. | Store a UTC datetime and serialize it as epoch milliseconds in the API response. |
| 6 | `list_employees()` | Pagination uses `page * page_size` as the skip offset, which skips the first page's records. | Request page 1 with a page size of 20 and inspect the returned records. | Use `(page - 1) * page_size` and validate page parameters. |
| 7 | `list_employees()` | The total count ignores the department filter. | Request employees for a department and compare `total` with the matching records. | Apply the same filter to `count_documents()` and the result query. |
| 8 | `list_employees()` | The query has no explicit deterministic sort order. | Repeat paginated requests and inspect whether records have a stable order. | Sort by `emp_code` before applying pagination. |
| 9 | `punch_in()` | The employee lookup result is not checked before accessing shift fields. | Punch in using an unknown employee code. | Return 404 when the employee does not exist. |
| 10 | `punch_in()` | The timestamp conversion uses local system time instead of a defined UTC instant. | Send an epoch-millisecond timestamp and compare the stored date with the expected IST attendance date. | Convert supplied timestamps to UTC and derive attendance dates using IST and the employee's shift. |
| 11 | `punch_in()` | The duplicate attendance check is vulnerable to concurrent requests. | Send simultaneous punch-in requests for the same employee and attendance date. | Create a unique compound index on `(emp_code, date)` and handle duplicate-key errors as 409 responses. |
| 12 | `punch_in()` | The starter accepts a status without validating that it is a presence status. | Attempt to punch in with `ABSENT` or `LEAVE`. | Restrict punch-in status to `PRESENT`, `WFH`, and `ON_DUTY`. |
| 13 | `punch_in()` | The attendance date calculation does not correctly account for overnight shifts and the attendance-day definition. | Punch in or test seeded records around midnight for an overnight shift. | Derive the attendance date using IST and the employee's shift rules. |
| 14 | `list_attendance()` | The endpoint loads all matching records into Python before sorting, counting, and paginating. | Run the endpoint against a large attendance collection and observe memory use and response time. | Count matching documents in MongoDB and apply sorting, skip, and limit in the database query. |
| 15 | `list_attendance()` | The endpoint returns MongoDB datetime values directly instead of converting punch instants to epoch milliseconds. | Inspect the JSON response for `punch_in` and `punch_out`. | Serialize instants, including nested history timestamps, as epoch milliseconds. |
| 16 | `list_attendance()` | The endpoint does not validate date ranges or status values. | Supply `date_from` later than `date_to`, or an unsupported status. | Validate date formats, date ordering, and allowed status values; return 422 for invalid input. |
| 17 | `list_attendance()` | Pagination parameters are not validated, and the in-memory slice is inefficient for large datasets. | Send page 0, a negative page size, or request a late page in a large collection. | Validate pagination parameters and perform database-side pagination. |
| 18 | Module initialization | The starter does not create the indexes required for uniqueness and efficient queries. | Inspect the collection indexes or run query explain on a large dataset. | Create appropriate unique and compound indexes at application startup. |
| 19 | API contract | `POST /attendance/punch-out` is missing. | Request the punch-out endpoint. | Implement punch-out with validation, work-hour calculation, overtime calculation, half-day calculation, and concurrency protection. |
| 20 | API contract | `PATCH /attendance/{emp_code}/{date}` is missing. | Request a manual correction for an existing attendance record. | Implement regularization with validation, recalculation of derived fields, atomic updates, and an appended audit entry. |
| 21 | API contract | `GET /analytics/employees/{emp_code}/monthly` is missing. | Request a monthly summary for a known employee. | Implement the monthly summary using a MongoDB aggregation pipeline and the specified working-day and attendance rules. |
| 22 | API contract | `GET /analytics/departments/summary` is missing. | Request a department summary for a month. | Aggregate attendance metrics and employee headcount, including employees with no attendance logs who qualify for the month. |
| 23 | API contract | `GET /analytics/leaderboard/late` is missing. | Request the monthly late-comers leaderboard and test tied totals. | Aggregate late minutes and counts, join employee details, and use competition ranking with the required tie handling. |
| 24 | API contract | `GET /analytics/departments/{department}/trend` is missing. | Request a trend covering weekdays, weekends, and dates without attendance records. | Implement a MongoDB aggregation that fills the requested date range, calculates daily metrics, and computes the seven-row moving average. |
| 25 | API contract | `GET /admin/explain/{endpoint}` is missing. | Request an explain plan for a supported endpoint. | Implement endpoint-specific MongoDB explain operations using execution statistics and the corresponding filters and sort. |
| 26 | Manual correction requirements | The starter has no mechanism to preserve the before-and-after values of manual changes. | Correct an attendance record and inspect its history. | Append one history entry per actual correction, storing only changed fields and preserving existing history. |
| 27 | API serialization | MongoDB's internal `_id` must not be exposed as the employee identifier. The starter's attendance response instead exposes an `id` field derived from `_id`. | Inspect employee and attendance response bodies. | Use `emp_code` and the attendance natural key `(emp_code, date)`; exclude MongoDB `_id` from API responses. |

## Items Reviewed and Not Considered Defects

| # | Where | What was reviewed | Why it is not a defect |
|---|---|---|---|
| 1 | `/health` | Basic health response | Returning `{"status": "ok"}` is a valid minimal health response when the contract only requires an application health check. A database connectivity check may be added as an operational improvement. |
| 2 | `EmployeeIn` | Default shift times | Defaults of `09:30` and `18:30` are consistent with the starter's intended default shift. The problem is missing or insufficient validation, not the existence of defaults. |
| 3 | `attendance_logs` data model | `date` stored as a string | This is intentional. Attendance dates are calendar dates, not instants, so storing them as `YYYY-MM-DD` avoids imposing a timezone on the date field. |
| 4 | `attendance_logs` data model | `history` stored as an array | This is intentional and supports an append-only audit trail for manual corrections. The implementation must preserve previous entries. |
| 5 | Employee identity | Use of `emp_code` | This is intentional. The assignment specifies a customer-generated employee code as the business identifier rather than MongoDB's internal `_id`. |

## Review Notes

- The original implementation should be tested against the complete OpenAPI contract, including validation errors and concurrent requests.
- Analytics must use MongoDB aggregation pipelines and stored derived attendance values rather than loading the entire collection into Python or recomputing seeded metrics.
- Index creation alone does not prove query performance. The explain endpoint should be checked against the large dataset to confirm that supported queries use suitable indexes.
- The fixes described above should be verified with automated tests and the assignment's expected business rules before the solution is considered complete.
