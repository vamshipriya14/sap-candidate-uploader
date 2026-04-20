import re
import hashlib
from datetime import datetime, timezone, timedelta  # 🔴 Fix #2: added timedelta

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# 🔐 SECRETS
# ─────────────────────────────────────────────
def _secret(name: str, *fallback_names: str, default: str = "") -> str:
    try:
        secrets_obj = st.secrets
    except Exception:
        secrets_obj = None

    for key in (name, *fallback_names):
        if secrets_obj:
            try:
                v = secrets_obj.get(key)
                if v:
                    return str(v)
            except Exception:
                pass

    return default


SUPABASE_URL = _secret("SUPABASE_URL")
SUPABASE_KEY = _secret("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY")
SUPABASE_TABLE = _secret("SUPABASE_RESUME_TABLE", default="candidates_submitted")

BUCKET = "resumes"

# ─────────────────────────────────────────────
# 🧠 HELPERS
# ─────────────────────────────────────────────
def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# 🔴 Fix #4: Single consolidated header function used everywhere
def _headers(json: bool = True) -> dict:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise Exception("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json" if json else "application/octet-stream",
        "Prefer": "return=representation",
    }


def _clean_file_name(name: str) -> str:
    import re

    name = str(name or "").strip()

    # Replace spaces
    name = name.replace(" ", "_")

    # Remove ALL unsafe characters (including [] ())
    name = re.sub(r"[^a-zA-Z0-9._-]", "", name)

    if not name:
        name = "file"

    return name

def jr_folder_name(jr_number: str) -> str:
    return re.sub(r"[<>:\"/\\|?*]+", "_", str(jr_number or "").strip()) or "pending_jr"


def _candidate_name(row: dict) -> str:
    return " ".join(
        part for part in [row.get("First Name", ""), row.get("Last Name", "")]
        if part
    ).strip()


# ─────────────────────────────────────────────
# 🏗️ DB PAYLOAD BUILDER
# ─────────────────────────────────────────────
# 🔴 Fix #1: Extracted _resume_db_payload so update_resume_record can call it
def _resume_db_payload(row: dict, user: dict, resume_path: str | None = None) -> dict:
    # Support both display-key ("Upload to SAP") used by the main app's row_dict
    # and the DB-column key ("upload_to_sap") used by the email inbox's row_data.
    upload_to_sap_val = (
        str(row.get("upload_to_sap", "") or row.get("Upload to SAP", "")).strip() or None
    )
    payload = {
        "jr_number": str(row.get("JR Number", "")).strip(),
        "date_text": str(row.get("Date", "")).strip(),
        "skill": str(row.get("Skill", "")).strip(),
        "file_name": str(row.get("File Name", "")).strip(),
        "resume_path": resume_path,
        "first_name": str(row.get("First Name", "")).strip(),
        "last_name": str(row.get("Last Name", "")).strip(),
        "candidate_name": _candidate_name(row),
        "email": str(row.get("Email", "")).strip(),
        "phone": str(row.get("Phone", "")).strip(),
        "created_at": _now_iso(),
        "recruiter": user.get("name", ""),
        "recruiter_email": user.get("email", ""),
        # ── Fields populated by the Email Inbox page ──────────────────────
        "upload_to_sap": upload_to_sap_val,
        "client_recruiter": str(row.get("client_recruiter", "")).strip() or None,
        "client_recruiter_email": str(row.get("client_recruiter_email", "")).strip() or None,
        "source_email_id": str(row.get("source_email_id", "")).strip() or None,
    }
    # remove empty / None values
    return {k: v for k, v in payload.items() if v not in ("", None)}


# ─────────────────────────────────────────────
# 📤 UPLOAD TO SUPABASE STORAGE
# ─────────────────────────────────────────────
# 🟡 Fix #5: Keep original filename; use hash only as a dedup check prefix
def upload_resume(file_name: str, content: bytes, jr_number: str) -> str:
    file_hash = hashlib.md5(content).hexdigest()[:8]
    safe_original = _clean_file_name(file_name)
    # e.g. "abc12345_John_Doe_Resume.pdf" — unique but still human-readable
    storage_name = f"{file_hash}_{safe_original}"

    folder = jr_folder_name(jr_number)
    file_path = f"{folder}/{storage_name}"

    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{file_path}"

    resp = requests.post(
        url,
        headers=_headers(json=False),
        data=content,
        timeout=60,
    )

    if resp.status_code not in (200, 201, 409):
        raise Exception(resp.text)

    return file_path


