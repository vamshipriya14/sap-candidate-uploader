"""
pages/Resume_Upload.py — Direct Resume Upload (form-based intake)

Drop PDF/DOCX resumes → auto-parse into editable table → on submit,
save rows to Supabase (Storage + Table) and fire a repository_dispatch
event to GitHub. The workflow then runs scheduler_form.py on GitHub's
runner (which has Chrome) to do the SAP upload and email the recruiter
when done.

This page requires NO Selenium / Chrome on the Streamlit host — the
heavy SAP work happens on GitHub Actions.
"""

import os
import sys
from datetime import date, datetime, timezone

import pandas as pd
import requests
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import require_login, show_navigation, show_user_profile
from resume_parser import parse_resume
from resume_repository import (
    fetch_active_jr_master,
    fetch_existing_record,
    insert_resume_record,
    jr_folder_name,
    upload_resume,
)

# ── Page setup ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="Resume Upload", page_icon="📤", layout="wide", initial_sidebar_state="collapsed")

# ── Check for public access (external user mode) ────────────────────────────
query_params = st.query_params
is_public = query_params.get("public", "").lower() == "true"

# ── Hide sidebar and navigation for public users ────────────────────────────
if is_public:
    st.markdown("""
    <style>
        [data-testid="stSidebarNav"] { display: none !important; }
        [data-testid="stSidebar"] { display: none !important; }
        section[data-testid="stSidebar"] { display: none !important; }
    </style>
    """, unsafe_allow_html=True)
else:
    st.markdown("""
    <style>[data-testid="stSidebarNav"] { display: none; }</style>
    """, unsafe_allow_html=True)

# ── Auth check: bypass for public users ────────────────────────────────────
if not is_public:
    user = require_login()
    show_user_profile(user)
    show_navigation("resume_upload")
else:
    user = None  # No user for public access

# ── User whitelist check ───────────────────────────────────────────────────
if not is_public:
    ALLOWED_USERS = st.secrets.get("ALLOWED_FORM_USERS", os.environ.get("ALLOWED_FORM_USERS", ""))
    user_email = user.get("email", "").strip().lower()

    if ALLOWED_USERS:
        # Handle both list and comma-separated string formats
        if isinstance(ALLOWED_USERS, list):
            allowed_list = [e.strip().lower() for e in ALLOWED_USERS if e]
        else:
            allowed_list = [e.strip().lower() for e in ALLOWED_USERS.split(",") if e.strip()]

        if user_email not in allowed_list:
            st.error(f"❌ Access Denied: {user.get('email')} is not authorized to submit resumes.")
            st.info(f"📧 Contact your administrator if you believe this is an error.")
            st.stop()

st.title("📤 Direct Resume Upload")
st.caption(
    "Upload resumes → save to Supabase → SAP upload runs automatically in the background. "
    "You will receive an email when processing is complete."
)

# ── GitHub dispatch config ─────────────────────────────────────────────────
GH_REPO  = st.secrets.get("GH_REPO", os.environ.get("GH_REPO", ""))
GH_TOKEN = st.secrets.get("GH_TOKEN", os.environ.get("GH_TOKEN", ""))
GH_EVENT = st.secrets.get("GH_EVENT_TYPE", "resume-form-submitted")

# ── Load JR master (active only) ────────────────────────────────────────────
@st.cache_data(ttl=300)
def _load_jr_master():
    try:
        rows = fetch_active_jr_master()
        # Filter only active JRs using jr_status column
        active_jrs = {str(r.get("jr_no", "")).strip(): r for r in rows
                      if r.get("jr_no") and r.get("jr_status", "").lower() == "active"}
        return active_jrs
    except Exception:
        return {}

jr_master = _load_jr_master()
jr_options = ["— select —"] + sorted(jr_master.keys())

# ── Helpers ─────────────────────────────────────────────────────────────────
def _safe(val) -> str:
    return str(val).strip() if val else ""

