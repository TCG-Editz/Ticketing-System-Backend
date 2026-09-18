"""
FastAPI backend for ticket scanner app.

Endpoints:
 - GET  /api/attendees
 - POST /api/login
 - GET  /api/attendee/{attendee_id}
 - POST /api/attendee/{attendee_id}/mark
 - GET  /api/stats
 - GET  /api/stats/by-branch

MongoDB fields:
 - Name
 - College Email ID
 - Ticket Status
 - Email Status
 - Attendee ID
 - Attendance

Config via environment variables.
"""

import os
import json

from dotenv import load_dotenv
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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
SHEETS_TAB_NAME = os.getenv(
    "SHEETS_TAB_NAME",
    "Form_Responses_1"
)

UPDATE_SHEETS_ON_MARK = (
    os.getenv(
        "UPDATE_SHEETS_ON_MARK",
        "false"
    ).lower()
    in ("true", "1", "yes")
)

CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "*"
).split(",")


# ============================================================
# CRITICAL CONFIGURATION CHECKS
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
        "FATAL: SCANNER_ID and SCANNER_PASSWORD "
        "environment variables must be set."
    )


# ============================================================
# MONGODB CONNECTION
# ============================================================

try:
    client = MongoClient(
        MONGO_URI
    )

    db = client[MONGO_DB_NAME]

    collection = db[MONGO_COLLECTION_NAME]

    client.admin.command("ping")

    print("✅ MongoDB connection successful.")
    print(f"   Database: {MONGO_DB_NAME}")
    print(f"   Collection: {MONGO_COLLECTION_NAME}")

except Exception as e:
    raise RuntimeError(
        f"❌ Could not connect to MongoDB: {e}"
    )


# ============================================================
# GOOGLE SHEETS SERVICE
# ============================================================

def build_sheets_service():
    """
    Builds and returns a Google Sheets service client
    if Google Sheets integration is configured.
    """

    if not GOOGLE_SA_JSON:
        print(
            "ℹ️ Google Sheets integration is disabled."
        )
        return None

    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        cred_dict = json.loads(
            GOOGLE_SA_JSON
        )

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

        print(
            "✅ Google Sheets service initialized."
        )

        return service

    except Exception as e:
        print(
            "⚠️ Warning: Could not initialize "
            f"Google Sheets service: {e}"
        )
        return None


sheets_service = build_sheets_service()


# ============================================================
# FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title="Ticket Scanner Backend"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=[
        "GET",
        "POST",
        "OPTIONS"
    ],
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
    Converts a MongoDB document into a JSON-serializable
    dictionary.
    """

    if not doc:
        return None

    out = {
        key: value
        for key, value in doc.items()
        if key != "_id"
    }

    out["id"] = str(
        doc.get("Attendee ID")
        or doc.get("_id")
    )

    return out


def column_number_to_letter(column_number: int) -> str:
    """
    Converts a zero-based column number to a Google Sheets
    column letter.

    Examples:
        0  -> A
        1  -> B
        25 -> Z
        26 -> AA
        27 -> AB
    """

    result = ""

    while column_number >= 0:
        result = (
            chr(
                column_number % 26
                + ord("A")
            )
            + result
        )

        column_number = (
            column_number // 26
        ) - 1

    return result


def update_google_sheet_mark(
    attendee_id: str,
    mark_value: str = "Attended"
) -> bool:
    """
    Updates the Attendance column in Google Sheets
    for the matching Attendee ID.
    """

    if (
        not sheets_service
        or not SHEETS_SPREADSHEET_ID
    ):
        print(
            "⚠️ Google Sheets service is not configured."
        )
        return False

    try:

        # ----------------------------------------------------
        # READ SHEET
        # ----------------------------------------------------

        range_all = (
            f"{SHEETS_TAB_NAME}!A:Z"
        )

        result = (
            sheets_service
            .spreadsheets()
            .values()
            .get(
                spreadsheetId=(
                    SHEETS_SPREADSHEET_ID
                ),
                range=range_all
            )
            .execute()
        )

        rows = result.get(
            "values",
            []
        )

        if not rows:
            print(
                "⚠️ Google Sheet contains no rows."
            )
            return False

        # ----------------------------------------------------
        # HEADER
        # ----------------------------------------------------

        header = rows[0]

        data_rows = rows[1:]

        if "Attendee ID" not in header:
            print(
                "❌ 'Attendee ID' column not found "
                "in Google Sheet."
            )
            return False

        if "Attendance" not in header:
            print(
                "❌ 'Attendance' column not found "
                "in Google Sheet."
            )
            return False

        id_col_index = header.index(
            "Attendee ID"
        )

        attendance_col_index = header.index(
            "Attendance"
        )

        # ----------------------------------------------------
        # FIND ATTENDEE ROW
        # ----------------------------------------------------

        row_index = -1

        for idx, row in enumerate(data_rows):

            if (
                len(row) > id_col_index
                and str(
                    row[id_col_index]
                ).strip()
                == str(
                    attendee_id
                ).strip()
            ):
                row_index = idx + 2
                break

        if row_index == -1:

            print(
                "⚠️ Attendee ID not found "
                f"in Google Sheet: {attendee_id}"
            )

            return False

        # ----------------------------------------------------
        # DETERMINE ATTENDANCE COLUMN
        # ----------------------------------------------------

        attendance_col_letter = (
            column_number_to_letter(
                attendance_col_index
            )
        )

        range_to_write = (
            f"{SHEETS_TAB_NAME}!"
            f"{attendance_col_letter}"
            f"{row_index}"
        )

        # ----------------------------------------------------
        # UPDATE ATTENDANCE
        # ----------------------------------------------------

        (
            sheets_service
            .spreadsheets()
            .values()
            .update(
                spreadsheetId=(
                    SHEETS_SPREADSHEET_ID
                ),
                range=range_to_write,
                valueInputOption="RAW",
                body={
                    "values": [
                        [mark_value]
                    ]
                }
            )
            .execute()
        )

        print(
            "✅ Google Sheet attendance updated:"
            f" {attendee_id} → {mark_value}"
        )

        return True

    except Exception as e:

        print(
            "❌ An exception occurred during "
            f"Google Sheet update: {e}"
        )

        return False


# ============================================================
# LOGIN ENDPOINT
# ============================================================

@app.post("/api/login")
def login(req: LoginRequest):
    """
    Validates scanner credentials.
    """

    if (
        req.scanner_id == SCANNER_ID
        and
        req.scanner_password == SCANNER_PASSWORD
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
    Fetches a list of all attendees from MongoDB.
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

@app.get(
    "/api/attendee/{attendee_id}"
)
def get_attendee(
    attendee_id: str
):
    """
    Fetches full details for a single attendee
    using the Attendee ID field.
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

