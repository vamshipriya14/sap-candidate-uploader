"""
scheduler_form.py — Autonomous SAP Pipeline for Form-Submitted Resumes
Triggered every 30 min by GitHub Actions (no Streamlit dependency).

This scheduler handles resumes submitted via the Resume_Upload.py form.
It is a PARALLEL pipeline to scheduler.py (which handles email inbox).

Flow:
  1. Fetch all Pending records from Supabase table
     (where upload_to_sap = 'Pending' AND source is form — not email)
  2. Download resume file from Supabase Storage
  3. Upload to SAP via headless browser
  4. Update upload_to_sap status in Supabase table
  5. Send notification email to the recruiter who submitted via form
"""

import io
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
SRC  = ROOT / "src"
sys.path.insert(0, str(SRC))

from notifier import send_upload_notification
from resume_repository import (
    _headers,
    download_resume,
    SUPABASE_URL,
    SUPABASE_TABLE,
)
from sap_bot_headless import SAPBot
from uploader import upload_to_sap

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("scheduler_form")

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
SUBMIT_TO_SAP  = os.environ.get("SCHEDULER_SUBMIT_TO_SAP", "true").lower() == "true"
MAX_RECORDS    = int(os.environ.get("SCHEDULER_MAX_RECORDS", "50"))
EMAIL_CC       = [e for e in os.environ.get("SCHEDULER_EMAIL_CC", "").split(",") if e.strip()]

NON_CRITICAL_SAP_ERRORS = ["requisition id", "not found in job list"]
DEAD_SESSION_ERRORS     = ["invalid session id", "no such session", "disconnected"]

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def _safe(val) -> str:
    return str(val).strip() if val else ""

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _patch_record(record_id: str, fields: dict) -> None:
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=eq.{record_id}",
        headers={**_headers(), "Prefer": "return=representation"},
        json=fields,
        timeout=15,
    )

def _start_bot() -> SAPBot:
    b = SAPBot()
    b.start()
    b.login()
    return b


def fetch_form_pending_records(limit: int = 50) -> list:
    """
    Fetch records submitted via the form that are still Pending.

    Key distinction from scheduler.py (email inbox):
        Email-submitted records have source_email_id populated.
        Form-submitted records have source_email_id IS NULL.
    """
    url = (
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
        f"?upload_to_sap=eq.Pending"
        f"&source_email_id=is.null"        # ← ONLY form-submitted rows
        f"&select=*"
        f"&limit={limit}"
    )
    resp = requests.get(url, headers=_headers(), timeout=30)
    if resp.status_code != 200:
        raise Exception(resp.text)
    return resp.json()


def _add_result(by_recruiter, recruiter_email, file_name, status, screenshots=None):
    if recruiter_email not in by_recruiter:
        by_recruiter[recruiter_email] = {"results": [], "screenshots": []}
    by_recruiter[recruiter_email]["results"].append({"File": file_name, "Status": status})
    if screenshots:
        by_recruiter[recruiter_email]["screenshots"].extend(screenshots)


