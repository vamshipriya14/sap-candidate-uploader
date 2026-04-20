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
from notifier import _get_app_token, send_upload_notification
from resume_parser import parse_resume
from resume_repository import (
    _headers,
    fetch_active_jr_master,
    fetch_existing_record,
    insert_resume_record,
    jr_folder_name,
    upload_resume,
    SUPABASE_URL,
    SUPABASE_TABLE,

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
    f"**<JR>/** → parses → SAP."
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


def fetch_inbox_messages(token: str, max_messages: int = 50) -> list[dict]:
    """
    Return messages whose subject starts with 'Profiles - BS:' from
    Inbox AND all its subfolders (handles Outlook routing rules).
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
def fetch_message_attachments(token: str, message_id: str) -> list[dict]:
    headers = _graph_headers(token)

    # Step 1: Get attachment metadata
    url = (
        f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
        f"/messages/{message_id}/attachments"
        f"?$select=id,name,contentType,size"
    )

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    attachments = []

    for att in resp.json().get("value", []):
        name = _safe(att.get("name"))
        att_id = att.get("id")

        if not name or not att_id:
            continue

        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ("pdf", "docx", "doc"):
            continue

        # Step 2: Download actual content
        content_url = (
            f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
            f"/messages/{message_id}/attachments/{att_id}/$value"
        )

        file_resp = requests.get(content_url, headers=headers, timeout=30)

        if file_resp.status_code == 200:
            attachments.append({
                "name": name,
                "bytes": file_resp.content
            })

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

HEADER_KEYS = {
    "s.no": "sno", "sno": "sno", "s no": "sno", "s_no": "sno",
    "jr_no": "jr_no", "jr no": "jr_no", "jr number": "jr_no", "jr_number": "jr_no",
    "candidate_name": "candidate_name", "candidate name": "candidate_name", "name": "candidate_name",
    "resume": "resume", "resume file": "resume", "file": "resume",
}

_STOP_WORDS = {"hi", "hello", "dear", "regards", "thanks", "thank", "sincerely", "best"}


def _find_header_tokens(line: str) -> list[tuple]:
    """
    Locate all known header keywords in a line (supports multi-word like 'candidate name').
    Returns list of (char_start, char_end, canonical_key) sorted by position.
    """
    found = []
    line_lower = line.lower()
    sorted_keys = sorted(HEADER_KEYS.keys(), key=lambda x: -len(x))
    used_ranges = []
    for key in sorted_keys:
        pattern = re.sub(r"[ _]", r"[ _]", re.escape(key))
        for m in re.finditer(pattern, line_lower):
            start, end = m.start(), m.end()
            if any(s <= start < e or s < end <= e for s, e in used_ranges):
                continue
            used_ranges.append((start, end))
            found.append((start, end, HEADER_KEYS[key]))
    found.sort(key=lambda x: x[0])
    return found


def _is_footer(line: str) -> bool:
    first = line.strip().split()[0].lower().rstrip(",") if line.strip() else ""
    return first in _STOP_WORDS


def _make_row(parts: list, col_map: dict) -> dict | None:
    def get(key):
        i = col_map.get(key)
        return parts[i].strip() if i is not None and i < len(parts) and isinstance(parts[i], str) else ""
    jr_no = get("jr_no")
    candidate_name = get("candidate_name")
    if not jr_no and not candidate_name:
        return None
    if not re.search(r"\w", get("sno") + jr_no + candidate_name):
        return None
    return {
        "sno": _safe(get("sno")),
        "jr_no": _safe(jr_no),
        "candidate_name": _safe(candidate_name),
        "resume": _safe(get("resume")),
    }


def _extract_rows(lines: list, col_map: dict, col_starts: list = None, splitter: str = None) -> list[dict]:
    rows = []
    for line in lines:
        if not line.strip():
            continue
        if _is_footer(line):
            break
        if col_starts:
            parts = [
                line[col_starts[i]: col_starts[i + 1]].strip() if col_starts[i] <= len(line) else ""
                for i in range(len(col_starts) - 1)
            ] + [line[col_starts[-1]:].strip() if col_starts[-1] <= len(line) else ""]
        else:
            parts = [p.strip() for p in re.split(splitter, line)]
            if splitter and "|" in splitter:
                parts = [p for p in parts if p]
        row = _make_row(parts, col_map)
        if row:
            rows.append(row)
    return rows


def parse_body_table(html_body: str) -> list[dict]:
    """
    Parse the candidate table from the email body.
    Expected columns: s.no, jr_no, candidate_name, resume  (order may vary)

    Handles all common formats in priority order:
      1. HTML <table>
      2. Tab-separated
      3. Pipe-separated
      4. Comma-separated
      5. Space-aligned plain text (e.g. typed in Outlook)

    Returns list of dicts with keys: sno, jr_no, candidate_name, resume
    """

    # ── 1. HTML <table> ───────────────────────────────────────────────────────
    if re.search(r"<table", html_body, re.IGNORECASE):
        tr_blocks = re.findall(r"<tr[^>]*>(.*?)</tr>", html_body, re.IGNORECASE | re.DOTALL)
        table_rows = []
        for tr in tr_blocks:
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.IGNORECASE | re.DOTALL)
            clean = [re.sub(r"<[^>]+>", " ", c).replace("&nbsp;", " ").replace("&amp;", "&").strip() for c in cells]
            if any(clean):
                table_rows.append(clean)
        if table_rows:
            for hi, hrow in enumerate(table_rows):
                tokens = _find_header_tokens(" | ".join(hrow))
                if len(tokens) >= 2:
                    cm = {}
                    for ci, cell in enumerate(hrow):
                        toks = _find_header_tokens(cell)
                        if toks:
                            cm[toks[0][2]] = ci
                    if len(cm) >= 2:
                        rows = []
                        for cells in table_rows[hi + 1:]:
                            if cells and _is_footer(cells[0]):
                                break
                            row = _make_row(cells, cm)
                            if row:
                                rows.append(row)
                        if rows:
                            return rows
                    break

    # ── Strip HTML to plain text ──────────────────────────────────────────────
    text = re.sub(r"<[^>]+>", " ", html_body)
    for ent, rep in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")]:
        text = text.replace(ent, rep)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    lines = [line for line in text.splitlines() if line.strip()]

    # Find header line using multi-word-aware token finder
    header_idx, header_tokens, header_line = None, [], ""
    for idx, line in enumerate(lines):
        tokens = _find_header_tokens(line)
        if len(tokens) >= 2:
            header_idx = idx
            header_tokens = tokens
            header_line = line
            break

    if header_idx is None:
        return []

    data_lines = lines[header_idx + 1:]
    col_map = {tok[2]: i for i, tok in enumerate(header_tokens)}

    # ── 2. Tab-separated ──────────────────────────────────────────────────────
    if "\t" in header_line:
        return _extract_rows(data_lines, col_map, splitter=r"\t+")

    # ── 3. Pipe-separated ─────────────────────────────────────────────────────
    if "|" in header_line:
        pipe_parts = [p.strip() for p in header_line.split("|") if p.strip()]
        cm = {}
        for i, p in enumerate(pipe_parts):
            toks = _find_header_tokens(p)
            if toks:
                cm[toks[0][2]] = i
        if len(cm) >= 2:
            return _extract_rows(data_lines, cm, splitter=r"\|")

    # ── 4. Comma-separated ────────────────────────────────────────────────────
    if "," in header_line and header_line.count(",") >= 2:
        comma_parts = [p.strip() for p in header_line.split(",")]
        cm = {}
        for i, p in enumerate(comma_parts):
            toks = _find_header_tokens(p)
            if toks:
                cm[toks[0][2]] = i
        if len(cm) >= 2:
            return _extract_rows(data_lines, cm, splitter=r",")

    # ── 5. Space-aligned: use char offsets of header token starts ─────────────
    col_starts = [tok[0] for tok in header_tokens]
    return _extract_rows(data_lines, col_map, col_starts=col_starts)


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
            headers=_headers(),
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
    "⚡ Process All Emails → Database → SAP",
    type="primary",
    use_container_width=True,
    help="Downloads attachments, uploads to Database, parses resumes, inserts into DB, uploads to SAP.",
)

if process_all:
    token = _app_token()
    today_text = date.today().strftime("%d-%b-%Y")
    overall_log = []
    results_log = []          # for send_upload_notification (keys: File, Status)
    failed_upload_attachments = []  # screenshots of SAP failures
    bot = None

    progress_bar = st.progress(0)
    status_box = st.empty()


    def start_sap_bot():
        bot = SAPBot()
        bot.start()
        bot.login()
        return bot


    try:
        status_box.info("Connecting to SAP…")
        bot = start_sap_bot()
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
                results_log.append({
                    "File": cand_label,
                    "Status": "Resume Not Found"
                })
                overall_log.append({
                    "Email": subject, "Candidate": cand_label, "Status": "Resume Not Found", "JR": jr_no
                })
                continue

            file_name = att["name"]
            file_bytes = att["bytes"]

            # 1. Upload to hrvolibot OneDrive  →  Inbox Resumes/<JR>/<file>
            try:
                jr_folder = jr_no if jr_no else "pending_jr"

                resume_path = upload_resume(file_name, file_bytes, jr_folder)
                st.write(
                    f"  ☁️ Uploaded to supabase resumes bucket: "
                    f"`{jr_folder_name(jr_no)}/{file_name}`"
                )
            except Exception as od_exc:
                resume_path = ""
                st.warning(f"  ⚠️ DB resume upload failed: {od_exc}")

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
                db_record = insert_resume_record(
                    row_data,
                    user,
                    resume_path=resume_path
                )

                db_record_id = str(db_record.get("id", "")).strip()
                existing_record = {}

                # 🔥 If duplicate (no ID returned)
                if not db_record_id:
                    existing_record = fetch_existing_record(
                        jr_no,
                        row_data["Email"],
                        row_data["Phone"]
                    )

                    db_record_id = str(existing_record.get("id", "")).strip()

                    st.write(f"  🔁 Duplicate found → using existing record `{db_record_id}`")

                    # 🔥 IMPORTANT: ensure we use existing status
                    if existing_record:
                        existing_status = str(existing_record.get("upload_to_sap", "")).strip().lower()
                    else:
                        existing_status = ""
                else:
                    existing_record = db_record
                    existing_status = str(existing_record.get("upload_to_sap", "")).strip().lower()

                st.write(f"  💾 DB ready (id: `{db_record_id}`)")

            except Exception as db_exc:
                st.error(f"  ❌ DB insert failed: {db_exc}")
                overall_log.append({
                    "Email": subject, "Candidate": cand_label, "Status": f"DB Error: {db_exc}", "JR": jr_no
                })
                results_log.append({
                    "File": file_name,
                    "Status": f"DB Error: {str(db_exc)[:100]}"
                })
                continue

            # 4. Upload to SAP
            # 🔥 Decide if SAP upload is needed
            upload_needed = True

            if existing_status == "Done":
                upload_needed = False
                st.info(f"  ⏭️ Already uploaded to SAP — skipping: **{cand_label}**")

            if not upload_needed:
                overall_log.append({
                    "Email": subject,
                    "Candidate": cand_label,
                    "Status": "Already in SAP",
                    "JR": jr_no,
                })
                results_log.append({
                    "File": file_name,
                    "Status": "Already in SAP"
                })
                continue

            if not bot:
                st.warning("  ⚠️ SAP bot not connected — skipping SAP upload.")
                overall_log.append({
                    "Email": subject,
                    "Candidate": cand_label,
                    "Status": "Skipped (SAP unavailable)",
                    "JR": jr_no,
                })
                results_log.append({
                    "File": file_name,
                    "Status": "SAP Skipped (No Bot)"
                })
                continue

            # 🔥 INIT (REQUIRED)
            sap_status = "Failed"
            sap_error = ""
            def is_session_dead(err):
                msg = str(err).lower()
                return "invalid session id" in msg or "disconnected" in msg or "not connected to devtools" in msg


            for attempt in range(2):  # retry once
                try:
                    # 🧠 Check if driver is alive before using
                    try:
                        bot.driver.current_url
                    except:
                        raise Exception("SAP session lost")

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
                    results_log.append({"File": file_name, "Status": "Success"})
                    break

                except Exception as sap_exc:
                    sap_error = str(sap_exc)

                    # 🔥 If session crashed → restart
                    if is_session_dead(sap_exc) and attempt == 0:
                        st.warning("⚠️ SAP session lost. Restarting browser...")

                        try:
                            bot.close()
                        except:
                            pass

                        try:
                            bot = start_sap_bot()
                            st.info("🔁 SAP session restarted. Retrying...")
                            continue
                        except Exception as restart_exc:
                            sap_error = f"Restart failed: {restart_exc}"
                            break
                    else:
                        break

            if sap_status != "Done":
                st.error(f"  ❌ SAP upload failed: {sap_error}")

                results_log.append({
                    "File": file_name,
                    "Status": sap_error[:120] if sap_error else "SAP upload failed",
                })
                st.error(f"  ❌ SAP upload failed: {sap_error}")
                # Capture a screenshot for the failure notification
                if bot:
                    try:
                        screenshot_name = (
                            f"{re.sub(r'[<>:\"/\\\\|?*]+', '_', jr_no or 'unknown_jr')}_"
                            f"{re.sub(r'[<>:\"/\\\\|?*]+', '_', cand_label or 'candidate')}_failed_upload"
                        )
                        screenshot_path = bot._screenshot(screenshot_name)
                        failed_upload_attachments.append({
                            "name": f"{screenshot_name}.png",
                            "content": screenshot_path.read_bytes(),
                        })
                    except Exception:
                        pass
                results_log.append({
                    "File": file_name,
                    "Status": sap_error[:120] if sap_error else "SAP upload failed",
                })
            # 5. Update DB with SAP status
            if db_record_id:
                try:
                    update_fields = {
                        "upload_to_sap": sap_status,
                        "error_message": sap_error[:500] if sap_error else "",
                    }

                    resp = requests.patch(
                        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=eq.{db_record_id}",
                        headers=_headers(),  # ✅ FIXED
                        json=update_fields,
                        timeout=15,
                    )

                    if resp.status_code not in (200, 204):
                        raise Exception(f"DB update failed: {resp.text}")
                    else:
                        st.write(f"  📝 DB updated → upload_to_sap = {sap_status}")

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

    # ── Send upload report email ──────────────────────────────────
    if results_log:
        with st.spinner("Sending upload report…"):
            ok, msg = send_upload_notification(
                access_token=user.get("access_token", ""),
                user=user,
                results=results_log,
                submit_mode=submit_mode,
                attachments=failed_upload_attachments,
            )
        if ok:
            st.info(f"📧 Upload report sent to **{user['email']}**")
        else:
            st.warning(f"Upload report not sent: {msg}")

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