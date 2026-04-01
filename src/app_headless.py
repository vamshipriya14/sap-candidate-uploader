import io
import re
from datetime import date

import pandas as pd
import streamlit as st

from auth import require_login, show_navigation, show_user_profile
from notifier import send_client_email, send_upload_notification
from resume_parser import parse_resume
from resume_repository import (
    delete_resume_from_shared_drive,
    fetch_active_jr_master,
    fetch_all_resume_records,
    get_user_signature,
    insert_resume_record,
    jr_folder_name,
    save_user_signature,
    update_resume_record,
    upload_resume_to_shared_drive
)
from sap_bot_headless import SAPBot
from uploader import upload_to_sap
import time
import base64
import requests
import urllib.parse as _up
import re as _re


def _download_sharepoint_file(resume_link: str, access_token: str, retries: int = 3) -> bytes:
    """
    Download a file from OneDrive/SharePoint via Microsoft Graph API.
    Strategy 1: /me/drive/root:/{path}:/content  (personal OneDrive path)
    Strategy 2: shares/{encode(url)}/driveItem/@microsoft.graph.downloadUrl
                then fetch the pre-authenticated download URL (no auth needed)
    Strategy 3: raw GET with Authorization header
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    last_exc = None

    for attempt in range(retries):
        try:
            # Strategy 1: personal OneDrive path
            personal_match = _re.search(
                r"/personal/[^/]+/Documents/(.+)$", _up.unquote(resume_link)
            )
            if personal_match:
                relative_path = personal_match.group(1)
                encoded_path  = _up.quote(relative_path)
                graph_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{encoded_path}:/content"
                resp = requests.get(graph_url, headers=headers, timeout=30, allow_redirects=True)
                if resp.status_code == 200:
                    return resp.content

            # Strategy 2: get a pre-authenticated download URL via shares endpoint
            # This works even when /me/drive returns 401 (scope issues)
            try:
                share_token = "u!" + base64.urlsafe_b64encode(
                    resume_link.encode("utf-8")
                ).decode("utf-8").rstrip("=")
                meta_url = f"https://graph.microsoft.com/v1.0/shares/{share_token}/driveItem"
                meta_resp = requests.get(
                    meta_url,
                    headers={**headers, "Prefer": "redeemSharingLink"},
                    params={"$select": "@microsoft.graph.downloadUrl"},
                    timeout=30,
                )
                if meta_resp.status_code == 200:
                    dl_url = meta_resp.json().get("@microsoft.graph.downloadUrl", "")
                    if dl_url:
                        dl_resp = requests.get(dl_url, timeout=30, allow_redirects=True)
                        if dl_resp.status_code == 200:
                            return dl_resp.content
            except Exception:
                pass

            # Strategy 3: raw GET with auth header
            resp = requests.get(resume_link, headers=headers, timeout=30, allow_redirects=True)
            if resp.status_code == 200:
                return resp.content

            raise Exception(f"HTTP {resp.status_code} for {resume_link}")

        except Exception as exc:
            last_exc = exc
            time.sleep(2 * (attempt + 1))

    raise Exception(f"Download failed after {retries} attempts: {last_exc}")


def normalize_upload_error(error: Exception) -> str:
    raw = str(error or "").strip()
    cleaned = raw.split("Stacktrace:", 1)[0].replace("Message:", "").strip()
    first_line = next((line.strip() for line in cleaned.splitlines() if line.strip()), "")
    lower = cleaned.lower()

    if "duplicate" in lower or "already exists" in lower or "already been submitted" in lower:
        return "Duplicate candidate"
    if "requisition id" in lower and "not found" in lower:
        return "Job not found"
    if "job" in lower and "not found" in lower:
        return "Job not found"
    if "agreement box" in lower or "terms checkbox" in lower or "checkbox" in lower:
        return "Agreement checkbox failed"
    if "dialog did not close after cancel" in lower:
        return "Cancel action failed"
    if "dialog did not close after submission" in lower:
        return "Submit action failed"
    if "form did not open" in lower or "open add candidate form" in lower:
        return "Add Candidate form did not open"
    if "resume" in lower and "upload" in lower:
        return "Resume upload failed"
    if "dropdown" in lower or "country code" in lower or "country" in lower:
        return "Country selection failed"
    if "fill text fields" in lower or "first name" in lower or "email" in lower:
        return "Candidate form fill failed"
    if "login failed" in lower or "credentials" in lower:
        return "SAP login failed"
    if "file bytes not found" in lower:
        return "Resume file missing from session"
    return first_line or "Upload failed"


def pretty_user_name(user: dict) -> str:
    display = (user.get("name") or "").strip()
    if display and "@" not in display:
        return " ".join(part.capitalize() for part in display.replace(".", " ").split())

    email = (user.get("email") or "").split("@", 1)[0]
    return " ".join(part.capitalize() for part in email.replace(".", " ").replace("_", " ").split())


def _safe_attachment_part(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", str(value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return cleaned or fallback


def build_email_body(recruiter_name: str, job_title: str, sender_name: str) -> str:
    return (
        f"Hi {recruiter_name or 'Team'},\n\n"
        f"Please find attached profiles for {job_title}\n\n"
        f"Regards,"
    )


def build_email_drafts(successful_rows, metadata_by_jr, user: dict) -> pd.DataFrame:
    sender_name = pretty_user_name(user)
    sender_email = user.get("email", "")
    drafts = []

    grouped = {}
    for row in successful_rows:
        jr = str(row.get("JR Number", "")).strip()
        grouped.setdefault(jr, []).append(row)

    for jr, rows in grouped.items():
        meta = metadata_by_jr.get(jr, {})
        job_title = meta.get("job_title", "")

        # Scan ALL rows for this JR to find any non-empty recruiter name/email
        recruiter_name = str(meta.get("client_recruiter", "")).strip()
        email_to = str(meta.get("email_to", "")).strip()
        for _r in rows:
            if not recruiter_name:
                recruiter_name = str(_r.get("client_recruiter", "")).strip()
            if not email_to:
                email_to = str(_r.get("client_recruiter_email", "")).strip()
            if recruiter_name and email_to:
                break

        drafts.append(
            {
                "JR Number": jr,
                "Job Title": job_title,
                "Client Recruiter Name": recruiter_name,
                "Email To": email_to,
                "CC": "rec_team@volibits.com",
                "Email From": sender_email,
                "Subject": f"BS: {job_title}" if job_title else "BS:",
                "Email Body": build_email_body(recruiter_name, job_title, sender_name),
                "Profiles": len(rows),
                "Files": ", ".join(str(row.get("File Name", "")) for row in rows),
            }
        )

    return pd.DataFrame(
        drafts,
        columns=[
            "JR Number",
            "Job Title",
            "Client Recruiter Name",
            "Email To",
            "CC",
            "Email From",
            "Subject",
            "Email Body",
            "Profiles",
            "Files",
        ],
    )


def build_candidate_details_table(successful_rows, metadata_by_jr) -> pd.DataFrame:
    today_text = date.today().strftime("%d-%b-%Y")
    candidate_rows = []
    seen_keys = set()  # prevent duplicate primary keys

    for row in successful_rows:
        first_name = str(row.get("First Name", "")).strip()
        last_name = str(row.get("Last Name", "")).strip()
        jr = str(row.get("JR Number", "")).strip()
        email_id = str(row.get("Email", "")).strip()
        contact_number = str(row.get("Phone", "")).strip()
        meta = metadata_by_jr.get(jr) or {}

        # Composite primary key
        primary_key = (jr, email_id, contact_number)
        if primary_key in seen_keys:
            continue  # skip duplicates
        seen_keys.add(primary_key)

        candidate_rows.append(
            {
                # Primary key columns first — locked from editing
                "JR Number": jr,
                "Email ID": email_id,
                "Contact Number": contact_number,
                # Editable columns
                "Date": today_text,
                "Skill": meta.get("job_title", "") or row.get("Skill", ""),
                "Candidate Name": " ".join(part for part in [first_name, last_name] if part),
                "Current Company": row.get("Current Company", ""),
                "Total Experience": row.get("Total Experience", ""),
                "Relevant Experience": row.get("Relevant Experience", ""),
                "Current CTC": row.get("Current CTC", ""),
                "Expected CTC": row.get("Expected CTC", ""),
                "Notice Period": row.get("Notice Period", ""),
                "Current Location": row.get("Current Location", ""),
                "Preferred Location": row.get("Preferred Location", ""),
                "comments/Availability": row.get("comments/Availability", row.get("Comments", "")),
            }
        )
    return pd.DataFrame(candidate_rows)


def reset_email_state() -> None:
    st.session_state.email_drafts_df = pd.DataFrame()
    st.session_state.email_candidates_df = pd.DataFrame()
    st.session_state.email_send_status = ""
    st.session_state.selected_email_draft_idx = 0
    st.session_state.last_selected_email_draft_idx = None
    st.session_state.last_rendered_draft_form_signature = None
    for key in list(st.session_state.keys()):
        if str(key).startswith("draft_"):
            del st.session_state[key]


def clear_pending_upload_state() -> None:
    st.session_state.pending_upload_rows = []
    st.session_state.pending_submit_mode = False
    st.session_state.upload_confirmed = False


def _review_row_style(row: pd.Series):
    if str(row.get("Error", "")).strip():
        return ["background-color: #ffe5e5"] * len(row)
    if str(row.get("Upload to SAP", "")).strip() == "Yes":
        return ["background-color: #e8f7e8"] * len(row)
    if str(row.get("Upload to SAP", "")).strip() == "Failed":
        return ["background-color: #ffe5e5"] * len(row)
    return [""] * len(row)


def _row_snapshot(row: dict) -> dict:
    tracked_columns = [
        "JR Number",
        "Date",
        "Skill",
        "File Name",
        "First Name",
        "Last Name",
        "Email",
        "Phone",
        "Current Company",
        "Total Experience",
        "Relevant Experience",
        "Current CTC",
        "Expected CTC",
        "Notice Period",
        "Current Location",
        "Preferred Location",
        "Actual Status",
        "Call Iteration",
        "comments/Availability",
        "Upload to SAP",
        "client_recruiter",
        "client_recruiter_email",
        "recruiter",
        "recruiter_email",
        "Error",
    ]
    snapshot = {}
    for column in tracked_columns:
        value = row.get(column, "")
        snapshot[column] = "" if pd.isna(value) else str(value).strip()
    return snapshot


def _sync_resume_rows_to_db(edited_df: pd.DataFrame, user: dict) -> None:
    # Build unique (JR Number, Email, Phone) set for all records (DB + current edited)
    # We use this to prevent duplicates
    unique_keys = {}  # key -> record_id (if exists in DB)

    # First, collect keys of already known records in the session
    for f_name, record_id in st.session_state.resume_record_ids.items():
        row_data = st.session_state.parsed_resume_rows.get(f_name, {})
        key = (
            str(row_data.get("JR Number", "")).strip(),
            str(row_data.get("Email", "")).strip(),
            str(row_data.get("Phone", "")).strip()
        )
        if key[1] or key[2]:  # Only if Email or Phone is non-empty
            unique_keys[key] = record_id

    for _, row in edited_df.iterrows():
        row_dict = row.to_dict()
        file_name = str(row_dict.get("File Name", "")).strip()
        if not file_name:
            continue

        # Check for duplicates (JR Number, Email, Phone)
        current_key = (
            str(row_dict.get("JR Number", "")).strip(),
            str(row_dict.get("Email", "")).strip(),
            str(row_dict.get("Phone", "")).strip()
        )

        # If JR Number is empty, the user wants it to still be treated as a duplicate
        # if Email and Phone match an existing record WITH empty JR Number.
        # If multiple records have empty JR and same email/phone, they are duplicates.

        # If current_key exists in another record, and this is a new record (no ID), skip it
        existing_id = unique_keys.get(current_key)
        record_id = st.session_state.resume_record_ids.get(file_name)

        if existing_id and not record_id:
            # This is a duplicate of an existing record we already know about
            st.warning(f"Skipping duplicate record for {file_name} (already exists in DB/session)")
            continue

        if not record_id:
            # If we don't have an ID for THIS file_name, but we found an ID for the KEY,
            # then THIS file_name is a duplicate.
            if existing_id:
                st.warning(f"Record for {file_name} matches an existing record. Linking to existing ID.")
                st.session_state.resume_record_ids[file_name] = existing_id
                record_id = existing_id
            else:
                # Add to unique_keys so subsequent identical rows in this same loop are caught
                unique_keys[current_key] = "PENDING"

        jr_folder = jr_folder_name(row_dict.get("JR Number", ""))
        current_link = st.session_state.resume_links.get(file_name, "")

        # Check if we need to upload/move the file on shared drive
        if f"/{jr_folder}/" not in current_link:
            file_bytes = st.session_state.uploaded_files_store.get(file_name)
            if file_bytes:
                previous_folder = "pending_jr" if "/pending_jr/" in current_link else ""
                resume_link = upload_resume_to_shared_drive(user["access_token"], file_name, file_bytes,
                                                            subfolder=jr_folder)
                st.session_state.resume_links[file_name] = resume_link
                if previous_folder and previous_folder != jr_folder:
                    delete_resume_from_shared_drive(user["access_token"], file_name, previous_folder)
                current_link = resume_link

        snapshot = _row_snapshot(row_dict)

        if record_id and record_id != "PENDING":
            # Update existing record if changed
            if st.session_state.resume_row_snapshots.get(file_name) != snapshot:
                update_resume_record(record_id, row_dict, user, resume_link=current_link)
                st.session_state.resume_row_snapshots[file_name] = snapshot
        else:
            # New record - insert into DB
            try:
                # Always default client_email_sent to "No" on new records
                row_dict.setdefault("client_email_sent", "No")
                if str(row_dict.get("client_email_sent", "No")).strip() not in ("Yes", "No"):
                    row_dict["client_email_sent"] = "No"
                record = insert_resume_record(row_dict, user, resume_link=current_link)
                new_id = str(record.get("id", "")).strip()
                st.session_state.resume_record_ids[file_name] = new_id
                st.session_state.resume_row_snapshots[file_name] = snapshot
                unique_keys[current_key] = new_id
            except Exception as e:
                st.error(f"Failed to insert record for {file_name}: {e}")

        st.session_state.parsed_resume_rows[file_name] = dict(row_dict)


def _candidate_display_name(row: pd.Series) -> str:
    return " ".join(
        part for part in [str(row.get("First Name", "")).strip(), str(row.get("Last Name", "")).strip()] if part
    ).strip()


st.set_page_config(page_title="Candidate Submission ATS", page_icon="📋", layout="wide")

# =========================
# AUTH
# =========================
user = require_login()
show_user_profile(user)
show_navigation("new_records")

st.title("Candidate Submission ATS")
st.caption(f"Logged in as **{user['name']}** ({user['email']})")


# =========================
# USER SIGNATURE
# =========================

def _get_default_signature_template(user_dict: dict) -> str:
    name = user_dict.get("name", "Name")
    job_title = user_dict.get("job_title") or "job_title"
    email = user_dict.get("email", "Email")
    phone = user_dict.get("phone") or "+91 0000000000"

    # Volibits logo as base64 JPEG.
    # WHY: Outlook Web App (OWA) strips SVG data URIs entirely — they appear blank.
    #      JPEG/PNG data URIs ARE supported in OWA since 2019.
    _logo = "data:image/jpeg;base64,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"

    # Social icons as CSS-styled HTML link buttons.
    # WHY: OWA blocks external image domains and SVG data URIs.
    #      Pure CSS <a> buttons render correctly in ALL email clients:
    #      Outlook Desktop, OWA, Gmail, Apple Mail, mobile clients.
    def _social_icon(href, label, bg_color, text_color="#ffffff", short_label=None):
        display = short_label or label[0]
        return (
            f'<a href="{href}" style="display:inline-block; width:22px; height:22px; '
            f'background-color:{bg_color}; border-radius:4px; text-align:center; '
            f'line-height:22px; font-family:Arial,sans-serif; font-size:10px; '
            f'font-weight:bold; color:{text_color}; text-decoration:none; margin:0 1px;" '
            f'title="{label}">{display}</a>'
        )

    social_icons_html = (
        _social_icon("https://www.linkedin.com/company/volibits/", "LinkedIn", "#0A66C2", short_label="in") +
        _social_icon("https://www.instagram.com/volibits_llp/", "Instagram", "#E1306C", short_label="IG") +
        _social_icon("https://www.facebook.com/Volibits/", "Facebook", "#1877F2", short_label="f") +
        _social_icon("https://x.com/VolibitsInd", "X / Twitter", "#000000", short_label="&#120143;") +
        _social_icon("https://www.youtube.com/channel/UCmSl5A2JfguK3PtcUdiI8-A", "YouTube", "#FF0000", short_label="&#9654;")
    )

    return f"""<table border="0" cellspacing="0" cellpadding="0"
        style="background:white; border-collapse:collapse; font-family:Arial,sans-serif; font-size:13px; color:#333;">
  <tbody>
    <tr>
      <!-- LEFT COLUMN: Logo + Connect with us + Social icons -->
      <td valign="middle" align="center"
          style="padding:8px 14px 8px 8px; border-right:1.5px solid #595959; width:160px;">
        <a href="http://www.volibits.com/" style="text-decoration:none; display:block; margin-bottom:6px;">
          <img src="{_logo}" width="140" height="45" alt="Volibits"
               style="display:block; border:0; margin:0 auto;">
        </a>
        <div style="font-size:8.5px; color:#5d5d5d; font-weight:700;
                    margin-bottom:6px; letter-spacing:0.3px; font-family:Arial,sans-serif;">
          Connect with us
        </div>
        <div style="text-align:center; line-height:1;">
          {social_icons_html}
        </div>
      </td>

      <!-- RIGHT COLUMN: Name, title, contact details -->
      <td valign="top" style="padding:8px 8px 8px 16px;">
        <p style="margin:0 0 2px 0; font-size:15px; font-weight:700;
                  color:#000; font-family:'Aptos Narrow',Arial,sans-serif;">
          {name}
        </p>
        <p style="margin:0 0 10px 0; font-size:12px; color:#444; font-family:Arial,sans-serif;">
          {job_title}
        </p>
        <p style="margin:0 0 3px 0; font-size:12px; color:#636363; font-weight:700; font-family:Arial,sans-serif;">
          <a href="tel:{phone.replace(' ', '')}" style="color:#0563C1; text-decoration:none;">{phone}</a>
          <span style="color:#636363;">&nbsp;|&nbsp;</span>
          <a href="mailto:{email}" style="color:blue; text-decoration:underline;">{email}</a>
        </p>
        <p style="margin:0 0 3px 0; font-size:12px; font-weight:700; font-family:Arial,sans-serif;">
          <a href="http://www.volibits.com/" style="color:#0058B9; text-decoration:underline;">
            www.volibits.com
          </a>
        </p>
        <p style="margin:0; font-size:12px; color:#636363; font-weight:700; font-family:Arial,sans-serif;">
          203, A Wing, The Capital, Baner-Pashan Link Rd, Baner, Pune, MH, India - 411045
        </p>
      </td>
    </tr>
  </tbody>
