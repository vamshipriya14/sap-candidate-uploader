"""
Email Inbox Integration — pages/Email_Inbox.py

Connects to hrvolibot@volibits.com mailbox, reads emails with subject
matching "Profiles - BS: <skill>", extracts candidate rows from the
email body table, downloads resume attachments, uploads to OneDrive,
parses them, inserts into Supabase, and triggers SAP upload — all
without manual intervention.
"""

import base64
import io
import os
import re
import sys
from datetime import date, datetime, timezone

import pandas as pd
import requests
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import require_login, show_navigation, show_user_profile
from notifier import _get_app_token
from resume_parser import parse_resume
from resume_repository import (
    _supabase_headers,
    fetch_active_jr_master,
    insert_resume_record,
    jr_folder_name,
    upload_resume_to_hrvolibot_drive,
    SUPABASE_URL,
    SUPABASE_TABLE,
    HRVOLIBOT_ROOT_FOLDER,
)
from sap_bot_headless import SAPBot
from uploader import upload_to_sap

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
INBOX_EMAIL = "hrvolibot@volibits.com"
SUBJECT_PREFIX = "Profiles - BS:"          # standard prefix in every email


# ─────────────────────────────────────────────────────────────
# PAGE SETUP
# ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="Email Inbox Sync", page_icon="📬", layout="wide")

st.markdown("""
<style>
[data-testid="stSidebarNav"] { display: none; }
</style>
""", unsafe_allow_html=True)

user = require_login()
show_user_profile(user)
show_navigation("email_inbox")

st.title("📬 Email Inbox — Auto Resume Processor")
st.caption(
    f"Reads **{INBOX_EMAIL}** for emails with subject starting with "
    f"`{SUBJECT_PREFIX}`, downloads attachments → uploads to "
    f"**hrvolibot OneDrive / {HRVOLIBOT_ROOT_FOLDER}/<JR>/** → parses → SAP."
)

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(val) -> str:
    return str(val).strip() if val else ""


def _app_token() -> str:
    return _get_app_token()


# ── Graph API helpers ────────────────────────────────────────

def _graph_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def fetch_inbox_messages(token: str, max_messages: int = 50) -> list[dict]:
    """
    Return unread messages from hrvolibot inbox whose subject starts with
    'Profiles - BS:'.  Uses $filter + $search isn't available on shared
    mailboxes with basic licences, so we page through recent messages and
    filter locally.
    """
    url = (
        f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}/mailFolders/Inbox/messages"
        f"?$top={max_messages}"
        f"&$select=id,subject,from,receivedDateTime,body,hasAttachments,isRead"
        f"&$orderby=receivedDateTime desc"
    )
    resp = requests.get(url, headers=_graph_headers(token), timeout=30)
    resp.raise_for_status()
    all_msgs = resp.json().get("value", [])

    # Filter locally to subject prefix (case-insensitive)
    prefix_lower = SUBJECT_PREFIX.lower()
    return [
        m for m in all_msgs
        if _safe(m.get("subject")).lower().startswith(prefix_lower)
    ]


def fetch_message_attachments(token: str, message_id: str) -> list[dict]:
    """Return list of attachment dicts with name + contentBytes (decoded)."""
    url = (
        f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
        f"/messages/{message_id}/attachments"
        f"?$select=name,contentBytes,contentType,size"
    )
    resp = requests.get(url, headers=_graph_headers(token), timeout=30)
    resp.raise_for_status()
    raw = resp.json().get("value", [])

    attachments = []
    for att in raw:
        name = _safe(att.get("name"))
        content_b64 = att.get("contentBytes", "")
        if not name or not content_b64:
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ("pdf", "docx", "doc"):
            continue          # skip non-resume attachments
        try:
            content_bytes = base64.b64decode(content_b64)
        except Exception:
            continue
        attachments.append({"name": name, "bytes": content_bytes})
    return attachments


def mark_message_read(token: str, message_id: str) -> None:
    url = f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}/messages/{message_id}"
    requests.patch(url, headers=_graph_headers(token), json={"isRead": True}, timeout=15)


