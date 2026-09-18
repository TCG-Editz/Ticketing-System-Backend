"""
FastAPI backend for ticket scanner app.

Endpoints:
 - GET  /api/attendees
 - POST /api/login
 - GET  /api/attendee/{attendee_id}
 - POST /api/attendee/{attendee_id}/mark
 - GET  /api/stats
 - GET  /api/stats/by-branch

MongoDB fields used:
 - Name
 - College Email ID
 - Ticket Status
 - Email Status
 - Attendee ID
 - Attendance
"""

import os
import json
import certifi

from dotenv import load_dotenv
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
from pymongo import MongoClient


# ============================================================
# LOAD ENVIRONMENT VARIABLES
# ============================================================

load_dotenv()


# ============================================================
# CONFIGURATION
# ============================================================

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME")
MONGO_COLLECTION_NAME = os.getenv("MONGO_COLLECTION_NAME")

SCANNER_ID = os.getenv("SCANNER_ID")
SCANNER_PASSWORD = os.getenv("SCANNER_PASSWORD")

GOOGLE_SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SHEETS_SPREADSHEET_ID = os.getenv("SHEETS_SPREADSHEET_ID")
SHEETS_TAB_NAME = os.getenv("SHEETS_TAB_NAME", "Sheet1")

UPDATE_SHEETS_ON_MARK = (
    os.getenv("UPDATE_SHEETS_ON_MARK", "false").lower()
    in ("true", "1", "yes")
)

CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")


# ============================================================
# REQUIRED CONFIGURATION CHECKS
# ============================================================

if not MONGO_URI:
    raise RuntimeError(
        "FATAL: MONGO_URI environment variable must be set."
    )

if not MONGO_DB_NAME:
    raise RuntimeError(
        "FATAL: MONGO_DB_NAME environment variable must be set."
    )

if not MONGO_COLLECTION_NAME:
    raise RuntimeError(
        "FATAL: MONGO_COLLECTION_NAME environment variable must be set."
    )

if not SCANNER_ID or not SCANNER_PASSWORD:
    raise RuntimeError(
        "FATAL: SCANNER_ID and SCANNER_PASSWORD environment variables must be set."
    )


# ============================================================
# MONGODB
# ============================================================

try:
    client = MongoClient(
        MONGO_URI,
        tls=True,
        tlsCAFile=certifi.where(),
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        socketTimeoutMS=10000,
        retryWrites=True,
    )

    db = client[MONGO_DB_NAME]
    collection = db[MONGO_COLLECTION_NAME]

    client.admin.command("ping")

    print("✅ MongoDB connection successful.")
    print(f"   Database: {MONGO_DB_NAME}")
    print(f"   Collection: {MONGO_COLLECTION_NAME}")

except Exception as e:
    raise RuntimeError(
        f"❌ Could not initialize MongoDB client: {e}"
    )


# ============================================================
# GOOGLE SHEETS
# ============================================================

def build_sheets_service():
    """Build and return Google Sheets service if configured."""

    if not GOOGLE_SA_JSON:
        print("ℹ️ Google Sheets integration is disabled.")
        return None

    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        cred_dict = json.loads(GOOGLE_SA_JSON)

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets"
        ]

        creds = Credentials.from_service_account_info(
            cred_dict,
            scopes=scopes
        )

        service = build(
            "sheets",
            "v4",
            credentials=creds
        )

        print("✅ Google Sheets service initialized.")

        return service

    except Exception as e:
        print(
            f"⚠️ Warning: Could not initialize Google Sheets service: {e}"
        )
        return None


sheets_service = build_sheets_service()


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Ticket Scanner Backend"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ============================================================
# PYDANTIC MODELS
# ============================================================

class LoginRequest(BaseModel):
    scanner_id: str
    scanner_password: str


class MarkRequest(BaseModel):
    scanner_id: str
    scanner_password: str
    meta: Optional[dict] = None


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def attendee_doc_to_dict(doc):
    """
    Converts a MongoDB document into a JSON-serializable dictionary.
    """

    if not doc:
        return None

    out = {
        k: v
        for k, v in doc.items()
        if k != "_id"
    }

    out["id"] = str(
        doc.get("Attendee ID")
        or doc.get("_id")
    )

    return out


