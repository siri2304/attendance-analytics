# DECISIONS.md

## 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employee codes. A unique compound index on `attendance_logs.emp_code` and `date` guarantees one attendance record per employee per day. I also added indexes for date-based attendance queries, status filters, department lookups, and late-minute sorting. I considered adding an index to every individual field but rejected that approach because extra indexes increase storage and slow down writes.

## 2. Punch-in race

If two identical punch-in requests arrive together, both might initially find no attendance record. The unique compound index is the final safeguard: only one insert succeeds. The other raises a duplicate-key error, which the API converts into HTTP 409. This prevents duplicate attendance records.

## 3. Ties

The leaderboard uses competition ranking. Employees with equal late-minute totals receive the same rank, and the next rank is skipped. The limit is applied after ranking, so all employees tied at the cutoff rank are returned.

## 4. Headcount

The department summary starts from employees rather than attendance logs. It includes eligible employees even when they have no attendance records, then joins their logs to calculate attendance metrics.

## 5. Scaling further

For 100 times more data, I would benchmark aggregation pipelines, inspect execution plans, and consider partitioning or archiving older attendance records if query performance required it.