# ─────────────────────────────────────────────
# 🔗 SIGNED URL (on demand)
# ─────────────────────────────────────────────
def get_resume_url(file_path: str) -> str:
    url = f"{SUPABASE_URL}/storage/v1/object/sign/{BUCKET}/{file_path}"

    resp = requests.post(
        url,
        headers=_headers(),
        json={"expiresIn": 3600},
        timeout=20,
    )

    if resp.status_code != 200:
        return ""

    return resp.json().get("signedURL", "")


# ─────────────────────────────────────────────
# 🗑️ DELETE FILE
# ─────────────────────────────────────────────
def delete_resume(file_path: str):
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{file_path}"
    requests.delete(url, headers=_headers(), timeout=20)


# ─────────────────────────────────────────────
# 🧹 CLEANUP (30 DAYS)
# ─────────────────────────────────────────────
def cleanup_old_resumes(days: int = 30):
    list_url = f"{SUPABASE_URL}/storage/v1/object/list/{BUCKET}"

    resp = requests.post(
        list_url,
        headers=_headers(),
        json={"limit": 1000},
        timeout=30,
    )

    if resp.status_code != 200:
        return

    files = resp.json()
    now = datetime.now(timezone.utc)

    for f in files:
        created_at = f.get("created_at")
        name = f.get("name")

        if not created_at or not name:
            continue

        created_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        if now - created_time > timedelta(days=days):  # 🔴 Fix #2: timedelta now imported
            delete_resume(name)


# ─────────────────────────────────────────────
# 💾 DB INSERT
# ─────────────────────────────────────────────
def insert_resume_record(row: dict, user: dict, resume_path: str | None = None) -> dict:
    payload = _resume_db_payload(row, user, resume_path=resume_path)

    def normalize_email(email):
        return email.strip().lower()

    def normalize_phone(phone):
        return re.sub(r"\D", "", phone)[-10:]  # last 10 digits
    payload["email"] = normalize_email(payload["email"])
    payload["phone"] = normalize_phone(payload["phone"])

    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        headers={
            **_headers(),
            "Prefer": "resolution=merge-duplicates"
        },
        json=payload,
        timeout=30,
    )

    if resp.status_code not in (200, 201):
        raise Exception(resp.text)

    # 🔥 Handle empty response safely
    try:
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return {}  # duplicate case (no body)
    except Exception:
        return {}  # empty response → treat as success


def fetch_existing_record_id(jr, email, phone):
    url = (
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
        f"?jr_number=eq.{jr}&email=eq.{email}&phone=eq.{phone}&select=id&limit=1"
    )
    resp = requests.get(url, headers=_headers(), timeout=15)

    if resp.status_code == 200:
        data = resp.json()
        if data:
            return str(data[0].get("id", "")).strip()

    return ""

def fetch_existing_record(jr, email, phone):
    url = (
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
        f"?jr_number=eq.{jr}&email=eq.{email}&phone=eq.{phone}"
        f"&select=id,upload_to_sap,resume_path,client_recruiter,client_recruiter_email&limit=1"
    )

    resp = requests.get(url, headers=_headers(), timeout=15)

    if resp.status_code == 200:
        data = resp.json()
        if data:
            return data[0]

    return {}


