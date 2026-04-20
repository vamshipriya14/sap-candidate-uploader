import io
import re
from datetime import date

import pandas as pd
import streamlit as st

from auth import require_login, show_navigation, show_user_profile
from notifier import send_upload_notification
from resume_parser import parse_resume
from resume_repository import (
    fetch_active_jr_master,
    fetch_all_resume_records,
    insert_resume_record,
    jr_folder_name,
    update_resume_record,
    upload_resume,
    download_resume,
    delete_resume,
)
from sap_bot_headless import SAPBot
from uploader import upload_to_sap
import time
import base64
import requests
import urllib.parse as _up
import re as _re

# Fields that are never exposed in the data editor and must never be
# overwritten with NaN/empty values when merging rows back from edited_df.
PROTECTED_FIELDS = [
    "client_email_sent",
    "client_recruiter",
    "client_recruiter_email",
    "recruiter",
    "recruiter_email",
]

EMAIL_CC = st.secrets.get("EMAIL_CC", [])

def _safe_merge(base: dict, incoming: dict) -> dict:
    """
    Merge incoming dict into base dict, but for every field in PROTECTED_FIELDS,
    only overwrite if the incoming value is a real non-empty, non-nan string.
    This prevents NaN/empty values from the data editor wiping real saved values.
    """
    result = base.copy()
    for key, val in incoming.items():
        if key in PROTECTED_FIELDS:
            incoming_str = str(val or "").strip()
            if incoming_str and incoming_str.lower() != "nan":
                result[key] = incoming_str
            # else: keep whatever is already in result (from base)
        else:
            result[key] = val
    return result


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


def update_email_body_greeting(body_text: str, recruiter_name: str) -> str:
    greeting = f"Hi {recruiter_name or 'Team'},"
    body = str(body_text or "")
    if re.match(r"^Hi\s+.*?,", body):
        return re.sub(r"^Hi\s+.*?,", greeting, body, count=1)
    return f"{greeting}\n\n{body}" if body else greeting


def get_jr_master_recruiter_email(master_row: dict) -> str:
    return str(
        master_row.get("client_recruiter_email", "") or master_row.get("recruiter_email", "")
    ).strip()