def _extract_name_from_email(email: str) -> str:
    """Extract name from email: john.doe@company.com → John Doe"""
    if not email or "@" not in email:
        return ""
    name_part = email.split("@")[0]
    # Convert john.doe → John Doe
    name = name_part.replace(".", " ").replace("-", " ").replace("_", " ")
    return " ".join(word.capitalize() for word in name.split())


def trigger_github_workflow(record_ids: list, recruiter_email: str) -> tuple[bool, str]:
    """Fire a repository_dispatch event on GitHub → workflow runs scheduler_form.py."""
    if not GH_REPO or not GH_TOKEN:
        return False, "GitHub credentials not configured in secrets (GH_REPO / GH_TOKEN)."

    resp = requests.post(
        f"https://api.github.com/repos/{GH_REPO}/dispatches",
        headers={
            "Authorization"        : f"Bearer {GH_TOKEN}",
            "Accept"               : "application/vnd.github+json",
            "X-GitHub-Api-Version" : "2022-11-28",
            "Content-Type"         : "application/json",
        },
        json={
            "event_type"    : GH_EVENT,
            "client_payload": {
                "record_ids"     : record_ids,
                "recruiter_email": recruiter_email,
                "submitted_at"   : datetime.now(timezone.utc).isoformat(),
            },
        },
        timeout=15,
    )
    if resp.status_code in (200, 201, 204):
        return True, ""
    return False, f"GitHub {resp.status_code}: {resp.text}"


# ── Session state ───────────────────────────────────────────────────────────
if "upload_rows" not in st.session_state:
    st.session_state.upload_rows = []

# ══════════════════════════════════════════════════════════════════════════
# SECTION 1 — JR selection + Recruiter Email + file upload
# ══════════════════════════════════════════════════════════════════════════
col_jr, col_email = st.columns([1, 1.5])

with col_jr:
    selected_jr = st.selectbox(
        "JR No",
        jr_options,
        help="All uploaded resumes will be tagged to this JR number.",
    )
    jr_no = "" if selected_jr == "— select —" else selected_jr
    jr_meta = jr_master.get(jr_no, {})
    skill = _safe(jr_meta.get("skill_name") or jr_meta.get("skill", ""))

with col_email:
    default_email = "sandhyam@revatechnosys.com" if is_public else user.get("email", "")
    recruiter_email = st.text_input(
        "Recruiter Email ID",
        value=default_email,
        help="Your email to receive SAP upload notifications.",
    )

# Display skill and job details in next line (full width)
if jr_no and jr_meta:
    skill_name = _safe(jr_meta.get("skill_name") or jr_meta.get("skill", ""))
    job_details = _safe(jr_meta.get("job_details", ""))

    # Trim job details to first 150 characters
    job_details_trimmed = (job_details[:150] + "...") if len(job_details) > 150 else job_details

    # Build info text - only skill and job details
    info_text = f"**Skill:** {skill_name or '—'}"
    if job_details_trimmed:
        info_text += f"\n**Job Details:** {job_details_trimmed}"

    # Custom styling for compact display
    st.markdown("""
    <style>
    [data-testid="stAlert"] { padding: 0.75rem 1rem !important; margin: 0 !important; }
    [data-testid="stAlert"] p { margin: 0.25rem 0 !important; font-size: 0.875rem; line-height: 1.2; }
    [data-testid="stAlert"] strong { font-size: 0.875rem; }
    </style>
    """, unsafe_allow_html=True)

    st.info(info_text)

st.divider()

uploaded_files = st.file_uploader(
    "Drop resumes here or click to browse (PDF / DOCX)",
    type=["pdf", "docx"],
    accept_multiple_files=True,
)

