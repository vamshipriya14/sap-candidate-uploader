import io
import re
from datetime import date

import pandas as pd
import streamlit as st

from auth import require_login, show_user_profile
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
        # job_title: prefer skill_name (jr_master), fallback to job_title (SAP metadata)
        job_title = str(meta.get("skill_name") or meta.get("job_title") or "").strip()
        recruiter_name = str(meta.get("client_recruiter") or "").strip()
        candidate_names = ", ".join(
            str(row.get("Candidate Name", "")).strip()
            or " ".join(p for p in [str(row.get("First Name", "")).strip(), str(row.get("Last Name", "")).strip()] if p)
            for row in rows
        )

        # Email To: read from candidate resumes table (stored per-candidate in DB),
        # fallback to email_to from SAP bot metadata (set during SAP upload flow).
        email_to = (
                str(rows[0].get("client_recruiter_email") or "").strip()
                or str(meta.get("email_to") or "").strip()
        )

        drafts.append(
            {
                "JR Number": jr,
                "Job Title": job_title,
                "Candidate Names": candidate_names,
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
            "Candidate Names",
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


st.set_page_config(page_title="Resume -> SAP Upload", layout="wide")

# =========================
# AUTH
# =========================
user = require_login()
show_user_profile(user)

st.title("Resume -> SAP Upload")
st.caption(f"Logged in as **{user['name']}** ({user['email']})")


# =========================
# USER SIGNATURE
# =========================
# FIXED: _get_default_signature_template
# Changes:
#   1. Logo: black background removed, correct Volibits branding (orange V + navy text), transparent PNG
#   2. Social icons: pure CSS <a> buttons — no SVG/external images, works in OWA + all clients

def _get_default_signature_template(user_dict: dict) -> str:
    name = user_dict.get("name", "Name")
    job_title = user_dict.get("job_title") or "job_title"
    email = user_dict.get("email", "Email")
    phone = user_dict.get("phone") or "+91 0000000000"

    # Correct Volibits logo: transparent PNG (black background removed), resized to 280x89px.
    # data:image/png works in OWA. SVG data URIs are blocked by Outlook Web.
    _logo = "data:image/png;base64,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"

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

with st.expander("📝 Manage Your Email Signature"):
    import streamlit.components.v1 as components

    # Initialise editable signature fields from user profile / DB
    if "sig_name" not in st.session_state:
        st.session_state.sig_name = pretty_user_name(user)
    if "sig_job_title" not in st.session_state:
        st.session_state.sig_job_title = user.get("job_title") or ""
    if "sig_phone" not in st.session_state:
        st.session_state.sig_phone = user.get("phone") or ""

    st.caption("Fill in your details below. The signature preview updates automatically.")

    form_col, preview_col = st.columns([1, 1], gap="large")

    with form_col:
        st.markdown("**Your Details**")
        sig_name = st.text_input("Full Name", value=st.session_state.sig_name, key="sig_name_input")
        sig_job_title = st.text_input(
            "Job Title", value=st.session_state.sig_job_title,
            placeholder="e.g. Senior Recruiter",
            key="sig_job_title_input",
        )
        sig_phone = st.text_input(
            "Phone Number", value=st.session_state.sig_phone,
            placeholder="e.g. +91 0000000000",
            key="sig_phone_input",
        )

        _user_for_sig = {
            **user,
            "name": sig_name or pretty_user_name(user),
            "job_title": sig_job_title,
            "phone": sig_phone,
        }
        preview_html = _get_default_signature_template(_user_for_sig)

        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
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
        with btn_col2:
            if st.button("Reset Fields", use_container_width=True,
                         help="Reset fields to your profile defaults"):
                st.session_state.sig_name = pretty_user_name(user)
                st.session_state.sig_job_title = user.get("job_title") or ""
                st.session_state.sig_phone = user.get("phone") or ""
                st.rerun()

    with preview_col:
        st.markdown("**Signature Preview**")
        components.html(
            f"""<div style="font-family:Arial,sans-serif; padding:4px;">
                <p style="font-size:12px; color:#888; margin:0 0 8px 0;">— Regards,</p>
                {preview_html}
            </div>""",
            height=165,
            scrolling=False,
        )


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
    if st.session_state.last_uploaded_signature != current_signature:
        # Clear only the newly uploaded file state, not the imported DB records
        # To distinguish, we'd need to track which ones are from files.
        # But the user might want to clear everything when new files are uploaded.
        # For now, let's keep it simple and only process new files.
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
                row["Error"] = f"{row['Error']} | {error}".strip(" |")

            st.session_state.resume_row_snapshots[file.name] = _row_snapshot(row)
            st.session_state.parsed_resume_rows[file.name] = row

        progress.progress((index + 1) / len(files))
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
    columns=["Candidate Name", "JR Number", "Actual Status", "Call Iteration", "Upload to SAP", "Email Sent"])

f1, f2, f3, f4, f5, f6 = st.columns(6)
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
with f6:
    email_sent_filter = st.multiselect(
        "Email Sent",
        options=sorted(
            value for value in filter_source_df["Email Sent"].fillna("No").astype(str).str.strip().unique() if
            value) if not filter_source_df.empty else ["No", "Yes"],
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
if email_sent_filter:
    filtered_db_df = filtered_db_df[
        filtered_db_df["Email Sent"].fillna("No").astype(str).str.strip().isin(email_sent_filter)]

with st.expander("Searchable Database Records - Add to Main Table", expanded=False):
    if filtered_db_df.empty:
        st.info("No records match the filters")
    else:
        # Reorder columns to match main table
        display_cols = [
            "JR Number", "Date", "Skill", "First Name", "Last Name", "Email", "Phone",
            "Current Company", "Total Experience", "Relevant Experience", "Current CTC",
            "Expected CTC", "Notice Period", "Current Location", "Preferred Location",
            "Actual Status", "Call Iteration", "comments/Availability", "Error", "Upload to SAP", "Email Sent",
            "File Name"
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
                        st.session_state.resume_links[file_name] = original_record.get("resume_link", "")

                    row_data = row.to_dict()
                    row_data.pop("Select", None)
                    row_data["File Name"] = file_name

                    # Ensure it's in parsed_resume_rows so main table shows it
                    st.session_state.parsed_resume_rows[file_name] = row_data
                    # Snapshot it so it doesn't trigger immediate sync unless changed
                    st.session_state.resume_row_snapshots[file_name] = _row_snapshot(row_data)

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
                options=active_jr_numbers,
                help="Select JR Number from active list",
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

        jr_number = str(current_data.get("JR Number", "")).strip()
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

                file_bytes = st.session_state.uploaded_files_store.get(row["File Name"])
                if not file_bytes:
                    # If file bytes not in store, it might be an added record from DB.
                    # We try to fetch from resume_link if available.
                    resume_link = st.session_state.resume_links.get(row["File Name"])
                    if resume_link:
                        import requests

                        # We need access token if it's a Microsoft Graph link
                        headers = {}
                        if "sharepoint.com" in resume_link or "graph.microsoft.com" in resume_link:
                            import base64

                            share_token = f"u!{base64.urlsafe_b64encode(resume_link.encode('utf-8')).decode('utf-8').rstrip('=')}"
                            graph_url = f"https://graph.microsoft.com/v1.0/shares/{share_token}/driveItem/content"
                            headers["Authorization"] = f"Bearer {user['access_token']}"
                            resp = requests.get(graph_url, headers=headers, timeout=30)
                        else:
                            resp = requests.get(resume_link, headers=headers, timeout=30)

                        if resp.status_code == 200:
                            file_bytes = resp.content
                            st.session_state.uploaded_files_store[row["File Name"]] = file_bytes
                        else:
                            raise Exception(
                                f"Failed to download resume from link: {resume_link} (Status {resp.status_code})")
                    else:
                        raise Exception("File bytes not found in session and no resume link available")

                file_obj = io.BytesIO(file_bytes)
                file_obj.name = row["File Name"]

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
                    updated_row = row.to_dict()
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
                            updated_row = row.to_dict()
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

# ── Generate drafts for unsent DB candidates ───────────────────────────────
unsent_db_records = [
    r for r in st.session_state.db_resume_records
    if str(r.get("client_email_sent", "No")).strip() not in ("Yes", "yes", "1", "true")
       and str(r.get("upload_to_sap", "")).strip() == "Done"
]
if unsent_db_records:
    st.subheader("Send Emails for Unsent Candidates")


    def _unsent_display_label(r):
        jr = str(r.get("jr_number", "")).strip()
        first = str(r.get("first_name", "")).strip()
        last = str(r.get("last_name", "")).strip()
        candidate = " ".join(p for p in [first, last] if p)
        meta = jr_master_by_number.get(jr, {})
        skill = str(meta.get("skill_name") or r.get("skill") or "").strip()
        label = f"{jr}"
        if skill:
            label += f" - {skill}"
        if candidate:
            label += f" - {candidate}"
        return label


    unsent_labels = [_unsent_display_label(r) for r in unsent_db_records]
    label_to_record = dict(zip(unsent_labels, unsent_db_records))

    selected_unsent_labels = st.multiselect(
        "Select candidates to generate email drafts",
        options=sorted(set(unsent_labels)),
        help="Only candidates with Upload to SAP = Done and Email Sent = No are shown.",
    )

    if st.button("Generate Drafts for Selected Candidates"):
        selected_records = [label_to_record[lbl] for lbl in selected_unsent_labels if lbl in label_to_record]
        if not selected_records:
            st.warning("No candidates selected.")
        else:
            prep_rows = []
            for r in selected_records:
                first = str(r.get("first_name", "")).strip()
                last = str(r.get("last_name", "")).strip()
                prep_rows.append({
                    "JR Number": str(r.get("jr_number", "")).strip(),
                    "First Name": first,
                    "Last Name": last,
                    "Candidate Name": " ".join(p for p in [first, last] if p),
                    "File Name": str(r.get("file_name", "")).strip(),
                    "Email": str(r.get("email", "")).strip(),
                    "Phone": str(r.get("phone", "")).strip(),
                    "Skill": str(r.get("skill", "")).strip(),
                    "client_recruiter": str(r.get("client_recruiter", "")).strip(),
                    "client_recruiter_email": str(r.get("client_recruiter_email") or "").strip(),
                    "Current Company": str(r.get("current_company", "")).strip(),
                    "Total Experience": str(r.get("total_experience", "")).strip(),
                    "Relevant Experience": str(r.get("relevant_experience", "")).strip(),
                    "Current CTC": str(r.get("current_ctc", "")).strip(),
                    "Expected CTC": str(r.get("expected_ctc", "")).strip(),
                    "Notice Period": str(r.get("notice_period", "")).strip(),
                    "Current Location": str(r.get("current_location", "")).strip(),
                    "Preferred Location": str(r.get("preferred_location", "")).strip(),
                    "comments/Availability": str(r.get("comments_availability", "")).strip(),
                })
            st.session_state.email_drafts_df = build_email_drafts(prep_rows, jr_master_by_number, user)
            st.session_state.email_candidates_df = build_candidate_details_table(prep_rows, jr_master_by_number)
            st.rerun()

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
        if ok:
            # Mark each candidate in this draft as email sent
            jr_filter = str(draft_row.get("JR Number", "")).strip()
            for db_record in st.session_state.db_resume_records:
                if str(db_record.get("jr_number", "")).strip() == jr_filter:
                    record_id = str(db_record.get("id", "")).strip()
                    if record_id:
                        try:
                            update_payload = dict(db_record)
                            update_payload["client_email_sent"] = "Yes"
                            update_resume_record(record_id, update_payload, user)
                        except Exception as e:
                            st.warning(f"Could not mark email sent for record {record_id}: {e}")
            # Refresh DB records so the filter reflects the new state
            try:
                st.session_state.db_resume_records = fetch_all_resume_records()
            except Exception:
                pass
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