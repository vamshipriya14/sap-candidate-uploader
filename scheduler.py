"""
scheduler.py — Autonomous Email → Resume → SAP Pipeline
Triggered every 30 min by GitHub Actions. No Streamlit dependency.
"""

import io
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
SRC  = ROOT / "src"
sys.path.insert(0, str(SRC))

from notifier import _get_app_token, _upload_report_status, send_upload_notification
from resume_parser import parse_resume
from resume_repository import (
    _headers,
    fetch_active_jr_master,
    fetch_existing_record,
    fetch_record_by_file_name,
    fetch_record_by_candidate_name,
    insert_resume_record,
    jr_folder_name,
    upload_resume,
    SUPABASE_URL,
    SUPABASE_TABLE,
)
from sap_bot_headless import SAPBot
from uploader import missing_upload_fields, upload_to_sap

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("scheduler")

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
INBOX_EMAIL = os.environ.get("INBOX_EMAIL", "hrvolibot@volibits.com")
SUBJECT_PREFIX   = "Profiles - BS:"
SUBMIT_TO_SAP    = os.environ.get("SCHEDULER_SUBMIT_TO_SAP", "true").lower() == "true"
MAX_MESSAGES     = int(os.environ.get("SCHEDULER_MAX_MESSAGES", "50"))
SCHEDULER_USER   = {
    "email": os.environ.get("SCHEDULER_USER_EMAIL", "scheduler@volibits.com"),
    "name": "Scheduler Bot",
    "access_token": "",
}
EMAIL_CC = os.environ.get("SCHEDULER_EMAIL_CC", "").split(",")


# ─────────────────────────────────────────────────────────────
# GRAPH API HELPERS
# ─────────────────────────────────────────────────────────────
def _safe(val) -> str:
    return str(val).strip() if val else ""

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _graph_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def _get_inbox_subfolder_ids(token: str) -> list:
    headers = _graph_headers(token)
    folders = [("Inbox", "Inbox")]
    child_url = (
        f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
        f"/mailFolders/Inbox/childFolders?$top=50&$select=id,displayName"
    )
    resp = requests.get(child_url, headers=headers, timeout=20)
    if resp.status_code == 200:
        for f in resp.json().get("value", []):
            fid, fname = f.get("id", ""), f.get("displayName", "")
            if fid:
                folders.append((fid, fname))
                gc_url = (
                    f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
                    f"/mailFolders/{fid}/childFolders?$top=50&$select=id,displayName"
                )
                gc_resp = requests.get(gc_url, headers=headers, timeout=20)
                if gc_resp.status_code == 200:
                    for gf in gc_resp.json().get("value", []):
                        gfid, gfname = gf.get("id", ""), gf.get("displayName", "")
                        if gfid:
                            folders.append((gfid, f"{fname}/{gfname}"))
    return folders

def fetch_inbox_messages(token: str) -> list:
    prefix_lower = SUBJECT_PREFIX.lower()
    headers      = _graph_headers(token)
    matched      = []
    for folder_id, _ in _get_inbox_subfolder_ids(token):
        search_url = (
            f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
            f"/mailFolders/{folder_id}/messages"
            f"?$search=\"subject:Profiles\""
            f"&$top={MAX_MESSAGES}"
            f"&$select=id,subject,from,receivedDateTime,body,hasAttachments,isRead"
        )
        resp = requests.get(search_url, headers=headers, timeout=30)
        if resp.status_code == 200:
            hits = [m for m in resp.json().get("value", [])
                    if _safe(m.get("subject")).lower().startswith(prefix_lower)]
            if hits:
                matched.extend(hits)
                continue
        plain_url = (
            f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
            f"/mailFolders/{folder_id}/messages"
            f"?$top={MAX_MESSAGES}"
            f"&$select=id,subject,from,receivedDateTime,body,hasAttachments,isRead"
            f"&$orderby=receivedDateTime desc"
        )
        resp = requests.get(plain_url, headers=headers, timeout=30)
        if resp.status_code == 200:
            hits = [m for m in resp.json().get("value", [])
                    if _safe(m.get("subject")).lower().startswith(prefix_lower)]
            matched.extend(hits)
    seen, unique = set(), []
    for m in matched:
        mid = m.get("id", "")
        if mid not in seen:
            seen.add(mid)
            unique.append(m)
    unique.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
    return unique