@app.post(
    "/api/attendee/{attendee_id}/mark"
)
def mark_attendance(
    attendee_id: str,
    req: MarkRequest
):
    """
    Marks an attendee as present.

    Flow:

    1. Validate scanner credentials.
    2. Find attendee using Attendee ID.
    3. Check whether the ticket was already used.
    4. Optionally update Google Sheets.
    5. Update MongoDB Attendance field.
    """

    # --------------------------------------------------------
    # AUTHENTICATION
    # --------------------------------------------------------

    if (
        req.scanner_id != SCANNER_ID
        or
        req.scanner_password != SCANNER_PASSWORD
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
    # CHECK WHETHER ALREADY ATTENDED
    # --------------------------------------------------------

    if doc.get("Attendance") == "Attended":

        raise HTTPException(
            status_code=409,
            detail="Ticket already used."
        )

    # --------------------------------------------------------
    # GOOGLE SHEETS UPDATE
    # --------------------------------------------------------

    sheet_updated = False

    if UPDATE_SHEETS_ON_MARK:

        sheet_updated = (
            update_google_sheet_mark(
                attendee_id,
                "Attended"
            )
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

    # --------------------------------------------------------
    # VERIFY MONGODB UPDATE
    # --------------------------------------------------------

    if result.matched_count == 0:

        raise HTTPException(
            status_code=404,
            detail=(
                "Attendee not found while "
                "updating attendance."
            )
        )

    print(
        "✅ MongoDB attendance updated:"
        f" {attendee_id} → Attended"
    )

    return {
        "ok": True,
        "message": (
            "Attendance marked successfully."
        ),
        "sheet_updated": sheet_updated
    }


# ============================================================
# ATTENDANCE STATISTICS
# ============================================================

@app.get("/api/stats")
def get_attendance_stats():
    """
    Calculates overall attendance statistics.
    """

    try:

        total_attendees = (
            collection.count_documents({})
        )

        attended_count = (
            collection.count_documents(
                {
                    "Attendance": "Attended"
                }
            )
        )

        absent_count = (
            total_attendees
            - attended_count
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
                "An error occurred while "
                f"fetching stats: {e}"
            )
        )


# ============================================================
# BRANCH-WISE ATTENDANCE STATISTICS
# ============================================================

@app.get("/api/stats/by-branch")
def get_branch_stats():
    """
    Calculates attendance statistics grouped by Branch.
    """

    try:

        pipeline = [

            # ------------------------------------------------
            # GROUP BY BRANCH
            # ------------------------------------------------

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

            # ------------------------------------------------
            # FORMAT RESULT
            # ------------------------------------------------

            {
                "$project": {

                    "branch": "$_id",

                    "total_members": (
                        "$total_members"
                    ),

                    "total_attended": (
                        "$total_attended"
                    ),

                    "total_absent": {
                        "$subtract": [
                            "$total_members",
                            "$total_attended"
                        ]
                    },

                    "_id": 0
                }
            },

            # ------------------------------------------------
            # SORT BY BRANCH
            # ------------------------------------------------

            {
                "$sort": {
                    "branch": 1
                }
            }
        ]

        stats = list(
            collection.aggregate(
                pipeline
            )
        )

        # ----------------------------------------------------
        # HANDLE EMPTY BRANCH VALUES
        # ----------------------------------------------------

        for stat in stats:

            if not stat.get("branch"):

                stat["branch"] = "Unknown"

        return stats

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=(
                "An error occurred while "
                f"fetching branch stats: {e}"
            )
        )
