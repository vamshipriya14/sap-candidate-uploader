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
    insert_resume_record,
    jr_folder_name,
    update_resume_record,
    upload_resume_to_shared_drive,
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
        f"Regards,\n"
        f"{sender_name}"
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
        recruiter_name = meta.get("client_recruiter", "")
        drafts.append(
            {
                "JR Number": jr,
                "Job Title": job_title,
                "Client Recruiter Name": recruiter_name,
                "Email To": meta.get("email_to", ""),
                "CC": "rec_team@volibits.com",
                "Email From": sender_email,
                "Subject": f"BS:{job_title}" if job_title else "BS:",
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
    unique_keys = {} # key -> record_id (if exists in DB)
    
    # First, collect keys of already known records in the session
    for f_name, record_id in st.session_state.resume_record_ids.items():
        row_data = st.session_state.parsed_resume_rows.get(f_name, {})
        key = (
            str(row_data.get("JR Number", "")).strip(),
            str(row_data.get("Email", "")).strip(),
            str(row_data.get("Phone", "")).strip()
        )
        if key[1] or key[2]: # Only if Email or Phone is non-empty
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
                resume_link = upload_resume_to_shared_drive(user["access_token"], file_name, file_bytes, subfolder=jr_folder)
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
    })

filter_source_df = db_df.copy() if not db_df.empty else pd.DataFrame(columns=["Candidate Name", "JR Number", "Actual Status", "Call Iteration", "Upload to SAP"])

f1, f2, f3, f4, f5 = st.columns(5)
with f1:
    candidate_filter = st.multiselect(
        "Candidate Name (from DB)",
        options=sorted(name for name in filter_source_df["Candidate Name"].unique() if name) if not filter_source_df.empty else [],
    )
with f2:
    jr_filter_values = st.multiselect(
        "JR Number (from DB)",
        options=sorted(value for value in filter_source_df["JR Number"].fillna("").astype(str).str.strip().unique() if value) if not filter_source_df.empty else [],
    )
with f3:
    actual_status_filter = st.multiselect(
        "Call Status (from DB)",
        options=sorted(value for value in filter_source_df["Actual Status"].fillna("").astype(str).str.strip().unique() if value) if not filter_source_df.empty else [],
    )
with f4:
    call_iteration_filter = st.multiselect(
        "Call Iteration (from DB)",
        options=sorted(value for value in filter_source_df["Call Iteration"].fillna("").astype(str).str.strip().unique() if value) if not filter_source_df.empty else [],
    )
with f5:
    upload_filter = st.multiselect(
        "Upload to SAP (from DB)",
        options=sorted(value for value in filter_source_df["Upload to SAP"].fillna("").astype(str).str.strip().unique() if value) if not filter_source_df.empty else [],
    )

filtered_db_df = filter_source_df.copy()
if candidate_filter:
    filtered_db_df = filtered_db_df[filtered_db_df["Candidate Name"].isin(candidate_filter)]
if jr_filter_values:
    filtered_db_df = filtered_db_df[filtered_db_df["JR Number"].fillna("").astype(str).str.strip().isin(jr_filter_values)]
if actual_status_filter:
    filtered_db_df = filtered_db_df[filtered_db_df["Actual Status"].fillna("").astype(str).str.strip().isin(actual_status_filter)]
if call_iteration_filter:
    filtered_db_df = filtered_db_df[filtered_db_df["Call Iteration"].fillna("").astype(str).str.strip().isin(call_iteration_filter)]
if upload_filter:
    filtered_db_df = filtered_db_df[filtered_db_df["Upload to SAP"].fillna("").astype(str).str.strip().isin(upload_filter)]

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
                        original_record = next((r for r in st.session_state.db_resume_records if str(r.get("id")) == str(row.get("id"))), {})
                    
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
        ~(editor_df[["First Name", "Last Name", "Email", "Phone"]].fillna("").apply(lambda x: x.str.strip()).eq("").all(axis=1))
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
    ~(edited_df[["First Name", "Last Name", "Email", "Phone"]].fillna("").apply(lambda x: x.str.strip()).eq("").all(axis=1))
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
                            headers["Authorization"] = f"Bearer {user['access_token']}"
                        
                        resp = requests.get(resume_link, headers=headers, timeout=30)
                        if resp.status_code == 200:
                            file_bytes = resp.content
                            st.session_state.uploaded_files_store[row["File Name"]] = file_bytes
                        else:
                            raise Exception(f"Failed to download resume from link: {resume_link} (Status {resp.status_code})")
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
                            part for part in [str(row.get("First Name", "")).strip(), str(row.get("Last Name", "")).strip()] if part
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

    st.subheader("Email Preview")
    st.text("\n".join(preview_lines))
    if candidate_rows:
        st.caption("Candidate table that will be included in email")
        st.dataframe(pd.DataFrame(candidate_rows), width="stretch")

    if st.button("Send Email", type="primary", width="stretch"):
        attachment_items = []
        for file_name in [part.strip() for part in str(draft_row.get("Files", "")).split(",") if part.strip()]:
            file_bytes = st.session_state.uploaded_files_store.get(file_name)
            if file_bytes:
                attachment_items.append({"name": file_name, "content": file_bytes})
        ok, msg = send_client_email(
            user=user,
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