def fetch_message_attachments(token: str, message_id: str) -> list:
    headers = _graph_headers(token)
    url = (
        f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
        f"/messages/{message_id}/attachments"
        f"?$select=id,name,contentType,size"
    )
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    attachments = []
    for att in resp.json().get("value", []):
        name, att_id = _safe(att.get("name")), att.get("id")
        if not name or not att_id:
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ("pdf", "docx", "doc"):
            continue
        content_url = (
            f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
            f"/messages/{message_id}/attachments/{att_id}/$value"
        )
        file_resp = requests.get(content_url, headers=headers, timeout=30)
        if file_resp.status_code == 200:
            attachments.append({"name": name, "bytes": file_resp.content})
    return attachments

def move_message_to_processed(token: str, message_id: str) -> None:
    folder_name = "Processed Profiles"
    try:
        folders_url = (
            f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}"
            f"/mailFolders/Inbox/childFolders"
        )
        resp      = requests.get(folders_url, headers=_graph_headers(token), timeout=15)
        folders   = resp.json().get("value", []) if resp.status_code == 200 else []
        folder_id = next((f["id"] for f in folders if f.get("displayName") == folder_name), None)
        if not folder_id:
            cr = requests.post(
                folders_url,
                headers=_graph_headers(token),
                json={"displayName": folder_name},
                timeout=15,
            )
            if cr.status_code in (200, 201):
                folder_id = cr.json().get("id")
        if folder_id:
            requests.post(
                f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}/messages/{message_id}/move",
                headers=_graph_headers(token),
                json={"destinationId": folder_id},
                timeout=15,
            )
            log.info(f"Moved message {message_id} → {folder_name}")
    except Exception as e:
        log.warning(f"Move failed (non-critical): {e}")

def check_already_processed(email_message_id: str) -> bool:
    try:
        if not SUPABASE_TABLE:
            log.warning("SUPABASE_TABLE is not set — skipping duplicate check")
            return False

        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
            f"?source_email_id=eq.{email_message_id}&select=id,upload_to_sap",
            headers=_headers(),
            timeout=15,
        )
        if resp.status_code == 200:
            records = resp.json()
            if not isinstance(records, list):   # ← guard against swagger response
                log.warning(f"Unexpected DB response type: {type(records)} — treating as unprocessed")
                return False
            if not records:
                return False
            statuses = [str(r.get("upload_to_sap", "")).strip().lower() for r in records]
            return all(s in ("done", "skipped") for s in statuses)
    except Exception as e:
        log.warning(f"check_already_processed error: {e}")
    return False

# ── Email body table parser (same as Email_Inbox.py) ─────────
HEADER_KEYS = {
    "s.no": "sno", "sno": "sno", "s no": "sno", "s_no": "sno",
    "jr_no": "jr_no", "jr no": "jr_no", "jr number": "jr_no", "jr_number": "jr_no",
    "candidate_name": "candidate_name", "candidate name": "candidate_name", "name": "candidate_name",
    "resume": "resume", "resume file": "resume", "file": "resume",
    # ── NEW ──────────────────────────────────────────────────────────────
    "email": "email", "email id": "email", "email address": "email",
    "phone": "phone", "phone number": "phone", "mobile": "phone",
    "mobile number": "phone", "contact": "phone", "contact number": "phone",
}

_STOP_WORDS = {"hi", "hello", "dear", "regards", "thanks", "thank", "sincerely", "best"}

def _find_header_tokens(line: str) -> list:
    found, used_ranges = [], []
    line_lower  = line.lower()
    sorted_keys = sorted(HEADER_KEYS.keys(), key=lambda x: -len(x))
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

def _make_row(parts: list, col_map: dict):
    def get(key):
        i = col_map.get(key)
        return parts[i].strip() if i is not None and i < len(parts) and isinstance(parts[i], str) else ""
    jr_no          = get("jr_no")
    candidate_name = get("candidate_name")
    if not jr_no and not candidate_name:
        return None
    if not re.search(r"\w", get("sno") + jr_no + candidate_name):
        return None
    return {
        "sno":            _safe(get("sno")),
        "jr_no":          _safe(jr_no),
        "candidate_name": _safe(candidate_name),
        "resume":         _safe(get("resume")),
        "email":          _safe(get("email")),    # ← NEW
        "phone":          _safe(get("phone")),    # ← NEW
    }
