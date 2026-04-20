import re
import hashlib
from datetime import datetime, timezone

import requests
import streamlit as st
from dotenv import load_dotenv
from streamlit.errors import StreamlitSecretNotFoundError

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


def _headers(json=True):
    return {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json" if json else "application/octet-stream",
        "apikey": SUPABASE_KEY,
    }


def _clean_file_name(name: str) -> str:
    return re.sub(r"[<>:\"/\\|?*]+", "_", str(name or "").strip())


def jr_folder_name(jr_number: str) -> str:
    return re.sub(r"[<>:\"/\\|?*]+", "_", str(jr_number or "").strip()) or "pending_jr"


def _candidate_name(row: dict) -> str:
    return " ".join(
        part for part in [row.get("First Name", ""), row.get("Last Name", "")]
        if part
    ).strip()


# ─────────────────────────────────────────────
# 📤 UPLOAD TO SUPABASE STORAGE
# ─────────────────────────────────────────────
def upload_resume(file_name: str, content: bytes, jr_number: str) -> str:
    file_hash = hashlib.md5(content).hexdigest()
    ext = file_name.split(".")[-1]
    safe_name = f"{file_hash}.{ext}"

    folder = jr_folder_name(jr_number)
    file_path = f"{folder}/{safe_name}"

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
def cleanup_old_resumes(days=30):
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

        if now - created_time > timedelta(days=days):
            delete_resume(name)


# ─────────────────────────────────────────────
# 💾 DB INSERT
# ─────────────────────────────────────────────
def insert_resume_record(row: dict, user: dict, resume_link=None) -> dict:
    payload = {
        "jr_number": str(row.get("JR Number", "")).strip(),
        "date_text": str(row.get("Date", "")).strip(),
        "skill": str(row.get("Skill", "")).strip(),
        "file_name": str(row.get("File Name", "")).strip(),
        "resume_path": resume_link,
        "first_name": str(row.get("First Name", "")).strip(),
        "last_name": str(row.get("Last Name", "")).strip(),
        "candidate_name": _candidate_name(row),
        "email": str(row.get("Email", "")).strip(),
        "phone": str(row.get("Phone", "")).strip(),
        "created_at": _now_iso(),
        "recruiter": user.get("name", ""),
        "recruiter_email": user.get("email", ""),
    }

    # remove empty values
    payload = {k: v for k, v in payload.items() if v not in ("", None)}

    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        headers={**_headers(), "Prefer": "return=representation"},
        json=payload,
        timeout=30,
    )

    if resp.status_code not in (200, 201):
        raise Exception(resp.text)

    return resp.json()[0]


# ─────────────────────────────────────────────
# 📊 FETCH
# ─────────────────────────────────────────────
def fetch_all_resume_records():
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?select=*",
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()

# ─────────────────────────────────────────────
# Download
# ─────────────────────────────────────────────

def download_resume(file_path: str) -> bytes:
    # Step 1: get signed URL
    signed_url = get_resume_url(file_path)

    if not signed_url:
        raise Exception("Failed to generate signed URL")

    # Step 2: download file
    resp = requests.get(signed_url, timeout=30)

    if resp.status_code != 200:
        raise Exception(f"Download failed: {resp.text}")

    return resp.content