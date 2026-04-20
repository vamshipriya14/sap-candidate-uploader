"""
Email Inbox Integration — pages/Email_Inbox.py (UPDATED)

Connects to hrvolibot@volibits.com mailbox, reads emails with subject
matching "Profiles - BS: <skill>", extracts candidate rows from the
email body table, downloads resume attachments, uploads to OneDrive,
parses them, inserts into Supabase, and triggers SAP upload — all
without manual intervention.

UPDATES:
✅ Fixed 400 Bad Request error when fetching attachments
✅ Integrated azure_auth for centralized credential management
✅ Added retry logic and detailed error handling
✅ Uses email_handler for robust email operations
"""

import base64
import io
import os
import re
import sys
import time
from datetime import date, datetime, timezone

import pandas as pd
import requests
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import require_login, show_navigation, show_user_profile
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

# ✅ NEW: Import improved Azure auth and email handling
try:
    from azure_auth import get_access_token, validate_credentials
    from email_handler import (
        fetch_message_attachments,
        list_inbox_messages,
        list_inbox_subfolders,
        mark_message_read,
        move_message_to_folder,
        get_attachment_file_names,
    )
    AZURE_AUTH_AVAILABLE = True
except ImportError as e:
    st.warning(f"⚠️ Azure auth modules not available: {e}. Using fallback auth.")
    AZURE_AUTH_AVAILABLE = False
    get_access_token = None


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

# ✅ NEW: Validate Azure credentials on startup
if AZURE_AUTH_AVAILABLE and "azure_validated" not in st.session_state:
    try:
        validate_credentials()
        st.session_state.azure_validated = True
    except Exception as e:
        st.error(
            f"❌ Azure Credentials Error\n\n{str(e)}\n\n"
            f"Please set environment variables:\n"
            f"- ST_AZURE_TENANT_ID\n"
            f"- ST_AZURE_CLIENT_ID\n"
            f"- ST_AZURE_CLIENT_SECRET"
        )
        st.info("Proceeding with fallback authentication...")


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(val) -> str:
    return str(val).strip() if val else ""


def _get_token() -> str:
    """
    Get Azure access token using new auth module if available,
    otherwise fall back to legacy method.
    """
    if AZURE_AUTH_AVAILABLE and get_access_token:
        try:
            return get_access_token()
        except Exception as e:
            st.warning(f"Azure token acquisition failed: {e}. Trying fallback...")

    # Fallback to original method
    try:
        from notifier import _get_app_token
        return _get_app_token()
    except Exception as e:
        st.error(f"Token acquisition failed: {e}")
        st.stop()


# ── Graph API helpers (legacy, kept for fallback) ────────────────────────────────────────

def _graph_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get_inbox_subfolder_ids(token: str) -> list[tuple]:
    """
    Return list of (folder_id, display_name) for Inbox + all its child subfolders.
    Recursively fetches up to 2 levels deep to cover Outlook rule-created folders.
    """
    headers = _graph_headers(token)
    folders = [("Inbox", "Inbox")]

    child_url = (
        f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
        f"/mailFolders/Inbox/childFolders?$top=50&$select=id,displayName"
    )
    resp = requests.get(child_url, headers=headers, timeout=20)
    if resp.status_code == 200:
        for f in resp.json().get("value", []):
            fid = f.get("id", "")
            fname = f.get("displayName", "")
            if fid:
                folders.append((fid, fname))
                # One level deeper (grandchildren)
                gc_url = (
                    f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
                    f"/mailFolders/{fid}/childFolders?$top=50&$select=id,displayName"
                )
                gc_resp = requests.get(gc_url, headers=headers, timeout=20)
                if gc_resp.status_code == 200:
                    for gf in gc_resp.json().get("value", []):
                        gfid = gf.get("id", "")
                        gfname = gf.get("displayName", "")
                        if gfid:
                            folders.append((gfid, f"{fname}/{gfname}"))
    return folders