def _extract_rows(lines, col_map, col_starts=None, splitter=None) -> list:
    rows = []
    for line in lines:
        if not line.strip() or _is_footer(line):
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

def parse_body_table(html_body: str) -> list:
    if re.search(r"<table", html_body, re.IGNORECASE):
        tr_blocks  = re.findall(r"<tr[^>]*>(.*?)</tr>", html_body, re.IGNORECASE | re.DOTALL)
        table_rows = []
        for tr in tr_blocks:
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.IGNORECASE | re.DOTALL)
            clean = [
                re.sub(r"<[^>]+>", " ", c).replace("&nbsp;", " ").replace("&amp;", "&").strip()
                for c in cells
            ]
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
    text = re.sub(r"<[^>]+>", " ", html_body)
    for ent, rep in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")]:
        text = text.replace(ent, rep)
    text  = re.sub(r"&#\d+;", "", text)
    text  = re.sub(r"&[a-z]+;", " ", text)
    lines = [line for line in text.splitlines() if line.strip()]
    header_idx, header_tokens, header_line = None, [], ""
    for idx, line in enumerate(lines):
        tokens = _find_header_tokens(line)
        if len(tokens) >= 2:
            header_idx, header_tokens, header_line = idx, tokens, line
            break
    if header_idx is None:
        return []
    data_lines = lines[header_idx + 1:]
    col_map    = {tok[2]: i for i, tok in enumerate(header_tokens)}
    if "\t" in header_line:
        return _extract_rows(data_lines, col_map, splitter=r"\t+")
    if "|" in header_line:
        pipe_parts = [p.strip() for p in header_line.split("|") if p.strip()]
        cm = {}
        for i, p in enumerate(pipe_parts):
            toks = _find_header_tokens(p)
            if toks:
                cm[toks[0][2]] = i
        if len(cm) >= 2:
            return _extract_rows(data_lines, cm, splitter=r"\|")
    if "," in header_line and header_line.count(",") >= 2:
        comma_parts = [p.strip() for p in header_line.split(",")]
        cm = {}
        for i, p in enumerate(comma_parts):
            toks = _find_header_tokens(p)
            if toks:
                cm[toks[0][2]] = i
        if len(cm) >= 2:
            return _extract_rows(data_lines, cm, splitter=r",")
    col_starts = [tok[0] for tok in header_tokens]
    return _extract_rows(data_lines, col_map, col_starts=col_starts)

def match_attachment(candidate_name: str, attachments: list):
    if not candidate_name or not attachments:
        return None
    def normalise(s):
        return re.sub(r"[\s._-]+", "", s.lower())
    name_norm  = normalise(candidate_name)
    name_parts = [p for p in re.split(r"\s+", candidate_name.lower()) if len(p) > 2]
    best, best_score = None, 0
    for att in attachments:
        att_norm = normalise(att["name"].rsplit(".", 1)[0])
        if name_norm and name_norm in att_norm:
            return att
        score = sum(1 for part in name_parts if part in att_norm)
        if score > best_score:
            best_score, best = score, att
    return best if best_score >= 1 else None