</table>"""

if "user_signature" not in st.session_state:
    try:
        db_sig = get_user_signature(user["email"])
        if not db_sig or not db_sig.strip():
            st.session_state.user_signature = _get_default_signature_template(user)
        else:
            st.session_state.user_signature = db_sig
    except Exception:
        st.session_state.user_signature = _get_default_signature_template(user)

try:
    jr_master_rows = fetch_active_jr_master()
except Exception as error:
    jr_master_rows = []
    st.warning(f"JR master lookup unavailable: {error}")

jr_master_by_number = {}
for row in jr_master_rows:
    jr_number = str(row.get("jr_no", "")).strip()
    if jr_number:
        jr_master_by_number[jr_number] = row

active_jr_numbers = sorted(jr_master_by_number.keys())

# Display options for the JR Number dropdown: "JR001 - Skill Name"
# When saved, only jr_no is stored (see save_table_changes handling below).
jr_display_options = [
    f"{jr} - {jr_master_by_number[jr].get('skill_name', '')}"
    if jr_master_by_number[jr].get("skill_name")
    else jr
    for jr in active_jr_numbers
]
# Map display label back to jr_no for stripping after selection
_jr_display_to_no = {disp: jr for jr, disp in zip(active_jr_numbers, jr_display_options)}
active_skills = sorted(
    {
        str(row.get("skill_name", "")).strip()
        for row in jr_master_rows
        if str(row.get("skill_name", "")).strip()
    }
)
active_recruiters = sorted(
    {
        str(row.get("client_recruiter", "")).strip()
        for row in jr_master_rows
        if str(row.get("client_recruiter", "")).strip()
    }
)

# =========================
# SESSION STATE INIT
# =========================
if "uploaded_files_store" not in st.session_state:
    st.session_state.uploaded_files_store = {}
if "email_drafts_df" not in st.session_state:
    st.session_state.email_drafts_df = pd.DataFrame()
if "email_candidates_df" not in st.session_state:
    st.session_state.email_candidates_df = pd.DataFrame()
if "email_send_status" not in st.session_state:
    st.session_state.email_send_status = ""
if "selected_email_draft_idx" not in st.session_state:
    st.session_state.selected_email_draft_idx = 0
if "last_uploaded_signature" not in st.session_state:
    st.session_state.last_uploaded_signature = ()
if "last_email_draft_signature" not in st.session_state:
    st.session_state.last_email_draft_signature = ""
if "last_selected_email_draft_idx" not in st.session_state:
    st.session_state.last_selected_email_draft_idx = None
if "last_rendered_draft_form_signature" not in st.session_state:
    st.session_state.last_rendered_draft_form_signature = None
if "pending_upload_rows" not in st.session_state:
    st.session_state.pending_upload_rows = []
if "pending_submit_mode" not in st.session_state:
    st.session_state.pending_submit_mode = False
if "upload_confirmed" not in st.session_state:
    st.session_state.upload_confirmed = False
if "parsed_resume_rows" not in st.session_state:
    st.session_state.parsed_resume_rows = {}
if "resume_record_ids" not in st.session_state:
    st.session_state.resume_record_ids = {}
if "resume_row_snapshots" not in st.session_state:
    st.session_state.resume_row_snapshots = {}
if "resume_links" not in st.session_state:
    st.session_state.resume_links = {}
if "db_resume_records" not in st.session_state:
    st.session_state.db_resume_records = []

# Fetch DB records for filters and collapsible table
try:
    st.session_state.db_resume_records = fetch_all_resume_records()
except Exception as e:
    st.warning(f"Could not fetch database records: {e}")

# =========================
# FILTERS & STATS
# =========================
st.subheader("Filters & Database Lookup")

_all_db_records = st.session_state.db_resume_records
_all_recruiters_in_db = sorted({
    str(r.get("recruiter", "") or r.get("recruiter_email", "")).strip()
    for r in _all_db_records
    if (r.get("recruiter") or r.get("recruiter_email"))
})
_current_user_recruiter = pretty_user_name(user) or user.get("email", "")
_today = date.today()
_default_start = _today.replace(day=1)

_sf1, _sf2, _sf3 = st.columns([1, 1, 2])
with _sf1:
    _stats_date_from = st.date_input("Date From", value=_default_start, key="stats_date_from",
        help="Filters stats cards and the DB table below.")
with _sf2:
    _stats_date_to = st.date_input("Date To", value=_today, key="stats_date_to")
with _sf3:
    _recruiter_options = ["All Recruiters"] + _all_recruiters_in_db
    _default_recruiter_idx = 0
    for _i, _opt in enumerate(_recruiter_options):
        if _current_user_recruiter.lower() in _opt.lower() or _opt.lower() in _current_user_recruiter.lower():
            _default_recruiter_idx = _i
            break
    _stats_recruiter = st.selectbox("Recruiter", options=_recruiter_options,
        index=_default_recruiter_idx, key="stats_recruiter",
        help="Default is the logged-in user. Select 'All Recruiters' to see everyone.")


def _parse_record_date(r):
    try:
        return date.fromisoformat(r.get("date_text", "") or "")
    except Exception:
        try:
            from datetime import datetime
            return datetime.strptime(str(r.get("date_text", "")), "%d-%b-%Y").date()
        except Exception:
            return None


def _record_matches_stats_filters(r) -> bool:
    rd = _parse_record_date(r)
    if rd is None:
        return False
    if rd < _stats_date_from or rd > _stats_date_to:
        return False
    if _stats_recruiter != "All Recruiters":
        rec = str(r.get("recruiter", "") or "").strip()
        rec_email = str(r.get("recruiter_email", "") or "").strip()
        if _stats_recruiter not in (rec, rec_email):
            return False
    return True


_filtered_stats_records = [r for r in _all_db_records if _record_matches_stats_filters(r)]
_total      = len(_filtered_stats_records)
_uploaded   = sum(1 for r in _filtered_stats_records if str(r.get("upload_to_sap", "")).strip() == "Done")
_pending    = sum(1 for r in _filtered_stats_records if str(r.get("upload_to_sap", "")).strip() not in ("Done", "No"))
_email_sent = sum(1 for r in _filtered_stats_records if str(r.get("client_email_sent", "No")).strip() == "Yes")

_today_str        = _today.strftime("%d-%b-%Y")
_today_records    = [r for r in _filtered_stats_records if str(r.get("date_text", "")).strip() == _today_str]
_today_total      = len(_today_records)
_today_uploaded   = sum(1 for r in _today_records if str(r.get("upload_to_sap", "")).strip() == "Done")
_today_pending    = sum(1 for r in _today_records if str(r.get("upload_to_sap", "")).strip() not in ("Done", "No"))
_today_email_sent = sum(1 for r in _today_records if str(r.get("client_email_sent", "No")).strip() == "Yes")


def _mini_stat(label: str, value, bg: str, text: str = "#ffffff") -> str:
    return (
        f"<div style='background:{bg}; border-radius:8px; padding:8px 6px; "
        f"text-align:center; flex:1; min-width:0;'>"
        f"<div style='font-size:1.15rem; font-weight:700; color:{text};'>{value:,}</div>"
        f"<div style='font-size:0.65rem; color:{text}; opacity:0.85; margin-top:2px;'>{label}</div>"
        f"</div>"
    )


def _mini_stats_row(label, icon, total, uploaded, pending, emails, row_bg, colors):
    c_total, c_up, c_pend, c_email = colors
    st.markdown(
        f"""<div style="background:{row_bg}; border-radius:10px; padding:10px 14px; margin-bottom:8px;">
          <div style="font-size:0.78rem; font-weight:700; color:#ccc; margin-bottom:7px; letter-spacing:0.4px;">
            {icon}&nbsp; {label}
          </div>
          <div style="display:flex; gap:7px;">
            {_mini_stat("Total", total, c_total)}
            {_mini_stat("SAP Done", uploaded, c_up)}
            {_mini_stat("Pending", pending, c_pend)}
            {_mini_stat("Emailed", emails, c_email)}
          </div>
        </div>""",
        unsafe_allow_html=True,
    )


with st.expander("📊 Stats Dashboard", expanded=True):
    _mini_stats_row("Period Total", "📊", _total, _uploaded, _pending, _email_sent,
        row_bg="#1a1f2e", colors=("#2563eb", "#16a34a", "#d97706", "#7c3aed"))
    _mini_stats_row("Today", "🗓️", _today_total, _today_uploaded, _today_pending, _today_email_sent,
        row_bg="#0f1a14", colors=("#0284c7", "#15803d", "#b45309", "#6d28d9"))

with st.expander("📝 Manage Your Email Signature"):
    import streamlit.components.v1 as _sig_components
    if "sig_name" not in st.session_state:
        st.session_state.sig_name = pretty_user_name(user)
    if "sig_job_title" not in st.session_state:
        st.session_state.sig_job_title = user.get("job_title") or ""
    if "sig_phone" not in st.session_state:
        st.session_state.sig_phone = user.get("phone") or ""
    st.caption("Fill in your details below. The signature preview updates automatically.")
    _sig_form_col, _sig_prev_col = st.columns([1, 1], gap="large")
    with _sig_form_col:
        st.markdown("**Your Details**")
        sig_name      = st.text_input("Full Name",    value=st.session_state.sig_name,      key="sig_name_input")
        sig_job_title = st.text_input("Job Title",    value=st.session_state.sig_job_title, placeholder="e.g. Senior recruiter", key="sig_job_title_input")
        sig_phone     = st.text_input("Phone Number", value=st.session_state.sig_phone,     placeholder="e.g. +91 0000000000",   key="sig_phone_input")
        _user_for_sig = {**user, "name": sig_name or pretty_user_name(user), "job_title": sig_job_title, "phone": sig_phone}
        preview_html  = _get_default_signature_template(_user_for_sig)
        _bc1, _bc2 = st.columns(2)
        with _bc1:
            if st.button("Save Signature", use_container_width=True):
                try:
                    st.session_state.sig_name = sig_name
                    st.session_state.sig_job_title = sig_job_title
                    st.session_state.sig_phone = sig_phone
                    save_user_signature(user["email"], preview_html)
                    st.session_state.user_signature = preview_html
                    st.success("Signature saved!")
                except Exception as e:
                    st.error(f"Failed to save: {e}")
        with _bc2:
            if st.button("Reset Fields", use_container_width=True):
                st.session_state.sig_name = pretty_user_name(user)
                st.session_state.sig_job_title = user.get("job_title") or ""
                st.session_state.sig_phone = user.get("phone") or ""
                st.rerun()
    with _sig_prev_col:
        st.markdown("**Signature Preview**")
        _sig_components.html(
            f"""<div style="font-family:Arial,sans-serif; padding:4px;">
                <p style="font-size:12px; color:#888; margin:0 0 8px 0;">— Regards,</p>
                {preview_html}
            </div>""",
            height=165, scrolling=False,
        )

# =========================
# FILE UPLOAD & PARSE
# =========================
files = st.file_uploader(
    "Upload Resumes",
    type=["pdf", "docx"],
    accept_multiple_files=True,
    help="Each resume must have a unique filename. Duplicates will be ignored.",
)

if files:
    seen = set()
    unique_files = []
    for file in files:
        if file.name in seen:
            st.warning(f"Duplicate file skipped: **{file.name}**")
        else:
            seen.add(file.name)
            unique_files.append(file)
    files = unique_files
    current_signature = tuple(sorted(file.name for file in files))
    _new_files_to_process = [
        f for f in files if f.name not in st.session_state.parsed_resume_rows
    ]
    if st.session_state.last_uploaded_signature != current_signature:
        st.session_state.last_uploaded_signature = current_signature

    st.info(f"{len(files)} resume(s) ready for processing")

    results = []
    progress = st.progress(0)
    today_text = date.today().strftime("%d-%b-%Y")

    for index, file in enumerate(files):
        file.seek(0)
        file_bytes = file.read()
        st.session_state.uploaded_files_store[file.name] = file_bytes

        if file.name not in st.session_state.parsed_resume_rows:
            row = {
                "JR Number": "",
                "Date": today_text,
                "Skill": "",
                "File Name": file.name,
                "First Name": "",
                "Last Name": "",
                "Email": "",
                "Phone": "",
                "Current Company": "",
                "Total Experience": "",
                "Relevant Experience": "",
                "Current CTC": "",
                "Expected CTC": "",
                "Notice Period": "",
                "Current Location": "",
                "Preferred Location": "",
                "Actual Status": "Not Called",
                "Call Iteration": "First Call",
                "comments/Availability": "",
                "Error": "",
                "Upload to SAP": "Yes",
                "client_recruiter": "",
                "client_recruiter_email": "",
                "recruiter": user.get("name", ""),
                "recruiter_email": user.get("email", ""),
            }
            try:
                file.seek(0)
                data = parse_resume(file)
                row["First Name"] = data.get("first_name", "")
                row["Last Name"] = data.get("last_name", "")
                row["Email"] = data.get("email", "")
                row["Phone"] = data.get("phone", "")

                # If JR Number is found in the master list, pre-fill the Skill and client_recruiter
                jr_number = str(row.get("JR Number", "")).strip()
                if jr_number in jr_master_by_number:
                    master_row = jr_master_by_number[jr_number]
                    if not str(row.get("Skill", "")).strip():
                        row["Skill"] = str(master_row.get("skill_name", "")).strip()
                    if not str(row.get("client_recruiter", "")).strip():
                        row["client_recruiter"] = str(master_row.get("client_recruiter", "")).strip()
                    if not str(row.get("client_recruiter_email", "")).strip():
                        row["client_recruiter_email"] = str(master_row.get("client_recruiter_email", "")).strip()
            except Exception as error:
                row["Error"] = str(error)

            try:
                # Upload to pending_jr initially, but don't insert into DB yet
                resume_link = upload_resume_to_shared_drive(
                    user["access_token"],
                    file.name,
                    file_bytes,
                    subfolder=jr_folder_name(""),
                )
                st.session_state.resume_links[file.name] = resume_link
            except Exception as error:
                _od_err = str(error).strip()
                row["Error"] = f"{row['Error']} | {_od_err}".strip(" |")
                st.warning(f"OneDrive upload failed for **{file.name}**: {_od_err}")

            st.session_state.resume_row_snapshots[file.name] = _row_snapshot(row)
            st.session_state.parsed_resume_rows[file.name] = row

        progress.progress((index + 1) / len(files))

    # If new files were processed this run, rerun so the table renders immediately
    if _new_files_to_process:
        st.rerun()
else:
    # If no files are currently in the uploader, we don't clear everything anymore.
    # This allows users to work with records imported from the database lookup.
    pass

# Collect all records for the main table from session state
results = [dict(row_data) for row_data in st.session_state.parsed_resume_rows.values()]

# =========================
# VALIDATION & TABLE
# =========================
if results:
    df = pd.DataFrame(results)
else:
    # Create an empty DataFrame with expected columns if no results
    df = pd.DataFrame(columns=[
        "JR Number", "Date", "Skill", "First Name", "Last Name", "Email", "Phone",
        "Current Company", "Total Experience", "Relevant Experience", "Current CTC",
        "Expected CTC", "Notice Period", "Current Location", "Preferred Location",
        "Actual Status", "Call Iteration", "comments/Availability", "Error", "Upload to SAP", "File Name"
    ])

# Convert stored jr_no → display format so SelectboxColumn accepts it as a valid option.
# On save the cleanup step (save_table_changes) strips it back to just jr_no.
if not df.empty and "JR Number" in df.columns:
    def _jr_to_display(val):
        jr = str(val or "").strip()
        if jr and jr in jr_master_by_number:
            skill = str(jr_master_by_number[jr].get("skill_name", "")).strip()
            if skill:
                return f"{jr} - {skill}"
        return jr
    df["JR Number"] = df["JR Number"].apply(_jr_to_display)

df.index = df.index + 1
df = df.reindex(
    columns=[
        "JR Number",
        "Date",
        "Skill",
        "First Name",
        "Last Name",
        "Email",
        "Phone",
        "Current Company",
        "Total Experience",
        "Relevant Experience",
        "Current CTC",
        "Expected CTC",
        "Notice Period",
        "Current Location",
        "Preferred Location",
        "Actual Status",
        "Call Iteration",
        "comments/Availability",
        "Error",
        "Upload to SAP",
        "File Name",
    ]
)
invalid_count = len(df[(df["First Name"].fillna("").str.strip() == "") | (df["Email"].fillna("").str.strip() == "")])
if invalid_count:
    st.warning(f"{invalid_count} resume(s) need correction before upload")

# =========================
# FILTERS & COLLAPSIBLE DB TABLE
# =========================
st.subheader("Filters & Database Lookup")
db_df = pd.DataFrame(st.session_state.db_resume_records)
if not db_df.empty:
    db_df["Candidate Name"] = db_df.apply(
        lambda row: " ".join(
            part for part in [str(row.get("first_name", "")).strip(), str(row.get("last_name", "")).strip()] if part
        ).strip(),
        axis=1
    )
    # Map DB column names to UI column names for consistency
    db_df = db_df.rename(columns={
        "jr_number": "JR Number",
        "date_text": "Date",
        "skill": "Skill",
        "file_name": "File Name",
        "first_name": "First Name",
        "last_name": "Last Name",
        "email": "Email",
        "phone": "Phone",
        "current_company": "Current Company",
        "total_experience": "Total Experience",
        "relevant_experience": "Relevant Experience",
        "current_ctc": "Current CTC",
        "expected_ctc": "Expected CTC",
        "notice_period": "Notice Period",
        "current_location": "Current Location",
        "preferred_location": "Preferred Location",
        "upload_to_sap": "Upload to SAP",
        "actual_status": "Actual Status",
        "call_iteration": "Call Iteration",
        "comments_availability": "comments/Availability",
        "error_message": "Error",
        "resume_link": "resume_link",
        "client_recruiter": "client_recruiter",
        "client_recruiter_email": "client_recruiter_email",
        "recruiter": "recruiter",
        "recruiter_email": "recruiter_email",
        "client_email_sent": "Email Sent",
    })

filter_source_df = db_df.copy() if not db_df.empty else pd.DataFrame(
    columns=["Candidate Name", "JR Number", "Actual Status", "Call Iteration", "Upload to SAP"])

f1, f2, f3, f4, f5 = st.columns(5)
with f1:
    candidate_filter = st.multiselect(
        "Candidate Name",
        options=sorted(
            name for name in filter_source_df["Candidate Name"].unique() if name) if not filter_source_df.empty else [],
    )
with f2:
    jr_filter_values = st.multiselect(
        "JR Number",
        options=sorted(value for value in filter_source_df["JR Number"].fillna("").astype(str).str.strip().unique() if
                       value) if not filter_source_df.empty else [],
    )
with f3:
    actual_status_filter = st.multiselect(
        "Call Status",
        options=sorted(
            value for value in filter_source_df["Actual Status"].fillna("").astype(str).str.strip().unique() if
            value) if not filter_source_df.empty else [],
    )
with f4:
    call_iteration_filter = st.multiselect(
        "Call Iteration",
        options=sorted(
            value for value in filter_source_df["Call Iteration"].fillna("").astype(str).str.strip().unique() if
            value) if not filter_source_df.empty else [],
    )
with f5:
    upload_filter = st.multiselect(
        "Upload to SAP",
        options=sorted(
            value for value in filter_source_df["Upload to SAP"].fillna("").astype(str).str.strip().unique() if
            value) if not filter_source_df.empty else [],
    )

filtered_db_df = filter_source_df.copy()
if candidate_filter:
    filtered_db_df = filtered_db_df[filtered_db_df["Candidate Name"].isin(candidate_filter)]
if jr_filter_values:
    filtered_db_df = filtered_db_df[
        filtered_db_df["JR Number"].fillna("").astype(str).str.strip().isin(jr_filter_values)]
if actual_status_filter:
    filtered_db_df = filtered_db_df[
        filtered_db_df["Actual Status"].fillna("").astype(str).str.strip().isin(actual_status_filter)]
if call_iteration_filter:
    filtered_db_df = filtered_db_df[
        filtered_db_df["Call Iteration"].fillna("").astype(str).str.strip().isin(call_iteration_filter)]
if upload_filter:
    filtered_db_df = filtered_db_df[
        filtered_db_df["Upload to SAP"].fillna("").astype(str).str.strip().isin(upload_filter)]

with st.expander("Searchable Database Records - Add to Main Table", expanded=False):
    if filtered_db_df.empty:
        st.info("No records match the filters")
    else:
        # Reorder columns to match main table
        display_cols = [
            "JR Number", "Date", "Skill", "First Name", "Last Name", "Email", "Phone",
            "Current Company", "Total Experience", "Relevant Experience", "Current CTC",
            "Expected CTC", "Notice Period", "Current Location", "Preferred Location",
            "Actual Status", "Call Iteration", "comments/Availability", "Error", "Upload to SAP", "File Name"
        ]

        # Add selection column
        filtered_db_df["Select"] = False
        avail_cols = [c for c in display_cols if c in filtered_db_df.columns]
        select_cols = ["Select"] + avail_cols

        # Display table (recruiter columns are not in display_cols)
        db_editor = st.data_editor(
            filtered_db_df[select_cols],
            hide_index=True,
            num_rows="fixed",
            width="stretch",
            disabled=avail_cols,
            key="db_records_editor"
        )

        if st.button("Add Selected Records to Main Table"):
            selected_rows_indices = db_editor[db_editor["Select"] == True].index
            selected_rows = filtered_db_df.loc[selected_rows_indices]
            if selected_rows.empty:
                st.warning("No rows selected")
            else:
                for _, row in selected_rows.iterrows():
                    file_name = str(row.get("File Name", "")).strip()
                    if not file_name:
                        file_name = f"db_record_{row.get('id', 'unknown')}"

                    # Store link and record ID for added records
                    # Search in original st.session_state.db_resume_records
                    original_record = None
                    # We need a way to find the ID. Let's include ID in the filtered_db_df but hide it if needed
                    # For now, let's assume 'id' is in filtered_db_df
                    if 'id' in row:
                        original_record = next(
                            (r for r in st.session_state.db_resume_records if str(r.get("id")) == str(row.get("id"))),
                            {})

                    if original_record:
                        st.session_state.resume_record_ids[file_name] = str(original_record.get("id", ""))
                        _stored_link = str(original_record.get("resume_link", "") or "").strip()
                        if _stored_link:
                            st.session_state.resume_links[file_name] = _stored_link

                    row_data = row.to_dict()
                    row_data.pop("Select", None)
                    row_data["File Name"] = file_name

                    # Ensure it's in parsed_resume_rows so main table shows it
                    st.session_state.parsed_resume_rows[file_name] = row_data
                    # Snapshot it so it doesn't trigger immediate sync unless changed
                    st.session_state.resume_row_snapshots[file_name] = _row_snapshot(row_data)
                    # Ensure resume_link is in session so sync + pre-download can find it
                    _rl = str(row_data.get("resume_link", "") or "").strip()
                    if _rl and file_name not in st.session_state.resume_links:
                        st.session_state.resume_links[file_name] = _rl

                st.success(f"Added {len(selected_rows)} record(s) to the table below.")
                st.rerun()

st.subheader("Review & Edit Data")
with st.form("resume_editor_form"):
    editor_df = st.data_editor(
        df,
        num_rows="dynamic",
        width="stretch",
        disabled=["File Name"],
        column_config={
            "JR Number": st.column_config.SelectboxColumn(
                "JR Number",
                options=jr_display_options,
                help="Select JR Number — shows JR No and skill name. Only the JR No is saved.",
                pinned=True,
            ),
            "Date": st.column_config.Column(
                pinned=True,
            ),
            "Skill": st.column_config.TextColumn(
                "Skill",
                width="small",
                pinned=True,
            ),
            "First Name": st.column_config.Column(
                pinned=True,
            ),
            "Actual Status": st.column_config.SelectboxColumn(
                "Actual Status",
                options=[
                    "Not Called",
                    "Called",
                    "No Answer",
                    "Interested",
                    "Not Interested",
                    "Wrong Number",
                    "Call Back Later",
                    "Interview Scheduled",
                ],
            ),
            "Upload to SAP": st.column_config.SelectboxColumn(
                "Upload to SAP",
                options=["Yes", "No", "Done", "Failed"],
            ),
            "Call Iteration": st.column_config.SelectboxColumn(
                "Call Iteration",
                options=[
                    "First Call",
                    "Recall Once",
                    "Recall Twice",
                    "Recall Thrice",
                ],
            ),
        },
        key="resume_editor",
    )
    save_table_changes = st.form_submit_button("Save Table Changes", use_container_width=True)

if save_table_changes:
    editor_df = editor_df.dropna(how="all")
    editor_df = editor_df[
        ~(editor_df[["First Name", "Last Name", "Email", "Phone"]].fillna("").apply(lambda x: x.str.strip()).eq("").all(
            axis=1))
    ]

    for _, row in editor_df.iterrows():
        file_name = str(row.get("File Name", "")).strip()
        if not file_name:
            continue

        # Merge edited row data back into session state, preserving hidden columns
        current_data = st.session_state.parsed_resume_rows.get(file_name, {}).copy()
        current_data.update(row.to_dict())

        # Strip skill suffix if user selected a display option like "JR001 - Skill"
        jr_raw = str(current_data.get("JR Number", "")).strip()
        jr_number = _jr_display_to_no.get(jr_raw, jr_raw.split(" - ")[0].strip() if " - " in jr_raw else jr_raw)
        current_data["JR Number"] = jr_number

        if jr_number in jr_master_by_number:
            master_row = jr_master_by_number[jr_number]
            if not str(current_data.get("Skill", "")).strip():
                current_data["Skill"] = str(master_row.get("skill_name", "")).strip()
            if not str(current_data.get("client_recruiter", "")).strip():
                current_data["client_recruiter"] = str(master_row.get("client_recruiter", "")).strip()
            if not str(current_data.get("client_recruiter_email", "")).strip():
                current_data["client_recruiter_email"] = str(master_row.get("client_recruiter_email", "")).strip()

        st.session_state.parsed_resume_rows[file_name] = current_data
    st.rerun()

all_rows_df = pd.DataFrame(list(st.session_state.parsed_resume_rows.values()))
all_rows_df.index = all_rows_df.index + 1
all_rows_df = all_rows_df.reindex(columns=df.columns)
edited_df = all_rows_df.dropna(how="all")
edited_df = edited_df[
    ~(edited_df[["First Name", "Last Name", "Email", "Phone"]].fillna("").apply(lambda x: x.str.strip()).eq("").all(
        axis=1))
]

# Strip any display label ("JR001 - Skill") back to plain jr_no before sync/upload.
# df["JR Number"] was converted to display labels for the SelectboxColumn — edited_df
# inherits those labels. _sync_resume_rows_to_db and jr_folder_name need plain jr_no.
def _strip_jr_label(val):
    raw = str(val or "").strip()
    return _jr_display_to_no.get(raw, raw.split(" - ")[0].strip() if " - " in raw else raw)

if "JR Number" in edited_df.columns:
    edited_df = edited_df.copy()
    edited_df["JR Number"] = edited_df["JR Number"].apply(_strip_jr_label)

if edited_df.empty:
    st.warning("No valid data to upload")
    st.stop()

try:
    _sync_resume_rows_to_db(edited_df, user)
except Exception as error:
    st.error(f"Supabase sync failed: {error}")

missing_jr = edited_df[edited_df["JR Number"].fillna("").str.strip() == ""]
if not missing_jr.empty:
    st.warning(f"{len(missing_jr)} row(s) are missing JR Number - fill them before uploading")

# =========================
# DOWNLOAD CSV
# =========================
csv = edited_df.to_csv(index=False).encode("utf-8")
st.download_button("Download CSV", data=csv, file_name="parsed_resumes.csv", mime="text/csv")

st.divider()

# =========================
# SINGLE-ACTION SAP UPLOAD
# =========================
st.subheader("SAP Upload")

submit_mode = st.toggle(
    "Submit candidates (Add Candidate)",
    value=False,
    help="ON = submit candidates. OFF = dry run and cancel at the end.",
)

if submit_mode:
    st.caption("Live mode - upload will connect to SAP, submit candidates, and close automatically.")
else:
    st.caption("Dry run mode - upload will connect to SAP, fill the form, cancel, and close automatically.")

if st.button("Upload", type="primary", width="stretch"):
    # Ensure all changes are saved to DB before starting SAP upload
    try:
        _sync_resume_rows_to_db(edited_df, user)
    except Exception as error:
        st.error(f"Sync to database failed before upload: {error}")
        st.stop()

    reset_email_state()
    upload_rows = edited_df[
        (edited_df["Upload to SAP"].fillna("").str.strip() == "Yes")
        & (edited_df["First Name"].fillna("").str.strip() != "")
        & (edited_df["Email"].fillna("").str.strip() != "")
        & (edited_df["JR Number"].fillna("").str.strip() != "")
        ]

    if upload_rows.empty:
        st.error("No valid rows with JR Number to upload")
    else:
        st.session_state.pending_upload_rows = upload_rows.to_dict(orient="records")
        st.session_state.pending_submit_mode = submit_mode
        st.session_state.upload_confirmed = False
        st.rerun()

if st.session_state.pending_upload_rows and not st.session_state.upload_confirmed:
    st.warning("Confirm the candidates below before SAP upload.")
    confirm_df = pd.DataFrame(st.session_state.pending_upload_rows)
    confirm_df["Candidate Name"] = (
            confirm_df["First Name"].fillna("").astype(str).str.strip()
            + " "
            + confirm_df["Last Name"].fillna("").astype(str).str.strip()
    ).str.strip()
    st.dataframe(confirm_df[["Candidate Name", "JR Number"]], width="stretch", hide_index=True)
    confirm_col, cancel_col = st.columns(2)
    with confirm_col:
        if st.button("Confirm Upload", type="primary", width="stretch"):
            st.session_state.upload_confirmed = True
            st.rerun()
    with cancel_col:
        if st.button("Cancel Upload", width="stretch"):
            clear_pending_upload_state()
            st.rerun()

if st.session_state.upload_confirmed and st.session_state.pending_upload_rows:
    submit_mode = st.session_state.pending_submit_mode
    upload_rows = pd.DataFrame(st.session_state.pending_upload_rows)
    bot = None
    results_log = []
    successful_rows = []
    metadata_by_jr = {}
    failed_upload_attachments = []
    upload_progress = st.progress(0)
    status_box = st.empty()

    try:
        # ── Pre-download ALL resume files BEFORE starting the SAP bot ──────────
        # The access token can expire during a long SAP session (401 errors).
        # Downloading everything upfront while the token is fresh avoids this.
        status_box.info("Downloading resume files...")
        for _, _pre_row in upload_rows.iterrows():
            _pre_fname = str(_pre_row.get("File Name", "")).strip()
            if not _pre_fname:
                continue
            if st.session_state.uploaded_files_store.get(_pre_fname):
                continue  # already in cache
            _pre_link = st.session_state.resume_links.get(_pre_fname)
            if not _pre_link:
                continue
            try:
                _pre_bytes = _download_sharepoint_file(_pre_link, user["access_token"])
                st.session_state.uploaded_files_store[_pre_fname] = _pre_bytes
            except Exception as _pre_exc:
                st.warning(f"Pre-download failed for {_pre_fname}: {_pre_exc}")

        status_box.info("Connecting to SAP...")
        bot = SAPBot()
        bot.start()
        bot.login()

        for index, (_, row) in enumerate(upload_rows.iterrows()):
            status_box.info(f"Uploading {row['File Name']} ({index + 1}/{len(upload_rows)})...")
            try:
                jr_number = str(row["JR Number"]).strip()
                if jr_number and jr_number not in metadata_by_jr:
                    metadata_by_jr[jr_number] = bot.get_job_email_details(jr_number)

                file_name = str(row.get("File Name", "")).strip()
                file_bytes = st.session_state.uploaded_files_store.get(file_name)
                if not file_bytes:
                    # Last-resort attempt in case pre-download missed it
                    resume_link = st.session_state.resume_links.get(file_name)
                    if not resume_link:
                        raise Exception("File bytes not found in session and no resume link available")
                    file_bytes = _download_sharepoint_file(resume_link, user["access_token"])
                    st.session_state.uploaded_files_store[file_name] = file_bytes

                file_obj = io.BytesIO(file_bytes)
                file_obj.name = file_name

                upload_to_sap(
                    bot,
                    {
                        "jr_number": jr_number,
                        "first_name": row["First Name"],
                        "last_name": row["Last Name"],
                        "submit": submit_mode,
                        "email": row["Email"],
                        "phone": row["Phone"],
                        "country_code": "+91",
                        "country": "India",
                        "resume_file": file_obj,
                    },
                )

                # Update Skill and recruiter details from SAP metadata
                if jr_number in metadata_by_jr:
                    meta = metadata_by_jr[jr_number]
                    if not str(row.get("Skill", "")).strip():
                        row["Skill"] = str(meta.get("job_title", "")).strip()
                    if not str(row.get("client_recruiter", "")).strip():
                        row["client_recruiter"] = str(meta.get("client_recruiter", "")).strip()
                    if not str(row.get("client_recruiter_email", "")).strip():
                        row["client_recruiter_email"] = str(meta.get("email_to", "")).strip()

                row["Upload to SAP"] = "Done"
                file_name = str(row.get("File Name", "")).strip()
                if file_name:
                    existing = st.session_state.parsed_resume_rows.get(file_name, {}).copy()
                    row_dict = row.to_dict()
                    # Preserve fields absent from upload_rows (e.g. client_email_sent).
                    # edited_df never carries client_email_sent, so row.to_dict() has
                    # it as NaN/empty — blindly calling existing.update() wipes the DB value.
                    for field in ["client_email_sent"]:
                        existing_val = str(existing.get(field, "")).strip()
                        incoming_val = str(row_dict.get(field, "")).strip()
                        if existing_val and not incoming_val:
                            row_dict[field] = existing_val
                    existing.update(row_dict)
                    updated_row = existing
                    st.session_state.parsed_resume_rows[file_name] = updated_row
                    st.session_state.resume_row_snapshots[file_name] = _row_snapshot(updated_row)
                    record_id = st.session_state.resume_record_ids.get(file_name)
                    if record_id:
                        update_resume_record(
                            record_id,
                            updated_row,
                            user,
                            resume_link=st.session_state.resume_links.get(file_name, ""),
                        )
                results_log.append({"File": row["File Name"], "Status": "Success"})
                successful_rows.append(row.to_dict())
            except Exception as error:
                screenshot_name = None
                if bot:
                    try:
                        row["Upload to SAP"] = "Failed"
                        file_name = str(row.get("File Name", "")).strip()
                        if file_name:
                            existing = st.session_state.parsed_resume_rows.get(file_name, {}).copy()
                            existing.update(row.to_dict())
                            updated_row = existing
                            st.session_state.parsed_resume_rows[file_name] = updated_row
                            st.session_state.resume_row_snapshots[file_name] = _row_snapshot(updated_row)
                            record_id = st.session_state.resume_record_ids.get(file_name)
                            if record_id:
                                update_resume_record(
                                    record_id,
                                    updated_row,
                                    user,
                                    resume_link=st.session_state.resume_links.get(file_name, ""),
                                )

                        candidate_name = " ".join(
                            part for part in
                            [str(row.get("First Name", "")).strip(), str(row.get("Last Name", "")).strip()] if part
                        ).strip()
                        screenshot_name = (
                            f"{_safe_attachment_part(jr_number, 'unknown_jr')}_"
                            f"{_safe_attachment_part(candidate_name, 'candidate')}_failed_upload"
                        )
                        screenshot_path = bot._screenshot(screenshot_name)
                        failed_upload_attachments.append(
                            {
                                "name": f"{screenshot_name}.png",
                                "content": screenshot_path.read_bytes(),
                            }
                        )
                    except Exception:
                        pass
                results_log.append(
                    {
                        "File": row["File Name"],
                        "Status": normalize_upload_error(error),
                    }
                )

            upload_progress.progress((index + 1) / len(upload_rows))

    except Exception as error:
        friendly = normalize_upload_error(error)
        if not results_log:
            for _, row in upload_rows.iterrows():
                results_log.append({"File": row["File Name"], "Status": friendly})
        status_box.error(f"SAP upload failed: {friendly}")
    finally:
        if bot:
            try:
                bot.close()
            except Exception:
                pass
        status_box.empty()

    st.session_state.email_drafts_df = build_email_drafts(successful_rows, metadata_by_jr, user)
    st.session_state.email_candidates_df = build_candidate_details_table(successful_rows, metadata_by_jr)

    results_df = pd.DataFrame(results_log)
    success_count = len(results_df[results_df["Status"] == "Success"])
    failed_count = len(results_df) - success_count

    if failed_count == 0:
        st.success(f"All {success_count} candidate(s) processed successfully.")
    else:
        st.warning(f"{success_count} succeeded, {failed_count} failed.")

    st.dataframe(results_df, width="stretch")

    with st.spinner("Sending upload report..."):
        ok, msg = send_upload_notification(
            access_token=user["access_token"],
            user=user,
            results=results_log,
            submit_mode=submit_mode,
            attachments=failed_upload_attachments,
        )

    if ok:
        st.info(f"Upload report sent to **{user['email']}**")
    else:
        st.warning(msg)

    clear_pending_upload_state()

if not st.session_state.email_drafts_df.empty:
    current_draft_signature = st.session_state.email_drafts_df.to_json(orient="split")
    if st.session_state.last_email_draft_signature != current_draft_signature:
        for key in list(st.session_state.keys()):
            if str(key).startswith("draft_"):
                del st.session_state[key]
        st.session_state.selected_email_draft_idx = 0
        st.session_state.last_selected_email_draft_idx = None
        st.session_state.last_rendered_draft_form_signature = None
        st.session_state.last_email_draft_signature = current_draft_signature

    st.divider()
    preview_options = []
    for idx, row in st.session_state.email_drafts_df.iterrows():
        jr = str(row.get("JR Number", "")).strip()
        title = str(row.get("Job Title", "")).strip()
        preview_options.append((idx, f"{jr} - {title}" if title else jr))

    valid_indices = [opt[0] for opt in preview_options]
    if st.session_state.selected_email_draft_idx not in valid_indices:
        st.session_state.selected_email_draft_idx = valid_indices[0]

    selected_idx = st.selectbox(
        "Email Draft",
        options=valid_indices,
        key="selected_email_draft_idx",
        format_func=lambda opt: next((label for i, label in preview_options if i == opt), str(opt)),
    )
    draft_row = st.session_state.email_drafts_df.loc[selected_idx].to_dict()

    current_form_signature = f"{current_draft_signature}:{selected_idx}"
    if st.session_state.last_rendered_draft_form_signature != current_form_signature:
        st.session_state[f"draft_recruiter_name_{selected_idx}"] = str(draft_row.get("Client Recruiter Name", ""))
        st.session_state[f"draft_email_to_{selected_idx}"] = str(draft_row.get("Email To", ""))
        st.session_state[f"draft_email_from_{selected_idx}"] = str(draft_row.get("Email From", ""))
        st.session_state[f"draft_jr_{selected_idx}"] = str(draft_row.get("JR Number", ""))
        st.session_state[f"draft_cc_{selected_idx}"] = str(draft_row.get("CC", ""))
        st.session_state[f"draft_subject_{selected_idx}"] = str(draft_row.get("Subject", ""))
        st.session_state[f"draft_body_{selected_idx}"] = str(draft_row.get("Email Body", ""))
        st.session_state.last_rendered_draft_form_signature = current_form_signature
        st.session_state.last_selected_email_draft_idx = selected_idx

    st.subheader("Email Details")
    st.caption("Edit the email fields here. The form uses full-width inputs for long values.")

    recruiter_options = active_recruiters.copy()
    current_recruiter_value = str(st.session_state.get(f"draft_recruiter_name_{selected_idx}", "")).strip()
    if current_recruiter_value and current_recruiter_value not in recruiter_options:
        recruiter_options = sorted(recruiter_options + [current_recruiter_value])

    col1, col2 = st.columns(2)
    with col1:
        recruiter_name = st.selectbox(
            "Client Recruiter Name",
            options=recruiter_options if recruiter_options else [current_recruiter_value or ""],
            key=f"draft_recruiter_name_{selected_idx}",
        )
        email_to = st.text_input(
            "Email To",
            key=f"draft_email_to_{selected_idx}",
            width="stretch",
        )
        email_from = st.text_input(
            "Email From",
            key=f"draft_email_from_{selected_idx}",
            width="stretch",
            disabled=True,
        )
    with col2:
        jr_number = st.text_input(
            "JR Number",
            key=f"draft_jr_{selected_idx}",
            width="stretch",
            disabled=True,
        )
        cc_value = st.text_input(
            "CC",
            key=f"draft_cc_{selected_idx}",
            width="stretch",
            help="Comma-separated email addresses. rec_team@volibits.com should remain included.",
        )
        subject = st.text_input(
            "Subject",
            key=f"draft_subject_{selected_idx}",
            width="stretch",
        )

    body_text = st.text_area(
        "Email Body",
        key=f"draft_body_{selected_idx}",
        height=160,
        width="stretch",
    )

    st.session_state.email_drafts_df.at[selected_idx, "Client Recruiter Name"] = recruiter_name
    st.session_state.email_drafts_df.at[selected_idx, "Email To"] = email_to
    st.session_state.email_drafts_df.at[selected_idx, "CC"] = cc_value
    st.session_state.email_drafts_df.at[selected_idx, "Subject"] = subject
    st.session_state.email_drafts_df.at[selected_idx, "Email Body"] = body_text
    draft_row = st.session_state.email_drafts_df.loc[selected_idx].to_dict()

    jr_filter = str(draft_row.get("JR Number", "")).strip()
    candidate_rows = []
    if not st.session_state.email_candidates_df.empty:
        candidate_rows = st.session_state.email_candidates_df[
            st.session_state.email_candidates_df["JR Number"].fillna("").astype(str).str.strip() == jr_filter
            ].to_dict(orient="records")

    body_text = str(draft_row.get("Email Body", "")).strip()
    preview_lines = [
        f"From: {draft_row.get('Email From', '')}",
        f"To: {draft_row.get('Email To', '')}",
        f"CC: {draft_row.get('CC', '')}",
        f"Subject: {draft_row.get('Subject', '')}",
        "",
        body_text,
    ]
    if user.get("signature") or st.session_state.get("user_signature"):
        preview_lines.append("\n--- Signature ---")
        preview_lines.append(user.get("signature") or st.session_state.get("user_signature"))

    import streamlit.components.v1 as components

    st.subheader("Email Preview")

    # Build a styled email-like preview box
    header_html = "".join([
        f"<div style='margin-bottom:4px; font-size:12px; color:#444; font-family:Arial,sans-serif;'>"
        f"<strong>{label}:</strong> {value}</div>"
        for label, value in [
            ("From", draft_row.get("Email From", "")),
            ("To", draft_row.get("Email To", "")),
            ("CC", draft_row.get("CC", "")),
            ("Subject", draft_row.get("Subject", "")),
        ]
    ])

    body_html = body_text.replace("\n", "<br>")

    signature_html = user.get("signature") or st.session_state.get("user_signature", "")

    components.html(
        f"""
        <div style="background:#f5f5f5; border:1px solid #ddd; border-radius:6px;
                    padding:16px; font-family:Arial,sans-serif; font-size:13px;">
          <div style="border-bottom:1px solid #ddd; padding-bottom:10px; margin-bottom:12px;">
            {header_html}
          </div>
          <div style="color:#222; line-height:1.6; margin-bottom:16px; white-space:pre-line;">
            {body_html}
          </div>
          <div style="border-top:1px solid #eee; padding-top:12px; margin-top:8px;">
            {signature_html}
          </div>
        </div>
        """,
        height=320,
        scrolling=True,
    )
    if candidate_rows:
        st.caption("Candidate table that will be included in email")
        st.dataframe(pd.DataFrame(candidate_rows), width="stretch")

    if st.button("Send Email", type="primary", width="stretch"):
        attachment_items = []
        for file_name in [part.strip() for part in str(draft_row.get("Files", "")).split(",") if part.strip()]:
            file_bytes = st.session_state.uploaded_files_store.get(file_name)
            if file_bytes:
                attachment_items.append({"name": file_name, "content": file_bytes})

        # Ensure signature is in user dict for send_client_email
        user_to_send = dict(user)
        if st.session_state.get("user_signature"):
            user_to_send["signature"] = st.session_state["user_signature"]

        ok, msg = send_client_email(
            user=user_to_send,
            draft=draft_row,
            candidate_rows=candidate_rows,
            attachments=attachment_items,
        )
        st.session_state.email_send_status = f"ok::{msg}" if ok else f"err::{msg}"
        st.rerun()

    if st.session_state.email_send_status:
        state, text = st.session_state.email_send_status.split("::", 1)
        if state == "ok":
            st.success(text)
        else:
            st.error(text)
else:
    st.session_state.last_email_draft_signature = ""