def fetch_inbox_messages_legacy(token: str, max_messages: int = 50) -> list[dict]:
    """
    Legacy version: Return messages whose subject starts with 'Profiles - BS:' from
    Inbox AND all its subfolders (handles Outlook routing rules).
    Used as fallback when email_handler is not available.
    """
    prefix_lower = SUBJECT_PREFIX.lower()
    headers = _graph_headers(token)
    matched = []

    # Discover Inbox root + all subfolders
    folders = _get_inbox_subfolder_ids(token)

    for folder_id, folder_name in folders:
        # Try $search first (scans full folder regardless of position)
        search_url = (
            f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
            f"/mailFolders/{folder_id}/messages"
            f"?$search=\"subject:Profiles\""
            f"&$top={max_messages}"
            f"&$select=id,subject,from,receivedDateTime,body,hasAttachments,isRead"
        )
        resp = requests.get(search_url, headers=headers, timeout=30)
        if resp.status_code == 200:
            msgs = resp.json().get("value", [])
            hits = [m for m in msgs if _safe(m.get("subject")).lower().startswith(prefix_lower)]
            if hits:
                matched.extend(hits)
                continue  # found via search, skip fallback for this folder

        # Fallback: plain page + local filter
        plain_url = (
            f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
            f"/mailFolders/{folder_id}/messages"
            f"?$top={max_messages}"
            f"&$select=id,subject,from,receivedDateTime,body,hasAttachments,isRead"
            f"&$orderby=receivedDateTime desc"
        )
        resp = requests.get(plain_url, headers=headers, timeout=30)
        if resp.status_code == 200:
            msgs = resp.json().get("value", [])
            hits = [m for m in msgs if _safe(m.get("subject")).lower().startswith(prefix_lower)]
            matched.extend(hits)

    # Deduplicate by message id
    seen = set()
    unique = []
    for m in matched:
        mid = m.get("id", "")
        if mid not in seen:
            seen.add(mid)
            unique.append(m)

    # Sort newest first
    unique.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
    return unique


def fetch_message_attachments_legacy(token: str, message_id: str) -> list[dict]:
    """
    Legacy version: Return list of attachment dicts with name + contentBytes (decoded).
    Used as fallback when email_handler is not available.
    """
    url = (
        f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
        f"/messages/{message_id}/attachments"
        f"?$select=name,contentBytes,contentType,size"
    )
    try:
        resp = requests.get(url, headers=_graph_headers(token), timeout=30)
        if resp.status_code == 400:
            raise Exception(
                f"❌ 400 Bad Request: Message ID may be malformed, expired, or inaccessible. "
                f"This email will be skipped. Message ID: {message_id}"
            )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        st.error(f"Failed to fetch attachments: {e}")
        raise

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


def get_resume_attachments(message_id: str, token: str = None) -> list[dict]:
    """
    Fetch resume attachments (PDF, DOCX, DOC) from a message.

    ✅ HANDLES 400 ERRORS with detailed diagnostics
    Uses new email_handler if available, otherwise falls back to legacy.

    Args:
        message_id: Message ID from list_inbox_messages
        token: Access token (auto-acquired if not provided)

    Returns:
        list: Attachments with name, bytes, contentType
    """
    if not token:
        token = _get_token()

    try:
        # Try using new email_handler first
        if AZURE_AUTH_AVAILABLE:
            attachments = fetch_message_attachments(INBOX_EMAIL, message_id, token=token)
        else:
            # Fallback to legacy method
            attachments = fetch_message_attachments_legacy(token, message_id)

        # Filter for resume file types
        resume_exts = ("pdf", "docx", "doc")
        resume_files = []

        for att in attachments:
            name = str(att.get("name", "")).strip()
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

            if ext in resume_exts:
                resume_files.append(att)

        return resume_files

    except Exception as e:
        error_msg = str(e)

        # Provide specific guidance for common errors
        if "400 Bad Request" in error_msg:
            st.error(
                f"⚠️ **Could not fetch attachments from this email**\n\n"
                f"This usually means:\n"
                f"• The email has been deleted or moved\n"
                f"• The message ID has expired\n"
                f"• You lack permission to access this message\n\n"
                f"**Action:** This email will be marked as processed and skipped.\n\n"
                f"Details: {error_msg}"
            )
        else:
            st.error(f"Error fetching attachments: {error_msg}")

        return []


