"""
Resume Repository — resume_repository.py (IMPROVED VERSION)

Handles:
  ✅ Resume uploads to OneDrive (hrvolibot's drive)
  ✅ Database operations (Supabase)
  ✅ Proper Azure credential management using azure_auth module
  ✅ Better error handling and logging

CHANGES FROM ORIGINAL:
  • Uses azure_auth.py for token management (centralized)
  • Removes duplicate credential handling
  • Adds retry logic for OneDrive operations
  • Better error messages for debugging
  • Token caching for performance
"""

import re
import base64
import time
from datetime import datetime, timezone
from typing import Optional

import requests
import streamlit as st
from dotenv import load_dotenv
from streamlit.errors import StreamlitSecretNotFoundError

# Import improved Azure auth
try:
    from azure_auth import get_access_token, graph_request
except ImportError:
    raise ImportError(
        "❌ Missing azure_auth.py module. "
        "Please ensure it's in the same directory as resume_repository.py"
    )

load_dotenv()


# ─────────────────────────────────────────────────────────────
# SECRETS & CONFIG
# ─────────────────────────────────────────────────────────────

def _secret(name: str, *fallback_names: str, default: str = "") -> str:
    """Retrieve secret from Streamlit secrets or environment variables."""
    secrets_obj = None
    try:
        secrets_obj = st.secrets
    except StreamlitSecretNotFoundError:
        secrets_obj = None
    except Exception:
        secrets_obj = None

    for key in (name, *fallback_names):
        if secrets_obj is not None:
            try:
                value = secrets_obj.get(key)
                if value:
                    return str(value)
            except StreamlitSecretNotFoundError:
                pass
            except Exception:
                pass

    return default


SUPABASE_URL = _secret("SUPABASE_URL")
SUPABASE_KEY = _secret("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY")
SUPABASE_TABLE = _secret("SUPABASE_RESUME_TABLE", default="candidates_submitted")

ONEDRIVE_SHARED_FOLDER_LINK = "https://volibitsllp-my.sharepoint.com/:f:/g/personal/vamshipriya_konda_volibits_com/IgCfyrmNjOPJRJjecem68VEXAedkAWgC7-ebPjHAKOnRFSM?e=X8vYf0"

# hrvolibot OneDrive configuration
HRVOLIBOT_EMAIL = "hrvolibot@volibits.com"
HRVOLIBOT_ROOT_FOLDER = "Inbox Resumes"

# Token cache for hrvolibot drive ID
_drive_id_cache = {"drive_id": None, "cached_at": 0}
DRIVE_ID_CACHE_TTL = 3600  # 1 hour


