# Employee Attendance & Analytics API

A FastAPI application for employee management, attendance punch-in and punch-out, manual attendance corrections, and attendance analytics using MongoDB.

## Requirements

- Python 3.10+
- MongoDB 6.0 or newer
- Dependencies listed in `requirements.txt`

## Setup

Install the dependencies:

```bash
pip install -r requirements.txt
```

Set the following environment variables before running the application:

- `MONGO_URI`: MongoDB connection string
- `MONGO_DB`: Database name (defaults to `attendance_db`)

For local MongoDB, you can use:

```text
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Do not commit credentials, `.env` files, or other secrets to the repository.

## Run

From the repository root:

```bash
uvicorn app.main:app --port 8000
```

The API documentation is available at `http://localhost:8000/docs` while the application is running.

## Main Endpoints

- `GET /health` — health check
- `POST /employees` and `GET /employees` — employee management
- `POST /attendance/punch-in` and `POST /attendance/punch-out` — attendance recording
- `GET /attendance` — attendance listing
- `PATCH /attendance/{emp_code}/{date}` — manual attendance correction
- `GET /analytics/employees/{emp_code}/monthly` — employee monthly summary
- `GET /analytics/departments/summary` — department summary
- `GET /analytics/leaderboard/late` — late-comers leaderboard
- `GET /analytics/departments/{department}/trend` — daily attendance trend
- `GET /admin/explain/{endpoint}` — MongoDB query explain output

## Testing

Configure a MongoDB instance, start the application, and test the endpoints using `/docs` or an HTTP client. Verify validation errors, duplicate and concurrent requests, overnight shifts, audit history, analytics calculations, and query execution plans.

The implementation should be validated against `openapi.yaml`, `DATA_MODEL.md`, and the assignment's business rules before submission.