def move_message_to_folder(token: str, message_id: str, folder_name: str = "Processed Profiles") -> None:
    """Move message to a sub-folder (creates it if it doesn't exist)."""
    try:
        # Find or create folder
        folders_url = f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}/mailFolders/Inbox/childFolders"
        resp = requests.get(folders_url, headers=_graph_headers(token), timeout=15)
        folders = resp.json().get("value", []) if resp.status_code == 200 else []
        folder_id = next((f["id"] for f in folders if f.get("displayName") == folder_name), None)

        if not folder_id:
            create_resp = requests.post(
                folders_url,
                headers=_graph_headers(token),
                json={"displayName": folder_name},
                timeout=15,
            )
            if create_resp.status_code in (200, 201):
                folder_id = create_resp.json().get("id")

        if folder_id:
            move_url = f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}/messages/{message_id}/move"
            requests.post(move_url, headers=_graph_headers(token), json={"destinationId": folder_id}, timeout=15)
    except Exception:
        pass  # Moving is best-effort


# ── Email body table parser ───────────────────────────────────

def parse_body_table(html_body: str) -> list[dict]:
    """
    Parse the candidate table from the email body.
    Expected columns: s.no, jr_no, candidate_name, resume  (order may vary)
    Handles both HTML <table> and plain-text tab-separated rows.
    Returns list of dicts with keys: sno, jr_no, candidate_name, resume
    """
    # Try to strip HTML tags to get plain text
    text = re.sub(r"<[^>]+>", "\t", html_body)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"&[a-z]+;", " ", text)

    rows = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # Find header line
    header_line_idx = None
    col_map = {}  # col_name -> index in the split row
    HEADER_KEYS = {
        "s.no": "sno", "sno": "sno", "s no": "sno",
        "jr_no": "jr_no", "jr no": "jr_no", "jr number": "jr_no",
        "candidate_name": "candidate_name", "candidate name": "candidate_name", "name": "candidate_name",
        "resume": "resume", "resume file": "resume", "file": "resume",
    }

    for idx, line in enumerate(lines):
        parts = [p.strip() for p in re.split(r"\t+|\|", line)]
        normalized = [p.lower().replace("_", " ") for p in parts]
        matches = sum(1 for n in normalized if n in HEADER_KEYS)
        if matches >= 2:
            header_line_idx = idx
            col_map = {HEADER_KEYS[n]: i for i, n in enumerate(normalized) if n in HEADER_KEYS}
            break

    if header_line_idx is None or not col_map:
        return []

    # Parse data rows
    for line in lines[header_line_idx + 1:]:
        parts = [p.strip() for p in re.split(r"\t+|\|", line)]
        if len(parts) < 2:
            continue

        # Skip separator/empty lines
        if all(p in ("", "-", "—") for p in parts):
            continue

        def get_col(key, fallback=""):
            idx = col_map.get(key)
            if idx is not None and idx < len(parts):
                return parts[idx].strip()
            return fallback

        sno = get_col("sno")
        # Must look like a row number or text
        if not sno:
            continue

        jr_no = get_col("jr_no")
        candidate_name = get_col("candidate_name")
        resume_file = get_col("resume")

        if not jr_no and not candidate_name:
            continue  # skip blank rows

        rows.append({
            "sno": sno,
            "jr_no": _safe(jr_no),
            "candidate_name": _safe(candidate_name),
            "resume": _safe(resume_file),
        })

    return rows


def match_attachment(candidate_name: str, attachments: list[dict]) -> dict | None:
    """
    Try to find the best matching attachment for a candidate when
    the resume filename is not specified in the table.
    Uses partial name matching (case-insensitive, spaces/dots/underscores ignored).
    """
    if not candidate_name or not attachments:
        return None

    def normalise(s):
        return re.sub(r"[\s._-]+", "", s.lower())

    name_norm = normalise(candidate_name)

    # Split candidate name into parts to support partial matching
    name_parts = [p for p in re.split(r"\s+", candidate_name.lower()) if len(p) > 2]

    best = None
    best_score = 0

    for att in attachments:
        att_norm = normalise(att["name"].rsplit(".", 1)[0])  # strip extension
        # Exact name match
        if name_norm and name_norm in att_norm:
            return att

        # Partial: count how many name parts appear in attachment filename
        score = sum(1 for part in name_parts if part in att_norm)
        if score > best_score:
            best_score = score
            best = att

    return best if best_score >= 1 else None