# ─────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────
def run_pipeline() -> dict:
    run_start = datetime.now(timezone.utc)
    summary   = {
        "started_at": run_start.isoformat(),
        "total": 0, "done": 0, "skipped": 0, "failed": 0, "errors": [],
    }

    log.info("=" * 60)
    log.info(f"Form scheduler run started — submit_to_sap={SUBMIT_TO_SAP}")
    log.info(f"SUPABASE_URL={SUPABASE_URL[:20] if SUPABASE_URL else 'NOT SET'}...")

    # ── 1. Fetch Pending form-submitted records ───────────────
    try:
        pending = fetch_form_pending_records(limit=MAX_RECORDS)
    except Exception as e:
        log.error(f"Failed to fetch pending records: {e}")
        summary["errors"].append(f"Fetch records: {e}")
        return summary

    log.info(f"Found {len(pending)} pending form-submitted record(s)")
    summary["total"] = len(pending)

    if not pending:
        log.info("Nothing to process.")
        return summary

    # ── 2. Start SAP bot (with retry) ─────────────────────
    bot = None
    max_bot_retries = 2
    for attempt in range(max_bot_retries):
        try:
            bot = _start_bot()
            log.info("SAP bot connected ✅")
            break
        except Exception as e:
            if attempt < max_bot_retries - 1:
                log.warning(f"SAP bot failed (attempt {attempt + 1}), retrying…")
            else:
                log.error(f"SAP bot failed to start: {e}")
                summary["errors"].append(f"SAP start: {e}")

    # ── 3. Process each record ────────────────────────────────
    # Group by created_by so one notification goes per recruiter
    by_recruiter: dict[str, dict] = {}

    for record in pending:
        record_id   = _safe(record.get("id"))
        jr_no       = _safe(record.get("jr_number"))
        first_name  = _safe(record.get("first_name"))
        last_name   = _safe(record.get("last_name"))
        email       = _safe(record.get("email"))
        phone       = _safe(record.get("phone"))
        resume_path = _safe(record.get("resume_path"))
        file_name   = _safe(record.get("file_name"))
        created_by  = _safe(record.get("created_by"))    # recruiter who submitted
        cand_label  = f"{first_name} {last_name}".strip() or file_name

        log.info(f"  → {cand_label} | JR: {jr_no} | id: {record_id}")

        # ── 3a. Download resume from Supabase Storage ─────────
        file_bytes = None
        if resume_path:
            try:
                # Handle already-signed URLs
                if resume_path.startswith("/object/sign/") or resume_path.startswith("http"):
                    if resume_path.startswith("/"):
                        url = f"{SUPABASE_URL}{resume_path}"
                    else:
                        url = resume_path
                    resp = requests.get(url, timeout=30)
                    resp.raise_for_status()
                    file_bytes = resp.content
                else:
                    file_bytes = download_resume(resume_path)
                log.info(f"     Downloaded resume ({len(file_bytes):,} bytes)")
            except Exception as e:
                log.warning(f"     Resume download failed: {e}")

        # ── 3b. SAP upload ────────────────────────────────────
        if not bot:
            log.warning("     SAP bot unavailable — marking Skipped")
            _patch_record(record_id, {
                "upload_to_sap": "Skipped",
                "error_message": "SAP bot unavailable",
                "modified_at"  : _now_iso(),
            })
            summary["skipped"] += 1
            _add_result(by_recruiter, created_by, file_name, "SAP bot unavailable")
            continue

        sap_status = "Failed"
        sap_error  = ""
        failed_screenshots = []

        for attempt in range(2):
            try:
                file_obj = None
                if file_bytes:
                    file_obj      = io.BytesIO(file_bytes)
                    file_obj.name = file_name

                upload_to_sap(bot, {
                    "jr_number"  : jr_no,
                    "first_name" : first_name,
                    "last_name"  : last_name,
                    "email"      : email,
                    "phone"      : phone,
                    "resume_file": file_obj,
                    "submit"     : SUBMIT_TO_SAP,
                })
                sap_status = "Done"
                log.info(f"     ✅ SAP upload success: {cand_label}")
                break

            except Exception as e:
                sap_error = str(e)

                if any(err in sap_error.lower() for err in NON_CRITICAL_SAP_ERRORS):
                    sap_status = "Skipped"
                    log.warning(f"     ⚠ SAP skipped (non-critical): {sap_error}")
                    break

                if any(err in sap_error.lower() for err in DEAD_SESSION_ERRORS):
                    log.warning(f"     Session dead (attempt {attempt + 1}) — restarting bot…")
                    try: bot.quit()
                    except Exception: pass
                    try:
                        bot = _start_bot()
                        log.info("     SAP bot restarted.")
                    except Exception as re_err:
                        log.error(f"     Bot restart failed: {re_err}")
                        bot = None
                        break
                    continue

                # Capture screenshot on real failure
                try:
                    snap_name = f"{jr_no}_{cand_label}_attempt{attempt + 1}"
                    snap_path = bot._screenshot(snap_name)
                    failed_screenshots.append({
                        "name"   : f"{snap_name}.png",
                        "content": snap_path.read_bytes(),
                    })
                except Exception:
                    pass
                log.error(f"     ❌ SAP upload failed (attempt {attempt + 1}): {sap_error}")

        # ── 3c. Update Supabase table ─────────────────────────
        patch = {
            "upload_to_sap": sap_status,
            "modified_at"  : _now_iso(),
        }
        if sap_error:
            patch["error_message"] = sap_error[:500]

        try:
            _patch_record(record_id, patch)
            log.info(f"     DB updated → upload_to_sap = {sap_status}")
        except Exception as e:
            log.warning(f"     DB update failed: {e}")

        _add_result(
            by_recruiter, created_by, file_name,
            "Success" if sap_status == "Done" else sap_error[:100],
            screenshots=failed_screenshots,
        )

        if   sap_status == "Done":    summary["done"]    += 1
        elif sap_status == "Skipped": summary["skipped"] += 1
        else:                         summary["failed"]   += 1

    # ── 4. Quit SAP bot ───────────────────────────────────────
    if bot:
        try: bot.quit()
        except Exception: pass

    # ── 5. Send notification per recruiter ────────────────────
    for recruiter_email, info in by_recruiter.items():
        if not recruiter_email:
            log.warning(f"Skipping notification — no recruiter email (results: {info['results']})")
            continue
        report_user = {
            "email"       : recruiter_email,
            "name"        : recruiter_email,
            "access_token": "",
        }
        try:
            ok, msg = send_upload_notification(
                access_token="",
                user=report_user,
                results=info["results"],
                submit_mode=SUBMIT_TO_SAP,
                attachments=info["screenshots"],
                cc=EMAIL_CC or None,
            )
            if ok:
                log.info(f"📧 Notification sent to {recruiter_email}")
            else:
                log.warning(f"Notification failed for {recruiter_email}: {msg}")
        except Exception as e:
            log.warning(f"Notification exception for {recruiter_email}: {e}")

    elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
    log.info(
        f"Run complete in {elapsed:.1f}s — "
        f"total={summary['total']} done={summary['done']} "
        f"skipped={summary['skipped']} failed={summary['failed']}"
    )
    log.info("=" * 60)
    return summary


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_pipeline()