if uploaded_files:
    if st.button("⚡ Parse All Resumes", type="primary", use_container_width=True):
        today_text = date.today().strftime("%d-%b-%Y")
        rows = []
        progress = st.progress(0, text="Parsing…")

        for i, f in enumerate(uploaded_files):
            progress.progress((i + 1) / len(uploaded_files), text=f"Parsing {f.name}…")
            try:
                f.seek(0)
                parsed = parse_resume(f)
            except Exception as e:
                parsed = {"first_name": "", "last_name": "", "email": "", "phone": ""}
                st.warning(f"⚠ Parse failed for **{f.name}**: {e}")

            full_name = f"{parsed.get('first_name','')} {parsed.get('last_name','')}".strip()
            rows.append({
                "Candidate Name" : full_name,
                "Email ID"       : parsed.get("email", ""),
                "Contact Number" : parsed.get("phone", ""),
                "Recruiter Email": "",  # Empty, user fills in
                "Resume File"    : f.name,
                "_first"         : parsed.get("first_name", ""),
                "_last"          : parsed.get("last_name", ""),
                "_today"         : today_text,
                "_file_bytes"    : f.getvalue(),
            })

        progress.empty()
        st.session_state.upload_rows = rows
        st.success(f"✓ Parsed **{len(rows)}** file(s) — review and edit below.")