def check_already_processed(email_message_id: str) -> bool:
    """Check Supabase if this email has been processed before (by email_message_id)."""
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
            f"?source_email_id=eq.{email_message_id}&select=id&limit=1",
            headers=_supabase_headers(),
            timeout=15,
        )
        if resp.status_code == 200 and resp.json():
            return True
    except Exception:
        pass
    return False


# ─────────────────────────────────────────────────────────────
# LOAD JR MASTER
# ─────────────────────────────────────────────────────────────
try:
    jr_master_rows = fetch_active_jr_master()
except Exception as e:
    jr_master_rows = []
    st.warning(f"JR master lookup unavailable: {e}")

jr_master_by_number = {}
for row in jr_master_rows:
    jr_no = _safe(row.get("jr_no"))
    if jr_no:
        jr_master_by_number[jr_no] = row


def _get_jr_meta(jr_no: str) -> dict:
    return jr_master_by_number.get(jr_no, {})


# ─────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────
if "inbox_messages" not in st.session_state:
    st.session_state.inbox_messages = []
if "inbox_last_fetched" not in st.session_state:
    st.session_state.inbox_last_fetched = None
if "inbox_processing_log" not in st.session_state:
    st.session_state.inbox_processing_log = []


# ─────────────────────────────────────────────────────────────
# UI — FETCH EMAILS
# ─────────────────────────────────────────────────────────────
col_fetch, col_info = st.columns([1, 3])

with col_fetch:
    fetch_clicked = st.button("🔄 Fetch Emails", type="primary", use_container_width=True)

with col_info:
    if st.session_state.inbox_last_fetched:
        st.caption(f"Last fetched: **{st.session_state.inbox_last_fetched}**")
    st.caption(
        f"Scanning inbox of `{INBOX_EMAIL}` for subjects starting with "
        f"`{SUBJECT_PREFIX}`"
    )

if fetch_clicked:
    with st.spinner("Connecting to mailbox…"):
        try:
            token = _app_token()
            messages = fetch_inbox_messages(token, max_messages=50)
            st.session_state.inbox_messages = messages
            st.session_state.inbox_last_fetched = datetime.now().strftime("%d %b %Y, %I:%M %p")
            if messages:
                st.success(f"Found **{len(messages)}** matching email(s).")
            else:
                st.info("No matching emails found in inbox.")
        except Exception as exc:
            st.error(f"Failed to fetch emails: {exc}")

messages = st.session_state.inbox_messages

# ─────────────────────────────────────────────────────────────
# DISPLAY EMAILS & PROCESS
# ─────────────────────────────────────────────────────────────
if not messages:
    st.info("Click **Fetch Emails** to scan the inbox.")
    st.stop()

st.divider()
st.subheader(f"📨 {len(messages)} Email(s) Found")

# Show summary table of emails
email_summary = []
for msg in messages:
    email_summary.append({
        "Subject": _safe(msg.get("subject")),
        "From": _safe(msg.get("from", {}).get("emailAddress", {}).get("address")),
        "Received": _safe(msg.get("receivedDateTime", ""))[:16].replace("T", " "),
        "Has Attachments": "✅" if msg.get("hasAttachments") else "❌",
        "Read": "✅" if msg.get("isRead") else "🔵 Unread",
        "ID": msg.get("id", ""),
    })

summary_df = pd.DataFrame(email_summary)
st.dataframe(
    summary_df.drop(columns=["ID"]),
    use_container_width=True,
    hide_index=True,
)

st.divider()

