import io
import re
from datetime import date

import pandas as pd
import streamlit as st

from auth import require_login, show_user_profile
from notifier import send_client_email, send_upload_notification
from resume_parser import parse_resume
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
        recruiter_name = meta.get("client_recruiter_name", "")
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

    return pd.DataFrame(drafts)


def build_candidate_details_table(successful_rows, metadata_by_jr) -> pd.DataFrame:
    today_text = date.today().strftime("%d-%b-%Y")
    candidate_rows = []
    for row in successful_rows:
        first_name = str(row.get("First Name", "")).strip()
        last_name = str(row.get("Last Name", "")).strip()
        jr = str(row.get("JR Number", "")).strip()
        meta = metadata_by_jr.get(jr, {})
        candidate_rows.append(
            {
                "JR Number": jr,
                "Date": today_text,
                "Skill": meta.get("job_title", "") or row.get("Skill", ""),
                "Candidate Name": " ".join(part for part in [first_name, last_name] if part),
                "Contact Number": row.get("Phone", ""),
                "Email ID": row.get("Email", ""),
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


st.set_page_config(page_title="Resume -> SAP Upload", layout="wide")

# =========================
# AUTH
# =========================
user = require_login()
show_user_profile(user)

st.title("Resume -> SAP Upload")
st.caption(f"Logged in as **{user['name']}** ({user['email']})")

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

# =========================
# FILE UPLOAD & PARSE
# =========================
files = st.file_uploader(
    "Upload Resumes",
    type=["pdf", "docx"],
    accept_multiple_files=True,
    help="Each resume must have a unique filename. Duplicates will be ignored.",
)

if not files:
    st.session_state.uploaded_files_store = {}
    reset_email_state()
    st.session_state.last_uploaded_signature = ()
    st.stop()

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
    st.session_state.uploaded_files_store = {}
    reset_email_state()
    st.session_state.last_uploaded_signature = current_signature

st.info(f"{len(files)} resume(s) ready for processing")

results = []
progress = st.progress(0)

for index, file in enumerate(files):
    file.seek(0)
    st.session_state.uploaded_files_store[file.name] = file.read()

    try:
        file.seek(0)
        data = parse_resume(file)
        results.append(
            {
                "File Name": file.name,
                "First Name": data.get("first_name", ""),
                "Last Name": data.get("last_name", ""),
                "Email": data.get("email", ""),
                "Phone": data.get("phone", ""),
                "Country Code": data.get("country_code", "+91"),
                "Country": data.get("country", "India"),
                "JR Number": "",
            }
        )
    except Exception as error:
        results.append(
            {
                "File Name": file.name,
                "First Name": "",
                "Last Name": "",
                "Email": "",
                "Phone": "",
                "Country Code": "",
                "Country": "",
                "JR Number": "",
                "Error": str(error),
            }
        )

    progress.progress((index + 1) / len(files))

# =========================
# VALIDATION & TABLE
# =========================
df = pd.DataFrame(results)
df["Status"] = df.apply(
    lambda row: "Missing Data" if not row["First Name"] or not row["Email"] else "OK",
    axis=1,
)

invalid_count = len(df[df["Status"] == "Missing Data"])
if invalid_count:
    st.warning(f"{invalid_count} resume(s) need correction before upload")

st.subheader("Review & Edit Data")
edited_df = st.data_editor(
    df,
    num_rows="dynamic",
    width="stretch",
    disabled=["File Name", "Status"],
)

edited_df = edited_df.dropna(how="all")
edited_df = edited_df[
    ~(edited_df[["First Name", "Last Name", "Email", "Phone"]].fillna("").apply(lambda x: x.str.strip()).eq("").all(axis=1))
]

if edited_df.empty:
    st.warning("No valid data to upload")
    st.stop()

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
    reset_email_state()
    upload_rows = edited_df[
        (edited_df["Status"] == "OK") & (edited_df["JR Number"].fillna("").str.strip() != "")
    ]

    if upload_rows.empty:
        st.error("No valid rows with JR Number to upload")
    else:
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
                        raise Exception("File bytes not found in session")

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
                            "country_code": row["Country Code"],
                            "country": row["Country"],
                            "resume_file": file_obj,
                        },
                    )
                    results_log.append({"File": row["File Name"], "Status": "Success"})
                    successful_rows.append(row.to_dict())
                except Exception as error:
                    screenshot_name = None
                    if bot:
                        try:
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

    col1, col2 = st.columns(2)
    with col1:
        recruiter_name = st.text_input(
            "Client Recruiter Name",
            key=f"draft_recruiter_name_{selected_idx}",
            width="stretch",
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
        st.subheader("Email Candidate Table")
        st.caption("Only the required candidate columns are listed here.")
        st.session_state.email_candidates_df = st.data_editor(
            st.session_state.email_candidates_df,
            num_rows="dynamic",
            width="stretch",
        )
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
