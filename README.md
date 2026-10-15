# HROne Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the HROne attendance assignment. The HTTP contract and business rules are in [openapi.yaml](openapi.yaml); storage shapes are in [DATA_MODEL.md](DATA_MODEL.md). All application code is in `app/main.py`.

## Requirements

- Python 3.11 or newer
- A reachable MongoDB instance (MongoDB 6.0 or newer; the assignment grader uses MongoDB 7)
- `MONGO_URI` and `MONGO_DB` set in the process environment

## Install and run

From a clean checkout, install the declared dependencies and set the MongoDB connection settings before launching the API:

```sh
python -m pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

In PowerShell, set the same variables with `$env:MONGO_URI = "mongodb://localhost:27017"` and `$env:MONGO_DB = "attendance_db"` before running the `uvicorn` command. Replace the example URI and database name with values for your MongoDB instance. The app also loads a local `.env` file when present; keep credentials out of source control.

On startup, the app creates its required MongoDB indexes idempotently, including unique indexes for employee codes and employee attendance dates. Sample data is optional; load it with `python sample_seed.py`. The seeder inserts only missing sample records and preserves existing documents.

Interactive API documentation is available at `http://127.0.0.1:8000/docs`; the OpenAPI document is at `http://127.0.0.1:8000/openapi.json`. The readiness endpoint is `/health`. Explain plans are available at `/admin/explain/{endpoint}` for the five endpoint names in the OpenAPI contract.

## Run tests

```sh
python -m pytest -q
```

Most API tests replace `MongoClient` before importing the application and use an in-memory MongoDB test double. `tests/test_mongodb_integration.py` additionally starts a local `mongod` on loopback with a new temporary data directory and unique database per test. It never reads the configured `MONGO_URI` or `MONGO_DB`, and drops only its own test databases. Set `MONGOD_PATH` if `mongod` is not available on `PATH` or the default Windows MongoDB location.

## Assignment files

- `openapi.yaml` - endpoints, response schemas and rules R1-R10
- `DATA_MODEL.md` - MongoDB document shapes and sample records
- `sample_data/` and `sample_seed.py` - Extended JSON examples and non-destructive sample loading
- `REVIEW.md` - starter defects found and their fixes
- `DECISIONS.md` - index, concurrency, tie, headcount and scaling decisions
- `REQUIREMENTS_AUDIT.md` - requirement checklist, verification evidence and remaining gaps

No database reset is performed by the API or sample seeder.

## Known limitations

- Performance timings in the audit are local smoke measurements, not a production benchmark or latency guarantee. At 100,000 attendance records, only the attendance-list Explain plan was checked at that scale; the other four Explain branches were checked on smaller isolated datasets.
- Integration tests require a local `mongod` binary and have been verified with MongoDB 8.0.12 in this workspace. Set `MONGOD_PATH` when the binary is not on `PATH` or at the default Windows location. MongoDB 7 compatibility was not independently verified here.