# ── Per-email processing ──────────────────────────────────────
submit_mode = st.toggle(
    "Submit to SAP (Live Mode)",
    value=False,
    help="ON = actually submit candidates to SAP. OFF = dry run (fill + cancel).",
)
if submit_mode:
    st.caption("🔴 Live mode — candidates will be submitted to SAP.")
else:
    st.caption("🟡 Dry run mode — SAP form will be filled and cancelled.")

process_all = st.button(
    "⚡ Process All Emails → OneDrive → SAP",
    type="primary",
    use_container_width=True,
    help="Downloads attachments, uploads to OneDrive, parses resumes, inserts into DB, uploads to SAP.",
)

if process_all:
    token = _app_token()
    today_text = date.today().strftime("%d-%b-%Y")
    overall_log = []
    bot = None

    progress_bar = st.progress(0)
    status_box = st.empty()

    try:
        status_box.info("Connecting to SAP…")
        bot = SAPBot()
        bot.start()
        bot.login()
        status_box.success("SAP connected ✅")
    except Exception as sap_exc:
        st.error(f"SAP connection failed: {sap_exc}")
        bot = None

    for msg_idx, msg in enumerate(messages):
        msg_id = msg.get("id", "")
        subject = _safe(msg.get("subject"))
        from_email = _safe(msg.get("from", {}).get("emailAddress", {}).get("address"))

        # Extract skill from subject: "Profiles - BS: SAP Architect" → "SAP Architect"
        skill_from_subject = ""
        subj_match = re.match(
            r"profiles\s*-\s*bs:\s*(.+)", subject, re.IGNORECASE
        )
        if subj_match:
            skill_from_subject = subj_match.group(1).strip()

        st.markdown(f"### 📧 Email {msg_idx + 1}/{len(messages)}: `{subject}`")
        st.caption(f"From: {from_email}")

        # Check if already processed
        if check_already_processed(msg_id):
            st.info("⏭️ Already processed — skipping.")
            overall_log.append({
                "Email": subject, "Candidate": "—", "Status": "Already Processed", "JR": "—"
            })
            continue

        # Parse candidate table from body
        body_content = msg.get("body", {}).get("content", "")
        candidates_in_email = parse_body_table(body_content)

        if not candidates_in_email:
            st.warning("⚠️ Could not parse candidate table from email body. Skipping.")
            overall_log.append({
                "Email": subject, "Candidate": "—", "Status": "Table Parse Failed", "JR": "—"
            })
            continue

        st.write(f"Found **{len(candidates_in_email)}** candidate row(s) in email body:")
        st.dataframe(pd.DataFrame(candidates_in_email), hide_index=True, use_container_width=True)

        # Fetch attachments
        try:
            attachments = fetch_message_attachments(token, msg_id)
            st.write(f"Downloaded **{len(attachments)}** resume attachment(s): "
                     + ", ".join(a["name"] for a in attachments))
        except Exception as att_exc:
            attachments = []
            st.warning(f"Could not fetch attachments: {att_exc}")

        # Build attachment lookup by filename
        att_by_name = {a["name"].lower(): a for a in attachments}

        # ── Process each candidate row ───────────────────────
        for cand in candidates_in_email:
            jr_no = cand["jr_no"]
            candidate_name = cand["candidate_name"]
            specified_resume = cand["resume"]

            cand_label = candidate_name or specified_resume or f"Row {cand['sno']}"
            st.markdown(f"**→ {cand_label}** (JR: `{jr_no}`)")

            # Resolve attachment
            att = None
            if specified_resume:
                att = att_by_name.get(specified_resume.lower())
                if not att:
                    # Try partial filename match
                    for att_name, a in att_by_name.items():
                        if specified_resume.lower() in att_name or att_name in specified_resume.lower():
                            att = a
                            break
            if not att and candidate_name:
                att = match_attachment(candidate_name, attachments)

            if not att:
                msg_str = f"Resume not found for **{cand_label}** (looked for `{specified_resume or candidate_name}`)"
                st.error(f"❌ {msg_str}")
                overall_log.append({
                    "Email": subject, "Candidate": cand_label, "Status": "Resume Not Found", "JR": jr_no
                })
                continue

            file_name = att["name"]
            file_bytes = att["bytes"]

            # 1. Upload to hrvolibot OneDrive  →  Inbox Resumes/<JR>/<file>
            try:
                resume_link = upload_resume_to_hrvolibot_drive(
                    file_name, file_bytes, jr_no
                )
                st.write(
                    f"  ☁️ Uploaded to hrvolibot OneDrive: "
                    f"`{HRVOLIBOT_ROOT_FOLDER}/{jr_folder_name(jr_no)}/{file_name}`"
                )
            except Exception as od_exc:
                resume_link = ""
                st.warning(f"  ⚠️ OneDrive upload failed: {od_exc}")

            # 2. Parse resume
            parsed = {}
            try:
                file_obj = io.BytesIO(file_bytes)
                file_obj.name = file_name
                parsed = parse_resume(file_obj)
            except Exception as parse_exc:
                st.warning(f"  ⚠️ Resume parse failed: {parse_exc}")

            # Build row dict
            jr_meta = _get_jr_meta(jr_no)
            skill = jr_meta.get("skill_name", "") or skill_from_subject

            # Split candidate_name from email table into first/last
            name_parts = candidate_name.split(" ", 1) if candidate_name else []
            first_name = parsed.get("first_name") or (name_parts[0] if name_parts else "")
            last_name = parsed.get("last_name") or (name_parts[1] if len(name_parts) > 1 else "")

            row_data = {
                "JR Number": jr_no,
                "Date": today_text,
                "Skill": skill,
                "File Name": file_name,
                "First Name": first_name,
                "Last Name": last_name,
                "Email": parsed.get("email", ""),
                "Phone": parsed.get("phone", ""),
                "Current Company": parsed.get("current_company", ""),
                "Total Experience": parsed.get("total_experience", ""),
                "Relevant Experience": parsed.get("relevant_experience", ""),
                "Current CTC": parsed.get("current_ctc", ""),
                "Expected CTC": parsed.get("expected_ctc", ""),
                "Notice Period": parsed.get("notice_period", ""),
                "Current Location": parsed.get("current_location", ""),
                "Preferred Location": parsed.get("preferred_location", ""),
                "Actual Status": "Not Called",
                "Call Iteration": "First Call",
                "comments/Availability": "",
                "Error": "",
                "Upload to SAP": "Yes",
                "client_recruiter": jr_meta.get("client_recruiter", ""),
                "client_recruiter_email": jr_meta.get("recruiter_email", ""),
                "client_email_sent": "No",
                "recruiter": user.get("name", ""),
                "recruiter_email": user.get("email", ""),
                # Store the source email ID to detect re-processing
                "source_email_id": msg_id,
            }

            # 3. Insert into Supabase DB
            db_record_id = None
            try:
                db_record = insert_resume_record(row_data, user, resume_link=resume_link)
                db_record_id = str(db_record.get("id", "")).strip()
                st.write(f"  💾 Saved to DB (id: `{db_record_id}`)")
            except Exception as db_exc:
                st.error(f"  ❌ DB insert failed: {db_exc}")
                overall_log.append({
                    "Email": subject, "Candidate": cand_label, "Status": f"DB Error: {db_exc}", "JR": jr_no
                })
                continue

            # 4. Upload to SAP
            if not bot:
                st.warning("  ⚠️ SAP bot not connected — skipping SAP upload.")
                overall_log.append({
                    "Email": subject, "Candidate": cand_label, "Status": "Skipped (SAP unavailable)", "JR": jr_no
                })
                continue

            sap_status = "Failed"
            sap_error = ""
            try:
                file_obj = io.BytesIO(file_bytes)
                file_obj.name = file_name
                upload_to_sap(
                    bot,
                    {
                        "jr_number": jr_no,
                        "first_name": first_name,
                        "last_name": last_name,
                        "submit": submit_mode,
                        "email": row_data["Email"],
                        "phone": row_data["Phone"],
                        "country_code": "+91",
                        "country": "India",
                        "resume_file": file_obj,
                    },
                )
                sap_status = "Done"
                st.success(f"  ✅ SAP upload {'submitted' if submit_mode else 'dry-run'}: **{cand_label}**")
            except Exception as sap_exc:
                sap_error = str(sap_exc)
                st.error(f"  ❌ SAP upload failed: {sap_error}")

            # 5. Update DB with SAP status
            if db_record_id:
                try:
                    update_fields = {
                        "upload_to_sap": sap_status,
                        "error_message": sap_error[:500] if sap_error else "",
                    }
                    requests.patch(
                        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=eq.{db_record_id}",
                        headers=_supabase_headers(),
                        json=update_fields,
                        timeout=15,
                    )
                    st.write(f"  📝 DB updated → `upload_to_sap = {sap_status}`")
                except Exception as upd_exc:
                    st.warning(f"  ⚠️ DB status update failed: {upd_exc}")

            overall_log.append({
                "Email": subject,
                "Candidate": cand_label,
                "JR": jr_no,
                "Status": f"SAP {sap_status}" if sap_status == "Done" else f"SAP Failed: {sap_error[:60]}",
            })

        # Mark email as read + move to processed folder
        try:
            mark_message_read(token, msg_id)
            move_message_to_folder(token, msg_id)
            st.write("  📁 Email marked as read and moved to **Processed Profiles** folder.")
        except Exception as mv_exc:
            st.warning(f"  ⚠️ Could not mark/move email: {mv_exc}")

        progress_bar.progress((msg_idx + 1) / len(messages))

    # Close SAP bot
    if bot:
        try:
            bot.close()
        except Exception:
            pass

    st.divider()
    st.subheader("📊 Processing Summary")
    if overall_log:
        log_df = pd.DataFrame(overall_log)
        st.dataframe(log_df, use_container_width=True, hide_index=True)
        done_count = sum(1 for r in overall_log if "Done" in r.get("Status", ""))
        st.metric("Successfully uploaded to SAP", done_count, delta=f"of {len(overall_log)} total")
    else:
        st.info("No candidates were processed.")

    st.session_state.inbox_processing_log = overall_log