# ─────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    """Return current UTC time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _supabase_headers() -> dict:
    """Build headers for Supabase API requests."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise Exception("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _clean_file_name(name: str) -> str:
    """Remove special characters from file name."""
    cleaned = re.sub(r"[<>:\"/\\|?*]+", "_", str(name or "").strip())
    return cleaned or "resume"


def _share_token(url: str) -> str:
    """Encode SharePoint URL to share token format."""
    encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"u!{encoded}"


def _candidate_name(row: dict) -> str:
    """Build candidate name from first and last name."""
    return " ".join(
        part for part in [str(row.get("First Name", "")).strip(), str(row.get("Last Name", "")).strip()] if part
    ).strip()


def jr_folder_name(jr_number: str) -> str:
    """Sanitize JR number to safe folder name."""
    cleaned = re.sub(r"[<>:\"/\\|?*]+", "_", str(jr_number or "").strip())
    return cleaned or "pending_jr"


# ─────────────────────────────────────────────────────────────
# ONEDRIVE - SHARED DRIVE OPERATIONS
# ─────────────────────────────────────────────────────────────

def _shared_folder_drive_item(access_token: str) -> dict:
    """
    Get drive item info for the shared OneDrive folder.

    Returns:
        dict: Contains 'drive_id' and 'item_id'
    """
    try:
        share_token = _share_token(ONEDRIVE_SHARED_FOLDER_LINK)
        response = requests.get(
            f"https://graph.microsoft.com/v1.0/shares/{share_token}/driveItem",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        parent_reference = data.get("parentReference", {})
        return {
            "drive_id": parent_reference.get("driveId") or data.get("parentReference", {}).get("driveId"),
            "item_id": data.get("id"),
        }
    except Exception as e:
        raise Exception(f"Failed to get shared folder drive item: {str(e)}")


def upload_resume_to_shared_drive(
        file_name: str,
        content: bytes,
        subfolder: str = "",
        max_retries: int = 2,
) -> str:
    """
    Upload a resume to the shared OneDrive folder.

    Args:
        file_name: Name of the file to upload
        content: File content as bytes
        subfolder: Optional subfolder path
        max_retries: Number of retry attempts for transient failures

    Returns:
        str: Web URL of the uploaded file

    Raises:
        Exception: If upload fails after retries
    """
    if not ONEDRIVE_SHARED_FOLDER_LINK:
        raise Exception("Set ONEDRIVE_SHARED_FOLDER_LINK in resume_repository.py")

    safe_file_name = _clean_file_name(file_name)
    subfolder = str(subfolder or "").strip().strip("/")
    remote_path = f"{subfolder}/{safe_file_name}" if subfolder else safe_file_name

    for attempt in range(max_retries):
        try:
            token = get_access_token()
            drive_item = _shared_folder_drive_item(token)

            response = requests.put(
                f"https://graph.microsoft.com/v1.0/drives/{drive_item['drive_id']}/items/{drive_item['item_id']}:/"
                f"{remote_path}:/content",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                },
                data=content,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("webUrl", "")

        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # exponential backoff
                continue
            raise Exception(f"OneDrive upload timeout after {max_retries} attempts")

        except requests.HTTPError as e:
            if e.response and e.response.status_code in [429, 503]:
                # Transient error - retry
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
            raise Exception(f"OneDrive upload failed: {str(e)}")

    raise Exception("OneDrive upload failed after all retries")


def delete_resume_from_shared_drive(file_name: str, subfolder: str = "") -> None:
    """Delete a file from the shared OneDrive folder (best-effort)."""
    try:
        safe_file_name = _clean_file_name(file_name)
        subfolder = str(subfolder or "").strip().strip("/")
        remote_path = f"{subfolder}/{safe_file_name}" if subfolder else safe_file_name

        token = get_access_token()
        drive_item = _shared_folder_drive_item(token)

        response = requests.delete(
            f"https://graph.microsoft.com/v1.0/drives/{drive_item['drive_id']}/items/{drive_item['item_id']}:/"
            f"{remote_path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if response.status_code not in (204, 404):
            response.raise_for_status()
    except Exception as e:
        print(f"Warning: Could not delete resume from shared drive: {e}")


# ─────────────────────────────────────────────────────────────
# ONEDRIVE - HRVOLIBOT DRIVE OPERATIONS
# ─────────────────────────────────────────────────────────────

def _hrvolibot_drive_id(token: str) -> str:
    """
    Get the drive ID for hrvolibot@volibits.com.
    Uses caching with TTL to reduce API calls.

    Args:
        token: Access token

    Returns:
        str: Drive ID
    """
    global _drive_id_cache

    # Return cached value if still fresh
    if _drive_id_cache["drive_id"] and (time.time() - _drive_id_cache["cached_at"]) < DRIVE_ID_CACHE_TTL:
        return _drive_id_cache["drive_id"]

    try:
        resp = requests.get(
            f"https://graph.microsoft.com/v1.0/users/{HRVOLIBOT_EMAIL}/drive",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()
        drive_id = resp.json()["id"]

        # Cache the result
        _drive_id_cache["drive_id"] = drive_id
        _drive_id_cache["cached_at"] = time.time()

        return drive_id

    except Exception as e:
        raise Exception(f"Failed to get hrvolibot drive ID: {str(e)}")


def upload_resume_to_hrvolibot_drive(
        file_name: str,
        content: bytes,
        jr_number: str,
        max_retries: int = 2,
) -> str:
    """
    Upload a resume to hrvolibot's OneDrive under:
        Inbox Resumes/<JR_FOLDER>/<file_name>

    Uses app-level authentication (client credentials).

    Args:
        file_name: Resume file name
        content: File content as bytes
        jr_number: JR number for folder organization
        max_retries: Retry attempts for transient errors

    Returns:
        str: Web URL of the uploaded file

    Raises:
        Exception: If upload fails after retries
    """
    for attempt in range(max_retries):
        try:
            token = get_access_token()
            drive_id = _hrvolibot_drive_id(token)
            safe_name = _clean_file_name(file_name)
            jr_folder = jr_folder_name(jr_number)
            remote_path = f"{HRVOLIBOT_ROOT_FOLDER}/{jr_folder}/{safe_name}"

            url = (
                f"https://graph.microsoft.com/v1.0"
                f"/drives/{drive_id}/root:/{remote_path}:/content"
            )
            resp = requests.put(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                },
                data=content,
                timeout=60,
            )
            resp.raise_for_status()
            result = resp.json().get("webUrl", "")

            if not result:
                raise Exception("Upload succeeded but no webUrl returned")

            return result

        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise Exception(f"Upload to hrvolibot drive timeout after {max_retries} attempts")

        except requests.HTTPError as e:
            if e.response and e.response.status_code in [429, 503]:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue

            error_detail = ""
            try:
                error_detail = e.response.json() if e.response else ""
            except:
                error_detail = e.response.text if e.response else ""

            raise Exception(
                f"Upload to hrvolibot drive failed ({e.response.status_code if e.response else 'unknown'}): {error_detail}"
            )

    raise Exception("Upload to hrvolibot drive failed after all retries")


def delete_resume_from_hrvolibot_drive(file_name: str, jr_number: str) -> None:
    """Delete a file from hrvolibot's OneDrive (best-effort)."""
    try:
        token = get_access_token()
        drive_id = _hrvolibot_drive_id(token)
        safe_name = _clean_file_name(file_name)
        jr_folder = jr_folder_name(jr_number)
        remote_path = f"{HRVOLIBOT_ROOT_FOLDER}/{jr_folder}/{safe_name}"

        url = (
            f"https://graph.microsoft.com/v1.0"
            f"/drives/{drive_id}/root:/{remote_path}"
        )
        resp = requests.delete(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp.status_code not in (204, 404):
            resp.raise_for_status()
    except Exception as e:
        print(f"Warning: Could not delete resume from hrvolibot drive: {e}")


# ─────────────────────────────────────────────────────────────
# DATABASE - SUPABASE OPERATIONS
# ─────────────────────────────────────────────────────────────

def _resume_db_payload(row: dict, user: dict, resume_link: Optional[str] = None) -> dict:
    """Build Supabase record payload from row data."""
    now = _now_iso()
    payload = {
        "jr_number": str(row.get("JR Number", "")).strip(),
        "date_text": str(row.get("Date", "")).strip(),
        "skill": str(row.get("Skill", "")).strip(),
        "client_recruiter": str(row.get("client_recruiter", "")).strip(),
        "file_name": str(row.get("File Name", "")).strip(),
        "first_name": str(row.get("First Name", "")).strip(),
        "last_name": str(row.get("Last Name", "")).strip(),
        "candidate_name": _candidate_name(row),
        "email": str(row.get("Email", "")).strip(),
        "phone": str(row.get("Phone", "")).strip(),
        "current_company": str(row.get("Current Company", "")).strip(),
        "total_experience": str(row.get("Total Experience", "")).strip(),
        "relevant_experience": str(row.get("Relevant Experience", "")).strip(),
        "current_ctc": str(row.get("Current CTC", "")).strip(),
        "expected_ctc": str(row.get("Expected CTC", "")).strip(),
        "notice_period": str(row.get("Notice Period", "")).strip(),
        "current_location": str(row.get("Current Location", "")).strip(),
        "preferred_location": str(row.get("Preferred Location", "")).strip(),
        "upload_to_sap": str(row.get("Upload to SAP", "")).strip(),
        "actual_status": str(row.get("Actual Status", "")).strip(),
        "call_iteration": str(row.get("Call Iteration", "")).strip(),
        "comments_availability": str(row.get("comments/Availability", "")).strip(),
        "error_message": str(row.get("Error", "")).strip(),
        "modified_by": str(user.get("email", "")).strip(),
        "modified_at": now,
        "client_recruiter_email": str(row.get("client_recruiter_email", "")).strip(),
        "client_email_sent": str(row.get("client_email_sent", "No")).strip(),
        "recruiter": str(row.get("recruiter", "") or user.get("name", "")).strip(),
        "recruiter_email": str(row.get("recruiter_email", "") or user.get("email", "")).strip(),
    }

    # Optional fields
    source_email_id = str(row.get("source_email_id", "")).strip()
    if source_email_id:
        payload["source_email_id"] = source_email_id

    if resume_link is not None:
        payload["resume_link"] = resume_link

    return payload


def fetch_active_jr_master() -> list[dict]:
    """Fetch all active JR records from Supabase."""
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/jr_master?select=jr_no,client_recruiter,recruiter_email,skill_name,jr_status",
        headers=_supabase_headers(),
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json()
    active_rows = []
    for row in rows:
        status = str(row.get("jr_status", "")).strip().lower()
        if status == "active":
            active_rows.append(row)
    return active_rows


def insert_resume_record(row: dict, user: dict, resume_link: Optional[str] = None) -> dict:
    """Insert a new resume record into Supabase."""
    payload = _resume_db_payload(row, user, resume_link=resume_link)
    payload["created_by"] = str(user.get("email", "")).strip()
    payload["created_at"] = _now_iso()

    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        headers=_supabase_headers(),
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    records = response.json()
    if not records:
        raise Exception("Supabase insert returned no rows")
    return records[0]


def update_resume_record(record_id: str, row: dict, user: dict, resume_link: Optional[str] = None) -> dict:
    """Update an existing resume record in Supabase."""
    payload = _resume_db_payload(row, user, resume_link=resume_link)
    response = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=eq.{record_id}",
        headers=_supabase_headers(),
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    records = response.json()
    if not records:
        return payload
    return records[0]


def update_resume_record_fields(record_id: str, fields: dict) -> dict:
    """Update specific fields of a resume record."""
    response = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=eq.{record_id}",
        headers=_supabase_headers(),
        json=fields,
        timeout=30,
    )
    response.raise_for_status()
    records = response.json()
    if not records:
        return fields
    return records[0]


