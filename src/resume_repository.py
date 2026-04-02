import re
import base64
from datetime import datetime, timezone

import requests
import streamlit as st
from dotenv import load_dotenv
from streamlit.errors import StreamlitSecretNotFoundError

load_dotenv()

def _secret(name: str, *fallback_names: str, default: str = "") -> str:
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _supabase_headers() -> dict:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise Exception("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _clean_file_name(name: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*]+", "_", str(name or "").strip())
    return cleaned or "resume"


def _share_token(url: str) -> str:
    encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"u!{encoded}"


def _shared_folder_drive_item(access_token: str) -> dict:
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


def jr_folder_name(jr_number: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*]+", "_", str(jr_number or "").strip())
    return cleaned or "pending_jr"


def _candidate_name(row: dict) -> str:
    return " ".join(
        part for part in [str(row.get("First Name", "")).strip(), str(row.get("Last Name", "")).strip()] if part
    ).strip()


def _resume_db_payload(row: dict, user: dict, resume_link: str | None = None) -> dict:
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
        "client_recruiter": str(row.get("client_recruiter", "")).strip(),
        "client_recruiter_email": str(row.get("client_recruiter_email", "")).strip(),
        "client_email_sent": str(row.get("client_email_sent", "No")).strip(),
        # Uploader (session user) — fall back to user param so old records get filled on next sync
        "recruiter": str(row.get("recruiter", "") or user.get("name", "")).strip(),
        "recruiter_email": str(row.get("recruiter_email", "") or user.get("email", "")).strip(),
    }
    if resume_link is not None:
        payload["resume_link"] = resume_link
    return payload


def upload_resume_to_shared_drive(access_token: str, file_name: str, content: bytes, subfolder: str) -> str:
    if not ONEDRIVE_SHARED_FOLDER_LINK:
        raise Exception("Set ONEDRIVE_SHARED_FOLDER_LINK in src/resume_repository.py")

    safe_file_name = _clean_file_name(file_name)
    subfolder = str(subfolder or "").strip().strip("/")
    remote_path = f"{subfolder}/{safe_file_name}" if subfolder else safe_file_name
    drive_item = _shared_folder_drive_item(access_token)

    response = requests.put(
        f"https://graph.microsoft.com/v1.0/drives/{drive_item['drive_id']}/items/{drive_item['item_id']}:/"
        f"{remote_path}:/content",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/octet-stream",
        },
        data=content,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return data.get("webUrl", "")


def delete_resume_from_shared_drive(access_token: str, file_name: str, subfolder: str) -> None:
    if not ONEDRIVE_SHARED_FOLDER_LINK:
        raise Exception("Set ONEDRIVE_SHARED_FOLDER_LINK in src/resume_repository.py")

    safe_file_name = _clean_file_name(file_name)
    subfolder = str(subfolder or "").strip().strip("/")
    remote_path = f"{subfolder}/{safe_file_name}" if subfolder else safe_file_name
    drive_item = _shared_folder_drive_item(access_token)

    response = requests.delete(
        f"https://graph.microsoft.com/v1.0/drives/{drive_item['drive_id']}/items/{drive_item['item_id']}:/"
        f"{remote_path}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if response.status_code not in (204, 404):
        response.raise_for_status()


def fetch_active_jr_master() -> list[dict]:
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


def insert_resume_record(row: dict, user: dict, resume_link: str) -> dict:
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


def update_resume_record(record_id: str, row: dict, user: dict, resume_link: str | None = None) -> dict:
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
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?select=*",
        headers=_supabase_headers(),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def get_user_signature(email: str) -> str:
    """Retrieves the stored signature for a user."""
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


def save_user_signature(email: str, signature: str) -> None:
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