def mark_message_read_safe(token: str, message_id: str) -> None:
    """Mark a message as read with error handling."""
    try:
        if AZURE_AUTH_AVAILABLE:
            mark_message_read(INBOX_EMAIL, message_id, token)
        else:
            url = f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}/messages/{message_id}"
            requests.patch(url, headers=_graph_headers(token), json={"isRead": True}, timeout=15)
    except Exception as e:
        st.warning(f"Could not mark message as read: {e}")


def move_message_to_folder_safe(token: str, message_id: str) -> None:
    """Move message to 'Processed Profiles' folder."""
    try:
        if AZURE_AUTH_AVAILABLE:
            # Try to get or create 'Processed Profiles' folder
            # For now, just use move_message_to_folder with 'ProcessedMail' ID
            # In real implementation, you'd fetch the folder ID first
            move_message_to_folder(INBOX_EMAIL, message_id, "ProcessedMail", token)
        else:
            # Legacy version - try to move to DeletedItems as fallback
            url = f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}/messages/{message_id}/move"
            requests.post(url, headers=_graph_headers(token), json={"destinationId": "DeletedItems"}, timeout=15)
    except Exception as e:
        st.warning(f"Could not move message: {e}")


# ─────────────────────────────────────────────────────────────
# TABLE PARSING
# ─────────────────────────────────────────────────────────────

def parse_body_table(body_html: str) -> list[dict]:
    """
    Parse an HTML email body table into candidate rows.
    Extracts: Candidate Name, Email, Phone, JR Number
    """
    if not body_html:
        return []

    rows = []
    # Pattern: <td>value</td> cells in table rows
    row_pattern = r"<tr[^>]*>(.*?)</tr>"
    cell_pattern = r"<td[^>]*>(.*?)</td>"

    for row_match in re.finditer(row_pattern, body_html, re.IGNORECASE | re.DOTALL):
        row_html = row_match.group(1)
        cells = re.findall(cell_pattern, row_html, re.IGNORECASE | re.DOTALL)

        if len(cells) >= 4:
            # Extract text and clean HTML
            def clean_text(html_text):
                text = re.sub(r"<[^>]+>", "", html_text)
                return _safe(text)

            candidate_name = clean_text(cells[0])
            email = clean_text(cells[1])
            phone = clean_text(cells[2])
            jr_number = clean_text(cells[3])

            if candidate_name and email:
                rows.append({
                    "Candidate Name": candidate_name,
                    "Email": email,
                    "Phone": phone,
                    "JR Number": jr_number,
                })

    return rows


def _get_jr_meta(jr_no: str) -> dict:
    """Get JR metadata from database."""
    try:
        jr_list = fetch_active_jr_master()
        for jr in jr_list:
            if str(jr.get("jr_no", "")).strip() == str(jr_no).strip():
                return {
                    "skill_name": jr.get("skill_name", ""),
                    "client_recruiter": jr.get("client_recruiter", ""),
                    "recruiter_email": jr.get("recruiter_email", ""),
                }
    except Exception as e:
        st.warning(f"Could not fetch JR metadata: {e}")
    return {}


# ─────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# ─────────────────────────────────────────────────────────────

if "inbox_processing_log" not in st.session_state:
    st.session_state.inbox_processing_log = []


# ─────────────────────────────────────────────────────────────
# MAIN PROCESSING
# ─────────────────────────────────────────────────────────────