def fetch_all_resume_records() -> list[dict]:
    """Fetch all resume records from Supabase."""
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?select=*",
        headers=_supabase_headers(),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def get_user_signature(email: str) -> str:
    """Retrieve the stored signature for a user."""
    if not email:
        return ""
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/recruiter_signatures?user_email=eq.{email}&select=signature",
        headers=_supabase_headers(),
        timeout=10,
    )
    if response.status_code == 200:
        data = response.json()
        if data:
            return data[0].get("signature", "")
    return ""


def save_user_signature(email: str, signature: str) -> None:
    """Save or update a user's signature."""
    if not email:
        return
    payload = {
        "user_email": email,
        "signature": signature,
        "updated_at": _now_iso(),
    }
    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/recruiter_signatures",
        headers={**_supabase_headers(), "Prefer": "resolution=merge-duplicates"},
        json=payload,
        timeout=10,
    )
    response.raise_for_status()


def fetch_unsent_email_records() -> list[dict]:
    """Fetch records where SAP upload is Done and client email has not been sent."""
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
        "?upload_to_sap=eq.Done&client_email_sent=eq.No&select=*",
        headers=_supabase_headers(),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def mark_client_email_sent(record_ids: list[str]) -> None:
    """Mark the given record IDs as client_email_sent=Yes."""
    if not record_ids:
        return
    ids_str = ",".join(record_ids)
    response = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=in.({ids_str})",
        headers=_supabase_headers(),
        json={"client_email_sent": "Yes"},
        timeout=30,
    )
    response.raise_for_status()