def sync_draft_recruiter_fields(selected_idx: int, recruiter_email_by_name: dict) -> None:
    recruiter_name = str(st.session_state.get(f"draft_recruiter_name_{selected_idx}", "")).strip()
    recruiter_email = str(recruiter_email_by_name.get(recruiter_name, "")).strip()
    if recruiter_email:
        st.session_state[f"draft_email_to_{selected_idx}"] = recruiter_email


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
    seen_keys = set()

    for row in successful_rows:
        first_name = str(row.get("First Name", "")).strip()
        last_name = str(row.get("Last Name", "")).strip()
        jr = str(row.get("JR Number", "")).strip()
        email_id = str(row.get("Email", "")).strip()
        contact_number = str(row.get("Phone", "")).strip()
        meta = metadata_by_jr.get(jr) or {}

        primary_key = (jr, email_id, contact_number)
        if primary_key in seen_keys:
            continue
        seen_keys.add(primary_key)

        candidate_rows.append(
            {
                "JR Number": jr,
                "Email ID": email_id,
                "Contact Number": contact_number,
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
    if str(row.get("Upload to SAP", "")).strip() == "Pending":
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
    unique_keys = {}

    for f_name, record_id in st.session_state.resume_record_ids.items():
        row_data = st.session_state.parsed_resume_rows.get(f_name, {})
        key = (
            str(row_data.get("JR Number", "")).strip(),
            str(row_data.get("Email", "")).strip(),
            str(row_data.get("Phone", "")).strip()
        )
        if key[0] and (key[1] or key[2]):
            unique_keys[key] = record_id

    for _, row in edited_df.iterrows():
        row_dict = row.to_dict()
        file_name = str(row_dict.get("File Name", "")).strip()
        if not file_name:
            continue

        jr_number = str(row_dict.get("JR Number", "")).strip()
        if not jr_number:
            st.warning(f"Skipping DB save for {file_name}: JR Number cannot be empty")
            continue

        current_key = (
            jr_number,
            str(row_dict.get("Email", "")).strip(),
            str(row_dict.get("Phone", "")).strip()
        )

        existing_id = unique_keys.get(current_key)
        record_id = st.session_state.resume_record_ids.get(file_name)

        if existing_id and not record_id:
            st.warning(f"Skipping duplicate record for {file_name} (already exists in DB/session)")
            continue

        if not record_id:
            if existing_id:
                st.warning(f"Record for {file_name} matches an existing record. Linking to existing ID.")
                st.session_state.resume_record_ids[file_name] = existing_id
                record_id = existing_id
            else:
                unique_keys[current_key] = "PENDING"

        jr_folder = jr_folder_name(jr_number)
        current_link = st.session_state.resume_paths.get(file_name, "")

        if not current_link.startswith(f"{jr_folder}/"):
            file_bytes = st.session_state.uploaded_files_store.get(file_name)

            if file_bytes:
                old_path = current_link

                resume_path = upload_resume(file_name, file_bytes, jr_number)

                st.session_state.resume_paths[file_name] = resume_path

                # 🔥 delete old pending file
                if old_path:
                    delete_resume(old_path)

        # Merge protected fields from session state before computing snapshot/saving.
        # edited_df never carries these columns so they arrive as NaN in row_dict.
        full_existing = st.session_state.parsed_resume_rows.get(file_name, {})
        merged_row_dict = _safe_merge(full_existing, row_dict)

        # If JR Number has changed from what was last committed to DB,
        # treat it as a brand-new record: detach from the old DB row and
        # reset status flags so it gets inserted fresh.
        if record_id and record_id != "PENDING":
            committed_jr = str(st.session_state.resume_committed_jr.get(file_name, "")).strip()
            new_jr = str(row_dict.get("JR Number", "")).strip()
            if new_jr and committed_jr and new_jr != committed_jr:
                # JR number changed from committed value — always treat as a new record
                del st.session_state.resume_record_ids[file_name]
                st.session_state.resume_row_snapshots.pop(file_name, None)
                st.session_state.resume_committed_jr.pop(file_name, None)
                merged_row_dict["upload_to_sap"] = "Pending"
                merged_row_dict["client_email_sent"] = "Pending"
                record_id = None

        snapshot = _row_snapshot(merged_row_dict)

        if record_id and record_id != "PENDING":
            if st.session_state.resume_row_snapshots.get(file_name) != snapshot:
                update_resume_record(record_id, merged_row_dict, user, resume_path=current_link)
                st.session_state.resume_row_snapshots[file_name] = snapshot
                st.session_state.resume_committed_jr[file_name] = str(merged_row_dict.get("JR Number", "")).strip()
        else:
            try:
                merged_row_dict.setdefault("client_email_sent", "Pending")
                if str(merged_row_dict.get("client_email_sent", "Pending")).strip() not in ("Sent", "Pending"):
                    merged_row_dict["client_email_sent"] = "Pending"
                record = insert_resume_record(merged_row_dict, user, resume_path=current_link)
                new_id = str(record.get("id", "")).strip()
                st.session_state.resume_record_ids[file_name] = new_id
                st.session_state.resume_row_snapshots[file_name] = snapshot
                st.session_state.resume_committed_jr[file_name] = str(merged_row_dict.get("JR Number", "")).strip()
                unique_keys[current_key] = new_id
            except Exception as e:
                st.error(f"Failed to insert record for {file_name}: {e}")

        st.session_state.parsed_resume_rows[file_name] = merged_row_dict


def _candidate_display_name(row: pd.Series) -> str:
    return " ".join(
        part for part in [str(row.get("First Name", "")).strip(), str(row.get("Last Name", "")).strip()] if part
    ).strip()


st.set_page_config(page_title="Candidate Submission ATS", page_icon="📋", layout="wide")

# Keep-alive: prevents Streamlit Cloud sleep mode
from streamlit.components.v1 import html as _html
_html("""
<script>
  setInterval(() => {
    fetch(window.location.href, { method: 'HEAD' }).catch(() => {});
  }, 240000);
</script>
""", height=0)
# Hide Streamlit's auto-generated multi-page navigation in the sidebar
st.markdown(
    """
    <style>
    [data-testid="stSidebarNav"] { display: none !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# =========================
# AUTH
# =========================
user = require_login()
show_user_profile(user)
show_navigation("new_records")

st.title("Candidate Submission ATS")

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

jr_display_options = [
    f"{jr} - {jr_master_by_number[jr].get('skill_name', '')}"
    if jr_master_by_number[jr].get("skill_name")
    else jr
    for jr in active_jr_numbers
]
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
recruiter_email_by_name = {}
for row in jr_master_rows:
    recruiter_name = str(row.get("client_recruiter", "")).strip()
    recruiter_email = get_jr_master_recruiter_email(row)
    if recruiter_name and recruiter_email and recruiter_name not in recruiter_email_by_name:
        recruiter_email_by_name[recruiter_name] = recruiter_email

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
if "resume_paths" not in st.session_state:
    st.session_state.resume_paths = {}
if "resume_committed_jr" not in st.session_state:
    st.session_state.resume_committed_jr = {}
if "db_resume_records" not in st.session_state:
    st.session_state.db_resume_records = []

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
_sf1, _sf2, _sf3 = st.columns([1, 1, 2])
with _sf1:
    _stats_date_from = st.date_input("Date From", value=None, key="stats_date_from",
        help="Filters stats cards and the DB table below.")
with _sf2:
    _stats_date_to = st.date_input("Date To", value=None, key="stats_date_to")
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
    if _stats_date_from is not None and rd < _stats_date_from:
        return False
    if _stats_date_to is not None and rd > _stats_date_to:
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
_pending    = sum(1 for r in _filtered_stats_records if str(r.get("upload_to_sap", "")).strip() not in ("Done", "Pending"))
_email_sent = sum(1 for r in _filtered_stats_records if str(r.get("client_email_sent", "Pending")).strip() == "Sent")

_today_str     = _today.strftime("%d-%b-%Y")
# Today always shows today's data for the selected recruiter, ignoring the date range.
_today_records = [
    r for r in _all_db_records
    if str(r.get("date_text", "")).strip() == _today_str
    and (
        _stats_recruiter == "All Recruiters"
        or _stats_recruiter in (
            str(r.get("recruiter", "") or "").strip(),
            str(r.get("recruiter_email", "") or "").strip(),
        )
    )
]
_today_total      = len(_today_records)
_today_uploaded   = sum(1 for r in _today_records if str(r.get("upload_to_sap", "")).strip() == "Done")
_today_pending    = sum(1 for r in _today_records if str(r.get("upload_to_sap", "")).strip() not in ("Done", "Pending"))
_today_email_sent = sum(1 for r in _today_records if str(r.get("client_email_sent", "Pending")).strip() == "Sent")


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

# =========================
# FILE UPLOAD & PARSE
# =========================
_up_col, _parse_col = st.columns([3, 1])
with _up_col:
    files = st.file_uploader(
        "Upload Resumes",
        type=["pdf", "docx"],
        accept_multiple_files=True,
        help="Each resume must have a unique filename. Duplicates will be ignored.",
    )
with _parse_col:
    st.write("")  # vertical alignment spacer
    st.write("")
    if st.button("🔄 Parse Resumes", width="stretch",
                 help="Manually re-trigger parsing for any uploaded files that are missing data."):
        _reparsed = 0
        _today_text = date.today().strftime("%d-%b-%Y")
        for _fname, _fbytes in list(st.session_state.uploaded_files_store.items()):
            _existing = st.session_state.parsed_resume_rows.get(_fname, {})
            _missing = not str(_existing.get("First Name", "")).strip() and not str(_existing.get("Email", "")).strip()
            if _missing:
                try:
                    import io
                    _fobj = io.BytesIO(_fbytes)
                    _fobj.name = _fname
                    _data = parse_resume(_fobj)
                    _existing["First Name"] = _data.get("first_name", "") or _existing.get("First Name", "")
                    _existing["Last Name"]  = _data.get("last_name", "")  or _existing.get("Last Name", "")
                    _existing["Email"]      = _data.get("email", "")      or _existing.get("Email", "")
                    _existing["Phone"]      = _data.get("phone", "")      or _existing.get("Phone", "")
                    _existing["Error"] = ""
                    st.session_state.parsed_resume_rows[_fname] = _existing
                    st.session_state.resume_row_snapshots[_fname] = _row_snapshot(_existing)
                    _reparsed += 1
                except Exception as _pe:
                    st.session_state.parsed_resume_rows[_fname]["Error"] = str(_pe)
        if _reparsed:
            st.success(f"Re-parsed {_reparsed} file(s) successfully.")
            st.rerun()
        else:
            st.info("All uploaded files already have parsed data.")

if files:
    seen = set()
    unique_files = []

    for file in files:
        file_name = file.name
        if file_name in seen:
            st.warning(f"Duplicate file skipped: **{file_name}**")
        else:
            seen.add(file_name)
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
        file_name = file.name
        file.seek(0)
        file_bytes = file.read()
        st.session_state.uploaded_files_store[file.name] = file_bytes

        if file_name not in st.session_state.parsed_resume_rows:
            row = {
                "JR Number": "",
                "Date": today_text,
                "Skill": "",
                "File Name": file_name,
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
                "Upload to SAP": "Pending",
                "client_recruiter": "",
                "client_recruiter_email": "",
                "client_email_sent": "Pending",
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

                jr_number = str(row.get("JR Number", "")).strip()
                if jr_number in jr_master_by_number:
                    master_row = jr_master_by_number[jr_number]
                    if not str(row.get("Skill", "")).strip():
                        row["Skill"] = str(master_row.get("skill_name", "")).strip()
                    if not str(row.get("client_recruiter", "")).strip():
                        row["client_recruiter"] = str(master_row.get("client_recruiter", "")).strip()
                    if not str(row.get("client_recruiter_email", "")).strip():
                        row["client_recruiter_email"] = get_jr_master_recruiter_email(master_row)
            except Exception as error:
                row["Error"] = str(error)

            try:
                jr_number = str(row.get("JR Number", "")).strip()

                resume_path = upload_resume(file_name, file_bytes, jr_number)

                st.session_state.resume_paths[file_name] = resume_path
            except Exception as error:
                _od_err = str(error).strip()
                row["Error"] = f"{row['Error']} | {_od_err}".strip(" |")
                st.warning(f"Supabase upload failed for **{file_name}**: {_od_err}")
            st.session_state.resume_row_snapshots[file_name] = _row_snapshot(row)
            st.session_state.parsed_resume_rows[file_name] = row
            st.session_state.resume_committed_jr[file_name] = ""

        progress.progress((index + 1) / len(files))

    if _new_files_to_process:
        st.rerun()

# Collect all records for the main table from session state
results = [dict(row_data) for row_data in st.session_state.parsed_resume_rows.values()]

# =========================
# VALIDATION & TABLE
# =========================
if results:
    df = pd.DataFrame(results)
else:
    df = pd.DataFrame(columns=[
        "JR Number", "Date", "Skill", "First Name", "Last Name", "Email", "Phone",
        "Current Company", "Total Experience", "Relevant Experience", "Current CTC",
        "Expected CTC", "Notice Period", "Current Location", "Preferred Location",
        "Actual Status", "Call Iteration", "comments/Availability", "Error", "Upload to SAP", "File Name"
    ])

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
        "JR Number", "Date", "Skill", "First Name", "Last Name", "Email", "Phone",
        "Current Company", "Total Experience", "Relevant Experience", "Current CTC",
        "Expected CTC", "Notice Period", "Current Location", "Preferred Location",
        "Actual Status", "Call Iteration", "comments/Availability", "Error", "Upload to SAP", "File Name",
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
    # NOTE: client_email_sent is intentionally NOT renamed here so it stays
    # accessible under its original key when added to parsed_resume_rows.
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
        "resume_path": "resume_path",
        "client_recruiter": "client_recruiter",
        "client_recruiter_email": "client_recruiter_email",
        "recruiter": "recruiter",
        "recruiter_email": "recruiter_email",
        # client_email_sent is intentionally kept as-is (not renamed to "Email Sent")
        # so _safe_merge can protect it correctly when records are added to parsed_resume_rows.
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
        display_cols = [
            "JR Number", "Date", "Skill", "First Name", "Last Name", "Email", "Phone",
            "Current Company", "Total Experience", "Relevant Experience", "Current CTC",
            "Expected CTC", "Notice Period", "Current Location", "Preferred Location",
            "Actual Status", "Call Iteration", "comments/Availability", "Error", "Upload to SAP", "File Name"
        ]

        filtered_db_df["Select"] = False
        avail_cols = [c for c in display_cols if c in filtered_db_df.columns]
        select_cols = ["Select"] + avail_cols

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
                    record_db_id = str(row.get("id", "")).strip()
                    if not file_name:
                        file_name = f"db_record_{record_db_id or 'unknown'}"

                    # If this file_name is already in session but belongs to a
                    # different DB record (same resume, different JR), make the
                    # key unique so it doesn't overwrite the existing entry.
                    existing_id_for_file = st.session_state.resume_record_ids.get(file_name, "")
                    if existing_id_for_file and record_db_id and existing_id_for_file != record_db_id:
                        _jr_suffix = str(row.get("JR Number", "") or "").strip().replace(" ", "_") or record_db_id
                        base, _, ext = file_name.rpartition(".")
                        file_name = f"{base}_{_jr_suffix}.{ext}" if ext else f"{file_name}_{_jr_suffix}"

                    original_record = None
                    if 'id' in row:
                        original_record = next(
                            (r for r in st.session_state.db_resume_records if str(r.get("id")) == str(row.get("id"))),
                            {})

                    if original_record:
                        st.session_state.resume_record_ids[file_name] = str(original_record.get("id", ""))
                        _stored_link = str(original_record.get("resume_path", "") or "").strip()
                        if _stored_link:
                            st.session_state.resume_paths[file_name] = _stored_link

                    row_data = row.to_dict()
                    row_data.pop("Select", None)
                    row_data["File Name"] = file_name

                    # Pull protected fields directly from the original DB record
                    # (which has the raw unrenamed keys) so nothing gets lost.
                    if original_record:
                        for field in PROTECTED_FIELDS:
                            raw_val = str(original_record.get(field, "") or "").strip()
                            if raw_val and raw_val.lower() != "nan":
                                row_data[field] = raw_val

                    st.session_state.parsed_resume_rows[file_name] = row_data
                    st.session_state.resume_row_snapshots[file_name] = _row_snapshot(row_data)
                    st.session_state.resume_committed_jr[file_name] = str(row_data.get("JR Number", "") or row_data.get("jr_number", "") or "").strip()
                    _rl = str(row_data.get("resume_path", "") or "").strip()
                    if _rl and file_name not in st.session_state.resume_paths:
                        st.session_state.resume_paths[file_name] = _rl

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
            "Date": st.column_config.Column(pinned=True),
            "Skill": st.column_config.TextColumn("Skill", width="small", pinned=True),
            "First Name": st.column_config.Column(pinned=True),
            "Actual Status": st.column_config.SelectboxColumn(
                "Actual Status",
                options=[
                    "Not Called", "Called", "No Answer", "Interested", "Not Interested",
                    "Wrong Number", "Call Back Later", "Interview Scheduled",
                ],
            ),
            "Upload to SAP": st.column_config.SelectboxColumn(
                "Upload to SAP",
                options=["Pending", "Done", "Failed"],
            ),
            "Call Iteration": st.column_config.SelectboxColumn(
                "Call Iteration",
                options=["First Call", "Recall Once", "Recall Twice", "Recall Thrice"],
            ),
        },
        key="resume_editor",
    )
    save_table_changes = st.form_submit_button("Save Table Changes", use_container_width=True)

if save_table_changes:
    editor_df = editor_df.dropna(how="all")
    editor_df = editor_df[
        ~(editor_df[["First Name", "Last Name", "Email", "Phone"]].fillna("").apply(lambda x: x.str.strip()).eq("").all(axis=1))
    ]

    for _, row in editor_df.iterrows():
        file_name = str(row.get("File Name", "")).strip()
        if not file_name:
            continue

        # Use _safe_merge so protected fields in session state are never wiped
        current_data = st.session_state.parsed_resume_rows.get(file_name, {}).copy()
        current_data = _safe_merge(current_data, row.to_dict())

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
                current_data["client_recruiter_email"] = get_jr_master_recruiter_email(master_row)

        st.session_state.parsed_resume_rows[file_name] = current_data
    st.rerun()

all_rows_df = pd.DataFrame(list(st.session_state.parsed_resume_rows.values()))
all_rows_df.index = all_rows_df.index + 1
all_rows_df = all_rows_df.reindex(columns=df.columns)
edited_df = all_rows_df.dropna(how="all")
edited_df = edited_df[
    ~(edited_df[["First Name", "Last Name", "Email", "Phone"]].fillna("").apply(lambda x: x.str.strip()).eq("").all(axis=1))
]

def _strip_jr_label(val):
    raw = str(val or "").strip()
    return _jr_display_to_no.get(raw, raw.split(" - ")[0].strip() if " - " in raw else raw)

if "JR Number" in edited_df.columns:
    edited_df = edited_df.copy()
    edited_df["JR Number"] = edited_df["JR Number"].apply(_strip_jr_label)

if edited_df.empty:
    st.warning("No valid data to upload")
    st.stop()

missing_jr = edited_df[edited_df["JR Number"].fillna("").str.strip() == ""]
if not missing_jr.empty:
    st.error(f"{len(missing_jr)} row(s) are missing JR Number - fill them before continuing")
    st.stop()

try:
    _sync_resume_rows_to_db(edited_df, user)
except Exception as error:
    st.error(f"Supabase sync failed: {error}")

# =========================
# DOWNLOAD CSV
# =========================
csv = edited_df.to_csv(index=False).encode("utf-8")
_dl_col, _cl_col = st.columns([1, 1])
with _dl_col:
    st.download_button("Download CSV", data=csv, file_name="parsed_resumes.csv", mime="text/csv", width="stretch")
with _cl_col:
    if st.button("🗑️ Clear Table", width="stretch", help="Remove all candidates from the current session table."):
        st.session_state.parsed_resume_rows = {}
        st.session_state.resume_record_ids = {}
        st.session_state.resume_row_snapshots = {}
        st.session_state.resume_paths = {}
        st.session_state.resume_committed_jr = {}
        st.session_state.uploaded_files_store = {}
        clear_pending_upload_state()
        st.session_state.email_drafts_df = pd.DataFrame()
        st.session_state.email_candidates_df = pd.DataFrame()
        st.session_state.email_send_status = ""
        st.rerun()

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
    try:
        _sync_resume_rows_to_db(edited_df, user)
    except Exception as error:
        st.error(f"Sync to database failed before upload: {error}")
        st.stop()

    reset_email_state()
    upload_rows = edited_df[
        (edited_df["Upload to SAP"].fillna("").str.strip() == "Pending")
    ]

    if upload_rows.empty:
        st.error("No rows selected for SAP upload")
    else:
        required_sap_fields = ["JR Number", "Email", "Phone", "First Name", "Last Name"]
        invalid_upload_rows = upload_rows[
            upload_rows[required_sap_fields].fillna("").apply(lambda x: x.astype(str).str.strip()).eq("").any(axis=1)
        ]
        if not invalid_upload_rows.empty:
            invalid_names = ", ".join(
                str(row.get("File Name", "")).strip() or f"row {idx + 1}"
                for idx, row in invalid_upload_rows.iterrows()
            )
            st.error(
                "SAP upload requires JR Number, Email, Phone, First Name, and Last Name. "
                f"Missing values found in: {invalid_names}"
            )
            st.stop()
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
        status_box.info("Downloading resume files...")
        for _, _pre_row in upload_rows.iterrows():
            _pre_fname = str(_pre_row.get("File Name", "")).strip()

            if not _pre_fname:
                continue

            if st.session_state.uploaded_files_store.get(_pre_fname):
                continue

            resume_path = st.session_state.resume_paths.get(_pre_fname)

            if not resume_path:
                continue

            try:
                file_bytes = download_resume(resume_path)
                st.session_state.uploaded_files_store[_pre_fname] = file_bytes
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
                    resume_path = st.session_state.resume_paths.get(file_name)

                    if not resume_path:
                        raise Exception("Resume path not found")

                    file_bytes = download_resume(resume_path)
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

                # Apply SAP metadata only if fields were previously empty
                if jr_number in metadata_by_jr:
                    meta = metadata_by_jr[jr_number]
                    if not str(row.get("Skill", "")).strip():
                        row["Skill"] = str(meta.get("job_title", "")).strip()
                    if not str(row.get("client_recruiter", "")).strip():
                        row["client_recruiter"] = str(meta.get("client_recruiter", "")).strip()
                    if not str(row.get("client_recruiter_email", "")).strip():
                        row["client_recruiter_email"] = str(
                            meta.get("email_to", "") or get_jr_master_recruiter_email(jr_master_by_number.get(jr_number, {}))
                        ).strip()

                row["Upload to SAP"] = "Done"
                file_name = str(row.get("File Name", "")).strip()
                if file_name:
                    # _safe_merge ensures protected fields from session state
                    # are never overwritten by NaN values from the upload row.
                    existing = st.session_state.parsed_resume_rows.get(file_name, {}).copy()
                    updated_row = _safe_merge(existing, row.to_dict())
                    st.session_state.parsed_resume_rows[file_name] = updated_row
                    st.session_state.resume_row_snapshots[file_name] = _row_snapshot(updated_row)
                    record_id = st.session_state.resume_record_ids.get(file_name)
                    if record_id:
                        update_resume_record(
                            record_id,
                            updated_row,
                            user,
                            resume_path=st.session_state.resume_paths.get(file_name, ""),
                        )
                results_log.append({"File": row["File Name"], "Status": "Success"})
                successful_rows.append(row.to_dict())

            except Exception as error:
                if bot:
                    try:
                        row["Upload to SAP"] = "Failed"
                        file_name = str(row.get("File Name", "")).strip()
                        if file_name:
                            existing = st.session_state.parsed_resume_rows.get(file_name, {}).copy()
                            # Use _safe_merge here too so failure path doesn't wipe fields
                            updated_row = _safe_merge(existing, row.to_dict())
                            st.session_state.parsed_resume_rows[file_name] = updated_row
                            st.session_state.resume_row_snapshots[file_name] = _row_snapshot(updated_row)
                            record_id = st.session_state.resume_record_ids.get(file_name)
                            if record_id:
                                update_resume_record(
                                    record_id,
                                    updated_row,
                                    user,
                                    resume_path=st.session_state.resume_paths.get(file_name, ""),
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
                if bot.driver:
                    bot.close()
            except Exception:
                pass
        status_box.empty()

    st.session_state.email_drafts_df = pd.DataFrame()
    st.session_state.email_candidates_df = pd.DataFrame()

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
            cc=EMAIL_CC,

        )

    if ok:
        st.info(f"Upload report sent to **{user['email']}**")
    else:
        st.warning(msg)

    clear_pending_upload_state()

st.session_state.last_email_draft_signature = ""