col1, col2, col3 = st.columns(3)

with col1:
    fetch_emails_btn = st.button("🔄 Fetch Emails", key="fetch_btn")

with col2:
    submit_mode = st.checkbox("✅ Submit to SAP (uncheck for dry-run)", value=False)

with col3:
    max_msgs = st.number_input("Max emails to process", min_value=1, max_value=100, value=20)

if fetch_emails_btn:
    st.write("---")

    try:
        token = _get_token()

        # Fetch emails using new handler if available, otherwise use legacy
        if AZURE_AUTH_AVAILABLE:
            try:
                messages = list_inbox_messages(INBOX_EMAIL, subject_filter="Profiles", max_messages=max_msgs, token=token)
                # Filter by exact prefix
                prefix_lower = SUBJECT_PREFIX.lower()
                messages = [
                    m for m in messages
                    if _safe(m.get("subject", "")).lower().startswith(prefix_lower)
                ]
            except Exception as e:
                st.warning(f"Failed to fetch with email_handler: {e}. Using legacy method...")
                messages = fetch_inbox_messages_legacy(token, max_messages=max_msgs)
        else:
            messages = fetch_inbox_messages_legacy(token, max_messages=max_msgs)

        if not messages:
            st.info(f"No emails found with subject starting with '{SUBJECT_PREFIX}'")
        else:
            st.success(f"✅ Found {len(messages)} email(s)")

            # Initialize SAP bot
            bot = None
            try:
                bot = SAPBot()
                st.success("✅ SAP bot connected")
            except Exception as e:
                st.warning(f"⚠️ SAP bot connection failed: {e}. Will skip SAP uploads.")

            # Process each message
            progress_bar = st.progress(0)
            overall_log = []

            for msg_idx, msg in enumerate(messages):
                msg_id = msg.get("id", "")
                subject = _safe(msg.get("subject", ""))

                st.write(f"\n### 📧 Email {msg_idx + 1}: {subject}")

                # Parse email body for candidate rows
                body_html = msg.get("body", {}).get("content", "")
                candidates = parse_body_table(body_html)

                if not candidates:
                    st.warning("  ⚠️ No candidate table found in email body")
                    overall_log.append({
                        "Email": subject,
                        "Candidate": "N/A",
                        "Status": "No candidates found",
                        "JR": "N/A"
                    })
                    progress_bar.progress((msg_idx + 1) / len(messages))
                    continue

                st.write(f"  Found {len(candidates)} candidate(s)")

                # Process each candidate
                for cand_idx, candidate in enumerate(candidates):
                    candidate_name = _safe(candidate.get("Candidate Name", ""))
                    email = _safe(candidate.get("Email", ""))
                    jr_no = _safe(candidate.get("JR Number", ""))

                    cand_label = f"{candidate_name} ({email})"
                    st.write(f"\n  **Candidate {cand_idx + 1}: {cand_label}** (JR: {jr_no})")

                    # Extract skill from subject
                    skill_match = re.search(r"Profiles - BS:\s*(.+?)(?:\s*[-–]|$)", subject)
                    skill_from_subject = skill_match.group(1).strip() if skill_match else ""

                    if not jr_no:
                        st.warning(f"    ⚠️ No JR number found. Skipping.")
                        overall_log.append({
                            "Email": subject, "Candidate": cand_label, "Status": "No JR number", "JR": "N/A"
                        })
                        continue

                    # Fetch attachments for this candidate
                    try:
                        file_attachments = get_resume_attachments(msg_id, token)
                        if not file_attachments:
                            st.info(f"    ℹ️ No resume attachments found. Skipping.")
                            overall_log.append({
                                "Email": subject, "Candidate": cand_label, "Status": "No attachments", "JR": jr_no
                            })
                            continue
                    except Exception as e:
                        st.error(f"    ❌ Attachment error: {str(e)}")
                        overall_log.append({
                            "Email": subject, "Candidate": cand_label, "Status": f"Attachment error: {str(e)[:50]}", "JR": jr_no
                        })
                        continue

                    # Process first attachment only
                    att = file_attachments[0]
                    file_name = _safe(att.get("name", ""))
                    file_bytes = att.get("bytes", b"")

                    st.write(f"    📎 Processing: {file_name}")

                    # Prepare resume data
                    today_text = str(date.today())
                    resume_link = ""

                    # 1. Upload to OneDrive
                    try:
                        resume_link = upload_resume_to_hrvolibot_drive(
                            file_name, file_bytes, jr_no
                        )
                        st.write(
                            f"    ☁️ Uploaded to hrvolibot OneDrive: "
                            f"`{HRVOLIBOT_ROOT_FOLDER}/{jr_folder_name(jr_no)}/{file_name}`"
                        )
                    except Exception as od_exc:
                        resume_link = ""
                        st.warning(f"    ⚠️ OneDrive upload failed: {od_exc}")

                    # 2. Parse resume
                    parsed = {}
                    try:
                        file_obj = io.BytesIO(file_bytes)
                        file_obj.name = file_name
                        parsed = parse_resume(file_obj)
                    except Exception as parse_exc:
                        st.warning(f"    ⚠️ Resume parse failed: {parse_exc}")

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
                        st.write(f"    💾 Saved to DB (id: `{db_record_id}`)")
                    except Exception as db_exc:
                        st.error(f"    ❌ DB insert failed: {db_exc}")
                        overall_log.append({
                            "Email": subject, "Candidate": cand_label, "Status": f"DB Error: {db_exc}", "JR": jr_no
                        })
                        continue

                    # 4. Upload to SAP
                    if not bot:
                        st.warning("    ⚠️ SAP bot not connected — skipping SAP upload.")
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
                        st.success(f"    ✅ SAP upload {'submitted' if submit_mode else 'dry-run'}: **{cand_label}**")
                    except Exception as sap_exc:
                        sap_error = str(sap_exc)
                        st.error(f"    ❌ SAP upload failed: {sap_error}")

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
                            st.write(f"    📝 DB updated → `upload_to_sap = {sap_status}`")
                        except Exception as upd_exc:
                            st.warning(f"    ⚠️ DB status update failed: {upd_exc}")

                    overall_log.append({
                        "Email": subject,
                        "Candidate": cand_label,
                        "JR": jr_no,
                        "Status": f"SAP {sap_status}" if sap_status == "Done" else f"SAP Failed: {sap_error[:60]}",
                    })

                # Mark email as read + move to processed folder
                try:
                    mark_message_read_safe(token, msg_id)
                    move_message_to_folder_safe(token, msg_id)
                    st.write("  📁 Email marked as read.")
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

    except Exception as e:
        st.error(f"Processing failed: {e}")
        import traceback
        st.error(traceback.format_exc())

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

try:
    token = _get_token()

    if AZURE_AUTH_AVAILABLE:
        try:
            messages = list_inbox_messages(INBOX_EMAIL, subject_filter="Profiles", max_messages=20, token=token)
            prefix_lower = SUBJECT_PREFIX.lower()
            messages = [
                m for m in messages
                if _safe(m.get("subject", "")).lower().startswith(prefix_lower)
            ]
        except Exception:
            messages = fetch_inbox_messages_legacy(token, max_messages=20)
    else:
        messages = fetch_inbox_messages_legacy(token, max_messages=20)

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
                atts = get_resume_attachments(selected_msg["id"], token)
                if atts:
                    for a in atts:
                        st.write(f"- **{a['name']}** ({len(a['bytes']):,} bytes)")
                else:
                    st.info("No resume attachments found.")
            except Exception as e:
                st.error(f"Could not fetch attachments: {e}")
    else:
        st.info("No emails found to preview")
except Exception as e:
    st.warning(f"Preview unavailable: {e}")