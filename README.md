# Employee Attendance & Analytics API

FastAPI service for employee attendance tracking, attendance regularization, and MongoDB analytics aggregations.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (local instance or MongoDB Atlas)

## Environment Configuration
The service reads environment variables with fallback to `.env`:
- `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
- `MONGO_DB`: MongoDB database name (default: `attendance_db`)

System environment variables take priority over `.env` values.

## Local Setup & Execution

1. **Create and activate a virtual environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Environment configuration** (optional, defaults apply automatically):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Start the FastAPI application**:
   ```bash
   uvicorn app.main:app --port 8000
   ```

All required MongoDB indexes are created idempotently at application startup via the FastAPI lifespan handler.

## API Documentation
Interactive OpenAPI documentation is available once the server is running:
- Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
- ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)

## Implementation Overview
- **Single-File Structure**: All models, business rule helpers (R1–R10), core endpoints, analytics pipelines, and explain endpoints are housed inside `app/main.py`.
- **Concurrency & Natural Keys**: Concurrency safety uses atomic MongoDB operations (`find_one_and_update`, `$setOnInsert`) and unique compound constraints (`employees.emp_code`, `attendance_logs.(emp_code, date)`).
- **MongoDB Aggregation Pipelines**: Analytics endpoints run fully inside MongoDB using aggregation stages including `$lookup`, `$densify`, `$setWindowFields` (`$rank`), and sliding document window moving averages.
- **Explain & Performance**: `GET /admin/explain/{endpoint}` executes real queries and aggregations with `executionStats` verbosity, verifying index usage (`IXSCAN`) and absence of collection scans.