# ── Show last run log if available ───────────────────────────
elif st.session_state.inbox_processing_log:
    st.divider()
    st.subheader("📋 Last Processing Run")
    st.dataframe(
        pd.DataFrame(st.session_state.inbox_processing_log),
        use_container_width=True,
        hide_index=True,
    )

# ─────────────────────────────────────────────────────────────
# INDIVIDUAL EMAIL PREVIEW
# ─────────────────────────────────────────────────────────────
st.divider()
st.subheader("🔍 Preview Individual Email")

if messages:
    email_options = [
        f"{i+1}. {_safe(m.get('subject'))} — {_safe(m.get('receivedDateTime',''))[:10]}"
        for i, m in enumerate(messages)
    ]
    selected_idx = st.selectbox("Select email to preview", range(len(messages)), format_func=lambda i: email_options[i])
    selected_msg = messages[selected_idx]

    body_html = selected_msg.get("body", {}).get("content", "")
    candidates_preview = parse_body_table(body_html)

    col_body, col_table = st.columns([1, 1])
    with col_body:
        with st.expander("📄 Raw Email Body (HTML)", expanded=False):
            st.code(body_html[:3000], language="html")

    with col_table:
        st.markdown("**Parsed Candidate Table:**")
        if candidates_preview:
            st.dataframe(pd.DataFrame(candidates_preview), hide_index=True, use_container_width=True)
        else:
            st.warning("Could not parse candidate table from this email.")

    if st.button("📎 Show Attachments", key="preview_attachments"):
        try:
            token = _app_token()
            atts = fetch_message_attachments(token, selected_msg["id"])
            if atts:
                for a in atts:
                    st.write(f"- **{a['name']}** ({len(a['bytes']):,} bytes)")
            else:
                st.info("No resume attachments found.")
        except Exception as e:
            st.error(f"Could not fetch attachments: {e}")
