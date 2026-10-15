# Starter review

| # | Where | What was wrong | How to notice | Fix
|---|---|---|---|---|
| 1 | Analytics and Explain parameters/routes | Used non-contract leaderboard and Explain paths; monthly endpoints took numeric year/month instead of `month=YYYY-MM`. | Compare route and query parameters with OpenAPI or call the documented URLs. | Corrected routes and month parsing for analytics and Explain. |
| 2 | Explain `department_trend` | Only validated inputs and returned a placeholder; a nested route definition was unreachable and had inconsistent date types. | Explain request did not contain execution statistics; route structure inspection. | Explain now runs the same shared trend pipeline as the public endpoint and returns MongoDB executionStats. |
| 3 | Leaderboard route | Used `/analytics/late-leaderboard` instead of `/analytics/leaderboard/late`. | Request documented route returned 404. | Registered the path from OpenAPI. |
| 4 | Analytics response and aggregation rules | Summary response keys did not match OpenAPI; department present days included weekends, and leaderboard omitted `late_count`. | Compare a response with its schema or seed weekend attendance. | Responses now use the documented `month`/`items` shapes, weekdays govern summary present days, and leaderboard counts late records. |
| 5 | Department trend aggregation | Moving average used MongoDB `$round`, which uses ties-to-even, contrary to half-up reporting. | A moving average exactly at a 4th-decimal half increment. | Uses floor(value * 10000 + 0.5) / 10000 for nonnegative rates. |
| 6 | Department summary aggregation | Average work hours was not rounded to two decimals as required. | A department average with more than two decimal places. | Applies half-up rounding to two decimal places. |
| 7 | `sample_seed.py` | Deleted both collections before inserting, risking unrelated database data loss. | Run seeder against a database with existing documents. | Upserts only sample natural keys and preserves pre-existing documents and values. |
| 8 | Index definitions | Missing compound indexes for the documented attendance sort, department headcount filter, and late range scan. | Inspect index list or Explain plans at assignment scale. | Added supporting indexes while retaining unique natural-key indexes. |
| 10 | Employee list response | `created_at` was left as a BSON datetime, which FastAPI serialized as an ISO string rather than epoch milliseconds. | Inspect `GET /employees` with `created_at` in the response. | Convert stored instants to epoch milliseconds before returning the page. |
| 11 | Punch-out missing-record handling | A punch-out before the open record time returned 404, and an already-closed record could be reported as missing. | Send a `punched_at` before `punch_in`, or punch out twice sequentially. | Return 422 for before-punch times, 409 for already-closed records, and 404 when no applicable record exists. |
| 12 | Department trend response | Returned `from`, `to`, and `trend`, while the OpenAPI `Trend` schema requires `department` and `items`. | Validate a live response against `components/schemas/Trend`. | Return the documented `{department, items}` shape; add a response-shape assertion. |
| 13 | Late leaderboard Explain | Omitted the public aggregation's post-rank positive-total predicate. | Compare the Explain pipeline to `late_leaderboard`. | Add the predicate so Explain mirrors the public pipeline. |
| 14 | Explain Decimal128 response values | BSON `Decimal128` values in live query plans could not be serialized by FastAPI's default JSON encoder. | Call Explain for a branch whose plan contains Decimal128 values. | Convert Decimal128 recursively to Python `Decimal` before returning JSON. |
| 9 | README | Referenced a missing `.env.example` and described a Unix-only setup despite this project running in Windows. | Follow setup instructions from this checkout. | Rewrote setup for PowerShell and environment variables; documented non-destructive seeding. |

## Checked and retained

- API timestamps, including nested correction history, are serialized as epoch milliseconds.
- Punch-in concurrency is protected by the unique `(emp_code, date)` database index, even though the friendly duplicate check is a separate read.
- Punch-out uses a conditional update so simultaneous requests cannot both close the same record.
- Regularization uses a compare-and-update predicate and appends history atomically, returning 409 on a concurrent edit.
- Attendance listing uses MongoDB filtering, sorting, skip and limit; it does not load the full collection into Python.
- Analytics aggregate from MongoDB and use stored derived fields. Monthly working days are calculated from the employee join date.
- The real MongoDB Explain suite checks the five current query shapes for IXSCAN and no reported COLLSCAN on its small test dataset; assignment-scale performance remains unmeasured.

The assignment's `PROBLEM_STATEMENT.docx` was not present in this checkout; this review is based on `openapi.yaml`, `DATA_MODEL.md`, the starter code and available repository files.