def fetch_record_by_file_name(jr: str, file_name: str) -> dict:
    """
    Fallback lookup by jr_number + file_name.
    Tries both the raw name and the cleaned name (as stored by upload_resume).
    """
    def _clean(name: str) -> str:
        name = str(name or "").strip().replace(" ", "_")
        return re.sub(r"[^a-zA-Z0-9._-]", "", name)

    for name_variant in dict.fromkeys([file_name, _clean(file_name)]):  # dedupe, preserve order
        url = (
            f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
            f"?jr_number=eq.{jr}&file_name=eq.{requests.utils.quote(name_variant)}"
            f"&select=id,upload_to_sap,resume_path,client_recruiter,client_recruiter_email&limit=1"
        )
        resp = requests.get(url, headers=_headers(), timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                return data[0]
    return {}


def fetch_record_by_candidate_name(jr: str, candidate_name: str) -> dict:
    """
    Last-resort fallback lookup by jr_number + candidate_name.
    Used when email/phone parse failed and file_name matching also fails.
    """
    if not candidate_name:
        return {}
    url = (
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
        f"?jr_number=eq.{jr}&candidate_name=ilike.*{requests.utils.quote(candidate_name.strip())}*"
        f"&select=id,upload_to_sap,resume_path,client_recruiter,client_recruiter_email&limit=1"
    )
    resp = requests.get(url, headers=_headers(), timeout=15)
    if resp.status_code == 200:
        data = resp.json()
        if data:
            return data[0]
    return {}

def fetch_retry_sap_records(limit=20):
    url = (
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
        f"?upload_to_sap=neq.Done"
        f"&select=*"
        f"&limit={limit}"
    )

    resp = requests.get(url, headers=_headers(), timeout=30)

    if resp.status_code != 200:
        raise Exception(resp.text)

    return resp.json()
# ─────────────────────────────────────────────
# ✏️ DB UPDATE
# ─────────────────────────────────────────────
# 🔴 Fix #1: update_resume_record now calls _resume_db_payload (no longer broken)
def update_resume_record(record_id: str, row: dict, user: dict, resume_path: str | None = None) -> dict:
    payload = _resume_db_payload(row, user, resume_path=resume_path)
    response = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=eq.{record_id}",
        headers=_headers(),
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
        headers=_headers(),
        json=fields,
        timeout=30,
    )
    response.raise_for_status()
    records = response.json()
    if not records:
        return fields
    return records[0]


# ─────────────────────────────────────────────
# 📊 FETCH
# ─────────────────────────────────────────────
def fetch_all_resume_records() -> list[dict]:
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?select=*",
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────────
# ⬇️ DOWNLOAD
# ─────────────────────────────────────────────
def download_resume(file_path: str) -> bytes:
    signed_url = get_resume_url(file_path)

    if not signed_url:
        raise Exception("Failed to generate signed URL")

    resp = requests.get(signed_url, timeout=30)

    if resp.status_code != 200:
        raise Exception(f"Download failed: {resp.text}")

    return resp.content


# ─────────────────────────────────────────────
# 🏢 JR MASTER
# ─────────────────────────────────────────────
def fetch_active_jr_master() -> list[dict]:
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/jr_master"
        "?select=jr_no,client_recruiter,recruiter_email,skill_name,jr_status",
        headers=_headers(),
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json()
    return [r for r in rows if str(r.get("jr_status", "")).strip().lower() == "active"]


# ─────────────────────────────────────────────
# 📧 EMAIL HELPERS
# ─────────────────────────────────────────────
def fetch_unsent_email_records() -> list[dict]:
    """Fetch records where SAP upload is Done and client email has not been sent."""
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
        "?upload_to_sap=eq.Done&client_email_sent=eq.Pending&select=*",
        headers=_headers(),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def mark_client_email_sent(record_ids: list[str]) -> None:
    """Mark the given record IDs as client_email_sent=Pending."""
    if not record_ids:
        return
    ids_str = ",".join(record_ids)
    response = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=in.({ids_str})",
        headers=_headers(),
        json={"client_email_sent": "Pending"},
        timeout=30,
    )
    response.raise_for_status()


# ─────────────────────────────────────────────
# ✍️ SIGNATURES
# ─────────────────────────────────────────────
def get_user_signature(email: str) -> str:
    """Retrieves the stored signature for a user."""
    if not email:
        return ""
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/recruiter_signatures"
        f"?user_email=eq.{email}&select=signature",
        headers=_headers(),
        timeout=10,
    )
    if response.status_code == 200:
        data = response.json()
        if data:
            return data[0].get("signature", "")
    return ""


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
        headers={**_headers(), "Prefer": "resolution=merge-duplicates"},
        json=payload,
        timeout=10,
    )
    response.raise_for_status()