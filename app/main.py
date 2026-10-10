                {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
            ]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
    ]
    agg = list(db.attendance_logs.aggregate(pipeline))
    result = agg[0] if agg else {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    present_days = result.get("present_days", 0)
    pct = round_half_up(present_days / working_days * 100, 2) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": round_half_up(present_days),
            "leave_days": result.get("leave_days", 0), "late_count": result.get("late_count", 0),
            "total_late_minutes": result.get("total_late_minutes", 0), "total_overtime_minutes": result.get("total_overtime_minutes", 0), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    first, last = month_bounds(month)
    match_emp = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None: match_emp["department"] = department
    pipeline = [
        {"$match": match_emp},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joined": "$joined_on"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first.isoformat()]}, {"$lte": ["$date", last.isoformat()]}, {"$gte": ["$date", "$$joined"]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", first.isoformat()]}}}}, [2,3,4,5,6]]}]}, {"$cond": ["$logs.half_day", 0.5, 1]}, 0]}},
            "work_hours_values": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount_codes"}, "present_days": 1,
            "avg_work_hours": {"$avg": "$work_hours_values"}, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]
    items = list(db.employees.aggregate(pipeline))
    for item in items:
        item["present_days"] = round_half_up(item.get("present_days", 0))
        item["avg_work_hours"] = round_half_up(item.get("avg_work_hours"))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$set": {"rank": {"$rank": {"sortBy": {"total_late_minutes": -1}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_day: date = Query(..., alias="from"), to_day: date = Query(..., alias="to")):
    if to_day < from_day: raise HTTPException(422, "to must not be before from")
    if (to_day - from_day).days + 1 > 92: raise HTTPException(422, "Date range must not exceed 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "Unknown department")
    # Aggregate daily log metrics in MongoDB, then densify to include missing calendar days.
    pipeline = [
        {"$match": {"date": {"$gte": from_day.isoformat(), "$lte": to_day.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}, {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date", "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "date": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(from_day, datetime.min.time()), datetime.combine(to_day + timedelta(days=1), datetime.min.time())]}}},
        {"$set": {"date_string": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}}, "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date_string"}, "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}}, {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "is_working_day": {"$in": [{"$dayOfWeek": "$date"}, [2,3,4,5,6]]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$date_string", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}},
        {"$sort": {"date": 1}},
    ]
    return {"department": department, "items": list(db.attendance_logs.aggregate(pipeline))}


def explain_query(endpoint, params):
    if endpoint == "attendance_list":
        q = {}
        if params.get("emp_code"): q["emp_code"] = params["emp_code"]
        if params.get("status"): q["status"] = params["status"]
        if params.get("date_from") or params.get("date_to"):
            q["date"] = {}
            if params.get("date_from"): q["date"]["$gte"] = params["date_from"]
            if params.get("date_to"): q["date"]["$lte"] = params["date_to"]
        page, size = params.get("page", 1), params.get("page_size", 20)
        exp = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*size, "limit": size}, verbosity="executionStats")
        return "attendance_logs", exp
    month = params.get("month")
    if not month: raise HTTPException(422, "month is required")
    first, last = month_bounds(month)
    if endpoint == "employee_monthly":
        code = params.get("emp_code")
        if not code: raise HTTPException(422, "emp_code is required")
        pipeline = [{"$match": {"emp_code": code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}, {"$group": {"_id": "$emp_code", "n": {"$sum": 1}}}]
        return "attendance_logs", db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    if endpoint == "late_leaderboard":
        pipeline = [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}}]
        return "attendance_logs", db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    if endpoint == "department_summary":
        pipeline = [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}, {"$group": {"_id": "$emp_code", "n": {"$sum": 1}}}]
        return "attendance_logs", db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    if endpoint == "department_trend":
        department, start, end = params.get("department"), params.get("from"), params.get("to")
        if not department or not start or not end: raise HTTPException(422, "department, from, and to are required")
        pipeline = [{"$match": {"date": {"$gte": start, "$lte": end}}}, {"$group": {"_id": "$date", "n": {"$sum": 1}}}]
        return "attendance_logs", db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    raise HTTPException(422, "Unsupported endpoint")


@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                  department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                  date_from: Optional[date] = None, date_to: Optional[date] = None, status: Optional[str] = None,
                  from_day: Optional[date] = Query(None, alias="from"), to_day: Optional[date] = Query(None, alias="to"),
                  page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    params = {"emp_code": emp_code, "month": month, "department": department, "limit": limit,
              "date_from": date_from.isoformat() if date_from else None, "date_to": date_to.isoformat() if date_to else None,
              "status": status, "from": from_day.isoformat() if from_day else None, "to": to_day.isoformat() if to_day else None,
              "page": page, "page_size": page_size}
    collection, explain = explain_query(endpoint, params)
    return {"endpoint": endpoint, "collection": collection, "explain": explain}