def update_google_sheet_mark(
    attendee_id: str,
    mark_value: str = "Attended"
) -> bool:
    """
    Updates the Attendance column in Google Sheets
    for the matching Attendee ID.
    """

    if not sheets_service or not SHEETS_SPREADSHEET_ID:
        return False

    try:
        range_all = f"{SHEETS_TAB_NAME}!A:Z"

        result = (
            sheets_service
            .spreadsheets()
            .values()
            .get(
                spreadsheetId=SHEETS_SPREADSHEET_ID,
                range=range_all
            )
            .execute()
        )

        rows = result.get("values", [])

        if not rows:
            print("⚠️ Google Sheet contains no rows.")
            return False

        header = rows[0]
        data_rows = rows[1:]

        if "Attendee ID" not in header:
            print("❌ 'Attendee ID' column not found in Google Sheet.")
            return False

        if "Attendance" not in header:
            print("❌ 'Attendance' column not found in Google Sheet.")
            return False

        id_col_index = header.index("Attendee ID")
        attendance_col_index = header.index("Attendance")

        row_index = -1

        for idx, row in enumerate(data_rows):

            if (
                len(row) > id_col_index
                and str(row[id_col_index]).strip() == attendee_id.strip()
            ):
                row_index = idx + 2
                break

        if row_index == -1:
            print(
                f"⚠️ Attendee ID not found in Google Sheet: {attendee_id}"
            )
            return False

        # Supports columns beyond Z as well.
        def column_letter(index):
            result = ""

            while index >= 0:
                result = chr(index % 26 + ord("A")) + result
                index = index // 26 - 1

            return result

        attendance_col_letter = column_letter(
            attendance_col_index
        )

        range_to_write = (
            f"{SHEETS_TAB_NAME}!"
            f"{attendance_col_letter}"
            f"{row_index}"
        )

        (
            sheets_service
            .spreadsheets()
            .values()
            .update(
                spreadsheetId=SHEETS_SPREADSHEET_ID,
                range=range_to_write,
                valueInputOption="RAW",
                body={
                    "values": [[mark_value]]
                }
            )
            .execute()
        )

        print(
            f"✅ Google Sheet attendance updated: "
            f"{attendee_id} → {mark_value}"
        )

        return True

    except Exception as e:
        print(
            f"❌ An exception occurred during sheet update: {e}"
        )
        return False


# ============================================================
# LOGIN
# ============================================================

@app.post("/api/login")
def login(req: LoginRequest):
    """Validates scanner credentials."""

    if (
        req.scanner_id == SCANNER_ID
        and req.scanner_password == SCANNER_PASSWORD
    ):
        return {
            "ok": True,
            "message": "Login successful"
        }

    raise HTTPException(
        status_code=401,
        detail="Invalid scanner credentials"
    )


# ============================================================
# GET ALL ATTENDEES
# ============================================================

@app.get("/api/attendees")
def get_all_attendees():
    """
    Fetches all attendees from MongoDB.
    """

    docs = collection.find(
        {},
        {
            "Name": 1,
            "Attendee ID": 1,
            "Attendance": 1,
            "_id": 0
        }
    )

    return list(docs)


# ============================================================
# GET SINGLE ATTENDEE
# ============================================================

@app.get("/api/attendee/{attendee_id}")
def get_attendee(attendee_id: str):
    """
    Fetches a single attendee using Attendee ID.
    """

    projection = {
        "_id": 0,
        "Timestamp": 0,
        "Ticket Status": 0,
        "Email Status": 0
    }

    doc = collection.find_one(
        {
            "Attendee ID": attendee_id
        },
        projection
    )

    if not doc:
        raise HTTPException(
            status_code=404,
            detail="Attendee not found"
        )

    return doc


# ============================================================
# MARK ATTENDANCE
# ============================================================