# ─────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────
def run_pipeline() -> dict:
    """
    Full pipeline: fetch → parse → upload → SAP → report.
    Returns a summary dict for logging / notification.
    """
    run_start  = datetime.now(timezone.utc)
    today_text = date.today().strftime("%d-%b-%Y")
    summary    = {"started_at": run_start.isoformat(), "emails": 0,
                  "candidates": 0, "done": 0, "skipped": 0,
                  "failed": 0, "errors": []}

    log.info("=" * 60)
    log.info(f"Scheduler run started — submit_to_sap={SUBMIT_TO_SAP}")

    # ── 1. Auth token ─────────────────────────────────────────
    try:
        token = _get_app_token()
    except Exception as e:
        log.error(f"Auth token failed: {e}")
        summary["errors"].append(f"Auth: {e}")
        return summary

    # ── 2. Load JR master ─────────────────────────────────────
    try:
        jr_master_rows = fetch_active_jr_master()
    except Exception as e:
        log.warning(f"JR master unavailable: {e}")
        jr_master_rows = []
    jr_master = {_safe(r.get("jr_no")): r for r in jr_master_rows if _safe(r.get("jr_no"))}

    def _get_jr_meta(jr_no):
        return jr_master.get(jr_no, {})

    # ── 3. Fetch emails ───────────────────────────────────────

    try:
        messages = fetch_inbox_messages(token)
    except Exception as e:
        log.error(f"Fetch inbox failed: {e}")
        summary["errors"].append(f"Fetch inbox: {e}")
        return summary

    log.info(f"Found {len(messages)} matching email(s)")
    summary["emails"] = len(messages)

    if not messages:
        log.info("Nothing to process.")
        return summary

    # ── 4. Start SAP bot ──────────────────────────────────────
    bot = None
    NON_CRITICAL_SAP_ERRORS = ["requisition id", "not found in job list"]
    DEAD_SESSION_ERRORS     = ["invalid session id", "no such session", "disconnected"]

    def _start_bot():
        b = SAPBot()
        b.start()
        b.login()
        return b

    try:
        bot = _start_bot()
        log.info("SAP bot connected ✅")
    except Exception as e:
        log.error(f"SAP bot failed to start: {e}")
        summary["errors"].append(f"SAP start: {e}")

    # ── 5. Per-email loop ─────────────────────────────────────
    results_log              = []   # for notification email
    failed_upload_attachments = []

    for msg in messages:
        msg_id   = msg.get("id", "")

        subject  = _safe(msg.get("subject"))
        from_email = _safe(msg.get("from", {}).get("emailAddress", {}).get("address"))
        log.info(f"Processing msg_id={msg_id} subject={subject} isRead={msg.get('isRead')}")
        skill_from_subject = ""
        subj_match = re.match(r"profiles\s*-\s*bs:\s*(.+)", subject, re.IGNORECASE)
        if subj_match:
            skill_from_subject = subj_match.group(1).strip()

        log.info(f"--- Email: {subject} | From: {from_email}")
        # Debug: log what check_already_processed actually finds
        try:
            debug_resp = requests.get(
                f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
                f"?source_email_id=eq.{msg_id}&select=id,upload_to_sap",
                headers=_headers(),
                timeout=15,
            )
            log.info(f"DB check for msg_id={msg_id}: status={debug_resp.status_code} records={debug_resp.json()}")
        except Exception as e:
            log.info(f"DB check failed: {e}")

        already_done = check_already_processed(msg_id)
        log.info(f"check_already_processed result = {already_done}")

        if already_done:
            log.info("Already processed — skipping.")
            continue
        body_content       = msg.get("body", {}).get("content", "")
        candidates_in_email = parse_body_table(body_content)

        if not candidates_in_email:
            log.warning("Could not parse candidate table — skipping email.")
            summary["errors"].append(f"Table parse failed: {subject}")
            continue

        log.info(f"Parsed {len(candidates_in_email)} candidate row(s)")

        try:
            attachments = fetch_message_attachments(token, msg_id)
            log.info(f"Downloaded {len(attachments)} attachment(s)")
        except Exception as e:
            attachments = []
            log.warning(f"Attachments fetch failed: {e}")

        att_by_name = {a["name"].lower(): a for a in attachments}

        # ── Per-candidate loop ────────────────────────────────
        for cand in candidates_in_email:
            summary["candidates"] += 1
            jr_no          = cand["jr_no"]
            candidate_name = cand["candidate_name"]
            specified_resume = cand["resume"]
            cand_label     = candidate_name or specified_resume or f"Row {cand['sno']}"

            log.info(f"  → {cand_label} (JR: {jr_no})")

            # 5a. Resolve attachment
            att = None
            if specified_resume:
                att = att_by_name.get(specified_resume.lower())
                if not att:
                    for k, a in att_by_name.items():
                        if specified_resume.lower() in k or k in specified_resume.lower():
                            att = a
                            break
            if not att and candidate_name:
                att = match_attachment(candidate_name, attachments)
            if not att:
                log.error(f"Resume not found for {cand_label}")
                summary["failed"] += 1
                results_log.append({"File": cand_label, "Status": "Failed"})
                continue

            file_name  = att["name"]
            file_bytes = att["bytes"]

            # 5b. Parse resume
            parsed = {}
            try:
                file_obj      = io.BytesIO(file_bytes)
                file_obj.name = file_name
                parsed        = parse_resume(file_obj)
            except Exception as e:
                log.warning(f"Resume parse failed: {e}")

            # 5c. Build row_data
            jr_meta    = _get_jr_meta(jr_no)
            skill      = jr_meta.get("skill_name", "") or skill_from_subject
            name_parts = candidate_name.split(" ", 1) if candidate_name else []
            first_name = parsed.get("first_name") or (name_parts[0] if name_parts else "")
            last_name  = parsed.get("last_name")  or (name_parts[1] if len(name_parts) > 1 else "")
            client_recruiter       = jr_meta.get("client_recruiter") or jr_meta.get("recruiter") or ""
            client_recruiter_email = jr_meta.get("client_recruiter_email") or jr_meta.get("recruiter_email") or ""

            # ── Fallback: use table values if parser returned nothing ──
            email_parsed = parsed.get("email", "") or cand.get("email", "")  # ← fallback
            phone_parsed = parsed.get("phone", "") or cand.get("phone", "")  # ← fallback

            row_data = {
                "JR Number":              jr_no,
                "Date":                   today_text,
                "Skill":                  skill,
                "File Name":              file_name,
                "First Name":             first_name,
                "Last Name":              last_name,
                "Email":                  email_parsed,
                "Phone":                  phone_parsed,
                "upload_to_sap":          "Pending",
                "client_recruiter":       client_recruiter,
                "client_recruiter_email": client_recruiter_email,
                "Actual Status":          "Not Called",
                "Call Iteration":         "First Call",
                "source_email_id":        msg_id,
                "created_by":             from_email,
                "modified_by":            from_email,
            }

            # 5d. Duplicate check
            existing_record = fetch_existing_record(jr_no, row_data["Email"], row_data["Phone"])
            db_record_id    = ""
            existing_status = ""
            resume_path     = ""

            if existing_record:
                db_record_id    = str(existing_record.get("id") or "").strip()
                existing_status = str(existing_record.get("upload_to_sap", "")).strip().lower()
                resume_path     = existing_record.get("resume_path", "")
                log.info(f"Existing record → {db_record_id} status={existing_status}")
                # Backfill recruiter if missing
                if db_record_id and (
                    not existing_record.get("client_recruiter")
                    or not existing_record.get("client_recruiter_email")
                ):
                    requests.patch(
                        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=eq.{db_record_id}",
                        headers=_headers(),
                        json={
                            "client_recruiter":       client_recruiter,
                            "client_recruiter_email": client_recruiter_email,
                            "modified_by":            from_email,
                            "modified_at":            _now_iso(),
                        },
                        timeout=10,
                    )

            # 5e. Upload resume (new only)
            if not existing_record:
                try:
                    jr_folder   = jr_no if jr_no else "pending_jr"
                    resume_path = upload_resume(file_name, file_bytes, jr_folder)
                except Exception as e:
                    if "409" in str(e):
                        resume_path = resume_path or f"{jr_folder_name(jr_no)}/{file_name}"
                    else:
                        log.warning(f"Upload failed: {e}")
                        resume_path = ""

            # 5f. Insert DB (new only)
            if not existing_record:
                try:
                    db_record    = insert_resume_record(row_data, SCHEDULER_USER, resume_path=resume_path)
                    db_record_id = str(db_record.get("id", "")).strip()
                    if not db_record_id:
                        recovered    = fetch_existing_record(jr_no, row_data["Email"], row_data["Phone"])
                        db_record_id = str(recovered.get("id", "")).strip() if recovered else ""
                    if db_record_id:
                        requests.patch(
                            f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=eq.{db_record_id}",
                            headers=_headers(),
                            json={
                                "upload_to_sap":          "Pending",
                                "client_recruiter":       client_recruiter,
                                "client_recruiter_email": client_recruiter_email,
                                "created_by":             from_email,
                                "modified_by":            from_email,
                            },
                            timeout=10,
                        )
                except Exception as e:
                    if "23505" in str(e):
                        log.warning("Duplicate — recovering record ID…")
                        recovered = (
                            fetch_existing_record(jr_no, row_data["Email"], row_data["Phone"])
                            or fetch_record_by_file_name(jr_no, file_name)
                            or fetch_record_by_candidate_name(jr_no, candidate_name)
                        )
                        if recovered:
                            db_record_id    = str(recovered.get("id", "")).strip()
                            existing_status = str(recovered.get("upload_to_sap", "")).strip().lower()
                            resume_path     = recovered.get("resume_path", "") or resume_path
                            log.info(f"Recovered duplicate → {db_record_id}")
                        else:
                            log.error(f"Could not recover record for {cand_label}")
                            summary["failed"] += 1
                            results_log.append({"File": file_name, "Status": "Failed"})
                            continue
                    else:
                        log.error(f"DB insert failed: {e}")
                        summary["failed"] += 1
                        continue

            log.info(f"DB ready → {db_record_id}")

            missing = missing_upload_fields({
                "jr_number": jr_no,
                "first_name": first_name,
                "last_name": last_name,
                "email": row_data["Email"],
                "phone": row_data["Phone"],
                "resume_file": resume_path,
            })
            if missing:
                log.info(f"Skipping SAP upload - missing required data: {', '.join(missing)}")
                if db_record_id:
                    try:
                        requests.patch(
                            f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=eq.{db_record_id}",
                            headers=_headers(),
                            json={
                                "upload_to_sap": "Skipped",
                                "error_message": "",
                                "modified_by": from_email,
                                "modified_at": _now_iso(),
                            },
                            timeout=10,
                        )
                    except Exception as e:
                        log.warning(f"Failed to mark incomplete record as Skipped: {e}")
                summary["skipped"] += 1
                continue

            # 5g. SAP upload decision
            if existing_status in ("done",):
                log.info("Already in SAP — skipping.")
                summary["skipped"] += 1
                results_log.append({"File": file_name, "Status": "Success"})
                continue

            if not bot:
                log.warning("SAP bot unavailable — skipping SAP upload.")
                summary["skipped"] += 1
                results_log.append({"File": file_name, "Status": "Failed"})
                continue

            # 5h. SAP upload with retry
            sap_status = "Failed"
            sap_error  = ""
            screenshot_captured = False

            for attempt in range(2):
                try:
                    file_obj      = io.BytesIO(file_bytes)
                    file_obj.name = file_name
                    upload_to_sap(bot, {
                        "jr_number":   jr_no,
                        "first_name":  first_name,
                        "last_name":   last_name,
                        "email":       row_data["Email"],
                        "phone":       row_data["Phone"],
                        "resume_file": file_obj,
                        "submit":      SUBMIT_TO_SAP,
                    })
                    sap_status = "Done"
                    log.info(f"SAP upload success: {cand_label}")
                    break
                except Exception as e:
                    sap_error = str(e)
                    sap_status = "Failed"
                    if any(err in sap_error.lower() for err in NON_CRITICAL_SAP_ERRORS):
                        sap_status = "Skipped"
                        log.warning(f"SAP skipped (non-critical): {sap_error}")
                        break
                    if any(err in sap_error.lower() for err in DEAD_SESSION_ERRORS):
                        sap_status = "Pending"
                        log.warning(f"SAP session dead (attempt {attempt + 1}) — restarting…")
                        try:
                            bot.quit()
                        except Exception:
                            pass
                        try:
                            bot = _start_bot()
                            log.info("SAP bot restarted.")
                        except Exception as restart_err:
                            log.error(f"Bot restart failed: {restart_err}")
                            bot = None
                            break
                        continue
                    if screenshot_captured:
                        log.error(f"SAP upload failed (attempt {attempt + 1}): {sap_error}")
                        continue
                    try:
                        screenshot_name = f"{jr_no}_{cand_label}_attempt{attempt + 1}"
                        screenshot_path = bot._screenshot(screenshot_name)
                        failed_upload_attachments.append({
                            "name":    f"{screenshot_name}.png",
                            "content": screenshot_path.read_bytes(),
                        })
                        screenshot_captured = True
                    except Exception:
                        pass
                    log.error(f"SAP upload failed (attempt {attempt + 1}): {sap_error}")

            # 5i. Update DB status
            if not db_record_id:
                recovered = (
                    fetch_existing_record(jr_no, row_data["Email"], row_data["Phone"])
                    or fetch_record_by_file_name(jr_no, file_name)
                )
                if recovered:
                    db_record_id = str(recovered.get("id", "")).strip()
                    log.warning(f"db_record_id was empty — recovered as {db_record_id}")

            if db_record_id:
                patch_payload = {
                    "upload_to_sap":          sap_status,
                    "client_recruiter":       client_recruiter,
                    "client_recruiter_email": client_recruiter_email,
                    "modified_by":            from_email,
                    "modified_at":            _now_iso(),
                }
                if sap_error:
                    patch_payload["error_message"] = sap_error[:500]
                try:
                    resp = requests.patch(
                        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=eq.{db_record_id}",
                        headers={**_headers(), "Prefer": "return=representation"},
                        json=patch_payload,
                        timeout=15,
                    )
                    if resp.status_code == 200 and not resp.json():
                        # fallback by jr_number + file_name
                        requests.patch(
                            f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
                            f"?jr_number=eq.{jr_no}&file_name=eq.{file_name}",
                            headers={**_headers(), "Prefer": "return=representation"},
                            json=patch_payload,
                            timeout=15,
                        )
                except Exception as e:
                    log.warning(f"DB patch exception: {e}")
            else:
                log.warning(f"Cannot update DB — no record ID for {cand_label}")

            if sap_status == "Done":
                summary["done"] += 1
            elif sap_status == "Skipped":
                summary["skipped"] += 1
            else:
                summary["failed"] += 1

            results_log.append({
                "File":   file_name,
                "Status": _upload_report_status("Success" if sap_status == "Done" else sap_error),
            })

            # Mark email as read + move after all candidates processed
            try:
                requests.patch(
                    f"https://graph.microsoft.com/v1.0/users/{INBOX_EMAIL}/messages/{msg_id}",
                    headers=_graph_headers(token),
                    json={"isRead": True},
                    timeout=15,
                )
                move_message_to_processed(token, msg_id)
            except Exception as e:
                log.warning(f"Mark read / move failed: {e}")

            # ── Send report per email to the recruiter who sent it ──
            if results_log:
                report_user = {"email": from_email, "name": from_email, "access_token": ""}
                ok, msg_result = send_upload_notification(
                    access_token="",
                    user=report_user,
                    results=results_log,
                    submit_mode=SUBMIT_TO_SAP,
                    attachments=failed_upload_attachments,
                    cc=os.environ.get("EMAIL_CC", "").split(",") if os.environ.get("EMAIL_CC") else [],
                )
                if ok:
                    log.info(f"📧 Report sent to {from_email}")
                else:
                    log.warning(f"Report not sent: {msg_result}")
                results_log = []  # reset for next email
                failed_upload_attachments = []  # reset for next email

        # ── 6. Quit SAP bot ──────────────────────────────────────
        if bot:
            try:
                bot.quit()
            except Exception:
                pass

        elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
        log.info(
            f"Run complete in {elapsed:.1f}s — "
            f"candidates={summary['candidates']} done={summary['done']} "
            f"skipped={summary['skipped']} failed={summary['failed']}"
        )
        log.info("=" * 60)
        return summary

    # ── 7. Send report email ──────────────────────────────────
        # ── end of per-candidate loop ──

        # Send report per email, to the sender of that specific email
        if results_log:
            report_user = {"email": from_email, "name": from_email, "access_token": ""}
            ok, msg_result = send_upload_notification(
                access_token="",
                user=report_user,
                results=results_log,
                submit_mode=SUBMIT_TO_SAP,
                attachments=failed_upload_attachments,
                cc=os.environ.get("EMAIL_CC", "").split(",") if os.environ.get("EMAIL_CC") else [],
            )
            if ok:
                log.info(f"📧 Report sent to {from_email}")
            else:
                log.warning(f"Report not sent: {msg_result}")
            results_log = []  # reset for next email
            failed_upload_attachments = []  # reset for next email

    elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
    log.info(
        f"Run complete in {elapsed:.1f}s — "
        f"candidates={summary['candidates']} done={summary['done']} "
        f"skipped={summary['skipped']} failed={summary['failed']}"
    )
    log.info("=" * 60)
    return summary


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_pipeline()