# ══════════════════════════════════════════════════════════════════════════
# SECTION 2 — Editable table
# ══════════════════════════════════════════════════════════════════════════
if st.session_state.upload_rows:
    rows = st.session_state.upload_rows

    display_cols = ["Candidate Name", "Email ID", "Contact Number", "Resume File"]
    df_display   = pd.DataFrame(rows)[display_cols].copy()
    df_display.insert(0, "S.No", range(1, len(df_display) + 1))

    st.markdown("#### Candidate Details — edit any cell before submitting")

    edited_df = st.data_editor(
        df_display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "S.No"          : st.column_config.NumberColumn(disabled=True, width="small"),
            "Candidate Name": st.column_config.TextColumn(width="medium"),
            "Email ID"      : st.column_config.TextColumn(width="medium"),
            "Contact Number": st.column_config.TextColumn(width="medium"),
            "Resume File"   : st.column_config.TextColumn(disabled=True, width="medium"),
        },
        num_rows="fixed",
        key="upload_table",
    )

    st.divider()

    col_submit, col_clear = st.columns([1, 1])
    with col_submit:
        do_submit = st.button(
            "✓ Submit Candidates",
            type="primary",
            use_container_width=True,
            disabled=not jr_no,
        )
        if not jr_no:
            st.caption("⚠ Select a JR No above first.")

    with col_clear:
        if st.button("✕ Clear Table", use_container_width=True):
            st.session_state.upload_rows = []
            st.rerun()

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 3 — Save to DB + Storage, then trigger GitHub
    # ══════════════════════════════════════════════════════════════════════
    if do_submit and jr_no:
        edited_records = edited_df.to_dict("records")
        today_text = date.today().strftime("%d-%b-%Y")

        # Merge edits back into internal rows (preserve _file_bytes etc.)
        merged = []
        for i, internal in enumerate(rows):
            edited = edited_records[i] if i < len(edited_records) else {}
            full_name  = _safe(edited.get("Candidate Name", ""))
            name_parts = full_name.split(" ", 1)
            first_name = name_parts[0] if name_parts else internal["_first"]
            last_name  = name_parts[1] if len(name_parts) > 1 else internal["_last"]
            merged.append({
                **internal,
                "Candidate Name": full_name,
                "Email ID"      : _safe(edited.get("Email ID",       internal["Email ID"])),
                "Contact Number": _safe(edited.get("Contact Number", internal["Contact Number"])),
                "_first"        : first_name,
                "_last"         : last_name,
            })

        progress_bar  = st.progress(0, text="Saving to database…")
        inserted_ids  = []
        summary_rows  = []

        client_recruiter       = _safe(jr_meta.get("client_recruiter") or jr_meta.get("recruiter"))
        client_recruiter_email = _safe(jr_meta.get("client_recruiter_email") or jr_meta.get("recruiter_email"))
        recruiter_name         = _extract_name_from_email(recruiter_email)

        for idx, row in enumerate(merged):
            progress_bar.progress(
                (idx + 1) / len(merged),
                text=f"Uploading {row['Candidate Name'] or row['Resume File']}…"
            )

            file_name  = row["Resume File"]
            file_bytes = row["_file_bytes"]
            email      = row["Email ID"]
            phone      = row["Contact Number"]
            first_name = row["_first"]
            last_name  = row["_last"]
            cand_label = row["Candidate Name"] or file_name

            row_data = {
                "JR Number"              : jr_no,
                "Date"                   : today_text,
                "Skill"                  : skill,
                "File Name"              : file_name,
                "First Name"             : first_name,
                "Last Name"              : last_name,
                "Email"                  : email,
                "Phone"                  : phone,
                "upload_to_sap"          : "Pending",
                "recruiter"              : recruiter_name,
                "recruiter_email"        : recruiter_email,
                "client_recruiter"       : client_recruiter,
                "client_recruiter_email" : client_recruiter_email,
                "Actual Status"          : "Not Called",
                "Call Iteration"         : "First Call",
                "created_by"             : recruiter_email,
                "modified_by"            : recruiter_email,
            }

            # ── 1. Duplicate check ────────────────────────────────────────
            existing     = fetch_existing_record(jr_no, email, phone)
            db_record_id = ""
            resume_path  = ""

            if existing:
                db_record_id = str(existing.get("id", "")).strip()
                resume_path  = existing.get("resume_path", "")
                summary_rows.append({"Candidate": cand_label, "Status": "Already exists", "ID": db_record_id})
                continue

            # ── 2. Upload to Supabase Storage ─────────────────────────────
            try:
                resume_path = upload_resume(file_name, file_bytes, jr_folder_name(jr_no))
            except Exception as e:
                if "409" in str(e):
                    resume_path = f"{jr_folder_name(jr_no)}/{file_name}"
                else:
                    summary_rows.append({"Candidate": cand_label, "Status": f"Upload failed: {e}", "ID": ""})
                    continue

            # ── 3. Insert into Supabase table ─────────────────────────────
            try:
                db_record    = insert_resume_record(row_data, user, resume_path=resume_path)
                db_record_id = str(db_record.get("id", "")).strip()
                if not db_record_id:
                    recovered    = fetch_existing_record(jr_no, email, phone)
                    db_record_id = str(recovered.get("id", "")).strip() if recovered else ""
                inserted_ids.append(db_record_id)
                summary_rows.append({"Candidate": cand_label, "Status": "Queued for SAP ✓", "ID": db_record_id})
            except Exception as e:
                if "23505" in str(e):
                    recovered    = fetch_existing_record(jr_no, email, phone)
                    db_record_id = str(recovered.get("id", "")).strip() if recovered else ""
                    summary_rows.append({"Candidate": cand_label, "Status": "Duplicate — queued", "ID": db_record_id})
                    if db_record_id:
                        inserted_ids.append(db_record_id)
                else:
                    summary_rows.append({"Candidate": cand_label, "Status": f"DB error: {e}", "ID": ""})

        progress_bar.empty()

        # ── 4. Show summary ───────────────────────────────────────────────
        st.subheader("📊 Submission Summary")
        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

        # ── 5. Trigger GitHub Actions ─────────────────────────────────────
        if inserted_ids:
            with st.spinner("Triggering SAP upload workflow…"):
                ok, err = trigger_github_workflow(
                    record_ids      = inserted_ids,
                    recruiter_email = recruiter_email if is_public else user.get("email", ""),
                )
            if ok:
                st.success(
                    f"✅ {len(inserted_ids)} candidate(s) queued. "
                    "SAP upload is running in the background — "
                    f"you will receive an email at **{user.get('email')}** when complete."
                )
            else:
                st.error(f"Records saved, but GitHub trigger failed: {err}")
                st.info("The next scheduled run will pick them up automatically.")
        else:
            st.warning("No new records queued for SAP upload.")

        st.session_state.upload_rows = []