@app.post("/api/attendee/{attendee_id}/mark")
def mark_attendance(
    attendee_id: str,
    req: MarkRequest
):
    """
    Marks an attendee as present.

    Flow:
    1. Validate scanner credentials
    2. Find attendee using Attendee ID
    3. Check whether already attended
    4. Optionally update Google Sheets
    5. Update MongoDB Attendance field
    """

    # --------------------------------------------------------
    # AUTHENTICATION
    # --------------------------------------------------------

    if (
        req.scanner_id != SCANNER_ID
        or req.scanner_password != SCANNER_PASSWORD
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid scanner credentials"
        )

    # --------------------------------------------------------
    # FIND ATTENDEE
    # --------------------------------------------------------

    doc = collection.find_one(
        {
            "Attendee ID": attendee_id
        }
    )

    if not doc:
        raise HTTPException(
            status_code=404,
            detail="Attendee not found"
        )

    # --------------------------------------------------------
    # CHECK ALREADY ATTENDED
    # --------------------------------------------------------

    if doc.get("Attendance") == "Attended":

        raise HTTPException(
            status_code=409,
            detail="Ticket already used."
        )

    # --------------------------------------------------------
    # GOOGLE SHEETS
    # --------------------------------------------------------

    sheet_updated = False

    if UPDATE_SHEETS_ON_MARK:

        sheet_updated = update_google_sheet_mark(
            attendee_id
        )

        if not sheet_updated:

            return {
                "ok": False,
                "message": (
                    "Failed to update Google Sheet. "
                    "Attendance not marked."
                ),
                "sheet_updated": False
            }

    # --------------------------------------------------------
    # MONGODB UPDATE
    # --------------------------------------------------------

    update_data: Dict[str, Any] = {
        "Attendance": "Attended"
    }

    result = collection.update_one(
        {
            "Attendee ID": attendee_id
        },
        {
            "$set": update_data
        }
    )

    if result.matched_count == 0:

        raise HTTPException(
            status_code=404,
            detail="Attendee not found while updating attendance."
        )

    print(
        f"✅ Attendance marked in MongoDB: "
        f"{attendee_id} → Attended"
    )

    return {
        "ok": True,
        "message": "Attendance marked successfully.",
        "sheet_updated": sheet_updated
    }


# ============================================================
# ATTENDANCE STATS
# ============================================================

@app.get("/api/stats")
def get_attendance_stats():
    """
    Calculates overall attendance statistics.
    """

    try:

        total_attendees = collection.count_documents({})

        attended_count = collection.count_documents(
            {
                "Attendance": "Attended"
            }
        )

        absent_count = (
            total_attendees - attended_count
        )

        return {
            "total_entries": total_attendees,
            "total_attended": attended_count,
            "total_absent": absent_count
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=(
                f"An error occurred while fetching stats: {e}"
            )
        )


# ============================================================
# BRANCH-WISE ATTENDANCE STATS
# ============================================================

@app.get("/api/stats/by-branch")
def get_branch_stats():
    """
    Calculates attendance statistics grouped by Branch.
    """

    try:

        pipeline = [

            {
                "$group": {

                    "_id": "$Branch",

                    "total_members": {
                        "$sum": 1
                    },

                    "total_attended": {

                        "$sum": {

                            "$cond": [
                                {
                                    "$eq": [
                                        "$Attendance",
                                        "Attended"
                                    ]
                                },
                                1,
                                0
                            ]

                        }

                    }

                }
            },

            {
                "$project": {

                    "branch": "$_id",

                    "total_members": "$total_members",

                    "total_attended": "$total_attended",

                    "total_absent": {
                        "$subtract": [
                            "$total_members",
                            "$total_attended"
                        ]
                    },

                    "_id": 0
                }
            },

            {
                "$sort": {
                    "branch": 1
                }
            }

        ]

        stats = list(
            collection.aggregate(pipeline)
        )

        for stat in stats:

            if not stat["branch"]:
                stat["branch"] = "Unknown"

        return stats

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=(
                f"An error occurred while fetching branch stats: {e}"
            )
        )
