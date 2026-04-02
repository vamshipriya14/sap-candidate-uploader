import base64
import re
import sys
import os
from datetime import date

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

# Ensure src/ is on the path when running as a page
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import require_login, show_navigation, show_user_profile
from notifier import send_client_email
from resume_repository import (
    fetch_active_jr_master,
    fetch_unsent_email_records,
    get_user_signature,
    mark_client_email_sent,
    save_user_signature,
)

st.set_page_config(page_title="Pending Client Emails", page_icon="📧", layout="wide")

# 🔥 HIDE DEFAULT STREAMLIT PAGE NAVIGATION
st.markdown("""
<style>
[data-testid="stSidebarNav"] {
    display: none;
}
</style>
""", unsafe_allow_html=True)
user = require_login()
show_user_profile(user)
show_navigation("pending_emails")

st.title("Pending Client Emails")
st.caption(f"Logged in as **{user['name']}** ({user['email']})")

# ── helpers ──────────────────────────────────────────────────────────────────

def _safe(val) -> str:
    """Return stripped string, treating None/falsy as empty."""
    return str(val).strip() if val else ""


def pretty_user_name(u: dict) -> str:
    display = (u.get("name") or "").strip()
    if display and "@" not in display:
        return " ".join(part.capitalize() for part in display.replace(".", " ").split())
    email = (u.get("email") or "").split("@", 1)[0]
    return " ".join(part.capitalize() for part in email.replace(".", " ").replace("_", " ").split())


def build_email_body(recruiter_name: str, job_title: str) -> str:
    return (
        f"Hi {recruiter_name or 'Team'},\n\n"
        f"Please find attached profiles for {job_title}\n\n"
        f"Regards,"
    )


def _download_resume(access_token: str, resume_link: str, retries: int = 3) -> bytes | None:
    """
    Download a resume from OneDrive/SharePoint via Microsoft Graph API.
    Strategy 1: /me/drive/root:/{path}:/content  (personal OneDrive path)
    Strategy 2: shares driveItem @microsoft.graph.downloadUrl (pre-auth URL, no token needed)
    Strategy 3: raw GET with Authorization header
    """
    import urllib.parse as _up
    import re as _re
    import time as _time

    if not resume_link:
        return None

    headers = {"Authorization": f"Bearer {access_token}"}

    for attempt in range(retries):
        try:
            # Strategy 1: personal OneDrive path
            personal_match = _re.search(
                r"/personal/[^/]+/Documents/(.+)$", _up.unquote(resume_link)
            )
            if personal_match:
                relative_path = personal_match.group(1)
                encoded_path  = _up.quote(relative_path)
                graph_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{encoded_path}:/content"
                resp = requests.get(graph_url, headers=headers, timeout=30, allow_redirects=True)
                if resp.status_code == 200:
                    return resp.content

            # Strategy 2: pre-authenticated download URL via shares endpoint
            try:
                share_token = "u!" + base64.urlsafe_b64encode(
                    resume_link.encode("utf-8")
                ).decode("utf-8").rstrip("=")
                meta_url = f"https://graph.microsoft.com/v1.0/shares/{share_token}/driveItem"
                meta_resp = requests.get(
                    meta_url,
                    headers={**headers, "Prefer": "redeemSharingLink"},
                    params={"$select": "@microsoft.graph.downloadUrl"},
                    timeout=30,
                )
                if meta_resp.status_code == 200:
                    dl_url = meta_resp.json().get("@microsoft.graph.downloadUrl", "")
                    if dl_url:
                        dl_resp = requests.get(dl_url, timeout=30, allow_redirects=True)
                        if dl_resp.status_code == 200:
                            return dl_resp.content
            except Exception:
                pass

            # Strategy 3: raw GET with auth header
            resp = requests.get(resume_link, headers=headers, timeout=30, allow_redirects=True)
            if resp.status_code == 200:
                return resp.content

        except Exception:
            pass

        _time.sleep(2 * (attempt + 1))

    return None


# ── fetch data ────────────────────────────────────────────────────────────────

try:
    jr_master_rows = fetch_active_jr_master()
except Exception as e:
    jr_master_rows = []
    st.warning(f"JR master lookup unavailable: {e}")

jr_master_by_number: dict[str, dict] = {}
for r in jr_master_rows:
    jn = str(r.get("jr_no", "")).strip()
    if jn:
        jr_master_by_number[jn] = r


def _jr_recruiter_email(row: dict) -> str:
    return _safe(row.get("client_recruiter_email")) or _safe(row.get("recruiter_email"))


def _sync_pending_recruiter_fields(selected_jr: str, recruiter_email_by_name: dict) -> None:
    recruiter_name = _safe(st.session_state.get(f"edp_rec_{selected_jr}", ""))
    recruiter_email = _safe(recruiter_email_by_name.get(recruiter_name, ""))
    if recruiter_email:
        st.session_state[f"edp_to_{selected_jr}"] = recruiter_email


recruiter_email_by_name = {}
for r in jr_master_rows:
    recruiter_name = _safe(r.get("client_recruiter"))
    recruiter_email = _jr_recruiter_email(r)
    if recruiter_name and recruiter_email and recruiter_name not in recruiter_email_by_name:
        recruiter_email_by_name[recruiter_name] = recruiter_email

try:
    unsent_records = fetch_unsent_email_records()
except Exception as e:
    st.error(f"Could not fetch pending email records: {e}")
    st.stop()

if not unsent_records:
    st.info("No pending client emails — all uploaded candidates have been emailed.")
    st.stop()

# ── signature ────────────────────────────────────────────────────────────────

if "user_signature_edp" not in st.session_state:
    try:
        db_sig = get_user_signature(user["email"])
        st.session_state.user_signature_edp = db_sig or ""
    except Exception:
        st.session_state.user_signature_edp = ""

# ── group by JR Number ────────────────────────────────────────────────────────

grouped: dict[str, list[dict]] = {}
for rec in unsent_records:
    jr = str(rec.get("jr_number", "")).strip()
    grouped.setdefault(jr, []).append(rec)

# Build dropdown options: "JR001 - Skill Name" or just "JR001"
jr_dropdown_options: list[str] = []
for jr in sorted(grouped.keys()):
    skill = ""
    if jr in jr_master_by_number:
        skill = str(jr_master_by_number[jr].get("skill_name", "")).strip()
    jr_dropdown_options.append(f"{jr} - {skill}" if skill else jr)

# ── draft email state init ────────────────────────────────────────────────────

if "edp_selected_jr_display" not in st.session_state:
    st.session_state.edp_selected_jr_display = jr_dropdown_options[0] if jr_dropdown_options else ""
if "edp_send_status" not in st.session_state:
    st.session_state.edp_send_status = ""

# ── JR selector ───────────────────────────────────────────────────────────────

selected_display = st.selectbox(
    "Select JR Number to email",
    options=jr_dropdown_options,
    key="edp_selected_jr_display",
    help="Only JRs where SAP upload is Done and email has not been sent are shown.",
)

# Extract actual jr_no from display label
selected_jr = selected_display.split(" - ")[0].strip() if selected_display else ""
rows_for_jr = grouped.get(selected_jr, [])

if not rows_for_jr:
    st.warning("No records found for selected JR.")
    st.stop()

# ── build draft fields ────────────────────────────────────────────────────────

meta = jr_master_by_number.get(selected_jr, {})
job_title = str(meta.get("skill_name", "")).strip()
# Scan ALL records for this JR to find any non-empty recruiter name/email
# (one record might have it even if others don't)
recruiter_name_default = _safe(meta.get("client_recruiter"))
recruiter_email_default = _safe(meta.get("client_recruiter_email")) or _safe(meta.get("recruiter_email"))

for _r in rows_for_jr:
    if not recruiter_name_default:
        recruiter_name_default = _safe(_r.get("client_recruiter"))
    if not recruiter_email_default:
        recruiter_email_default = _safe(_r.get("client_recruiter_email")) or _safe(_r.get("recruiter_email"))
    if recruiter_name_default and recruiter_email_default:
        break  # found both, no need to keep scanning

file_names = [str(r.get("file_name", "")).strip() for r in rows_for_jr if r.get("file_name")]

draft_key = f"edp_draft_{selected_jr}"
if draft_key not in st.session_state:
    st.session_state[draft_key] = {
        "recruiter_name": recruiter_name_default,
        "email_to": recruiter_email_default,
        "cc": "rec_team@volibits.com",
        "subject": f"BS: {job_title}" if job_title else "BS: ",
        "body": build_email_body(recruiter_name_default, job_title),
    }
d = st.session_state[draft_key]

# ── candidate table ───────────────────────────────────────────────────────────

today_text = date.today().strftime("%d-%b-%Y")
candidate_rows = []
seen = set()
for rec in rows_for_jr:
    email_id = str(rec.get("email", "")).strip()
    phone = str(rec.get("phone", "")).strip()
    pk = (email_id, phone)
    if pk in seen:
        continue
    seen.add(pk)
    candidate_rows.append({
        "JR Number": selected_jr,
        "Date": str(rec.get("date_text", "") or today_text),
        "Skill": str(rec.get("skill", "") or job_title),
        "Candidate Name": str(rec.get("candidate_name", "")).strip(),
        "Contact Number": phone,
        "Email ID": email_id,
        "Current Company": str(rec.get("current_company", "")),
        "Total Experience": str(rec.get("total_experience", "")),
        "Relevant Experience": str(rec.get("relevant_experience", "")),
        "Current CTC": str(rec.get("current_ctc", "")),
        "Expected CTC": str(rec.get("expected_ctc", "")),
        "Notice Period": str(rec.get("notice_period", "")),
        "Current Location": str(rec.get("current_location", "")),
        "Preferred Location": str(rec.get("preferred_location", "")),
        "comments/Availability": str(rec.get("comments_availability", "")),
        "_record_id": str(rec.get("id", "")),
        "_resume_link": str(rec.get("resume_link", "")),
    })

# ── email form ────────────────────────────────────────────────────────────────

st.subheader("Email Details")
st.caption(
    f"{len(candidate_rows)} candidate(s) for **{selected_jr}**"
    + (f" — {job_title}" if job_title else "")
)

active_recruiters = sorted({
    str(r.get("client_recruiter", "")).strip()
    for r in jr_master_rows
    if str(r.get("client_recruiter", "")).strip()
})
if recruiter_name_default and recruiter_name_default not in active_recruiters:
    active_recruiters = sorted(active_recruiters + [recruiter_name_default])

col1, col2 = st.columns(2)
with col1:
    recruiter_name = st.selectbox(
        "Client Recruiter Name",
        options=active_recruiters or [recruiter_name_default or ""],
        index=active_recruiters.index(d["recruiter_name"]) if d["recruiter_name"] in active_recruiters else 0,
        key=f"edp_rec_{selected_jr}",
    )
    email_to_key = f"edp_to_{selected_jr}"
    stored_client_recruiter_name = _safe(d["recruiter_name"])
    selected_client_recruiter_name = _safe(recruiter_name)
    selected_client_recruiter_email = _safe(
        recruiter_email_by_name.get(selected_client_recruiter_name, "")
    )
    if (
        selected_client_recruiter_name
        and selected_client_recruiter_name != stored_client_recruiter_name
        and selected_client_recruiter_email
    ):
        st.session_state[email_to_key] = selected_client_recruiter_email
    email_to = st.text_input("Email To", value=d["email_to"], key=email_to_key)
    st.text_input("Email From", value=user.get("email", ""), disabled=True, key=f"edp_from_{selected_jr}")
with col2:
    st.text_input("JR Number", value=selected_jr, disabled=True)
    cc_value = st.text_input(
        "CC",
        value=d["cc"],
        key=f"edp_cc_{selected_jr}",
        help="Comma-separated. rec_team@volibits.com should remain included.",
    )
    subject = st.text_input("Subject", value=d["subject"], key=f"edp_subj_{selected_jr}")

body_text = st.text_area("Email Body", value=d["body"], height=160, key=f"edp_body_{selected_jr}")

# Update draft state so preview stays in sync
st.session_state[draft_key].update({
    "recruiter_name": recruiter_name,
    "email_to": email_to,
    "cc": cc_value,
    "subject": subject,
    "body": body_text,
})

# ── email preview ─────────────────────────────────────────────────────────────

st.subheader("Email Preview")
header_html = "".join([
    f"<div style='margin-bottom:4px;font-size:12px;color:#444;font-family:Arial,sans-serif;'>"
    f"<strong>{lbl}:</strong> {val}</div>"
    for lbl, val in [
        ("From", user.get("email", "")),
        ("To", email_to),
        ("CC", cc_value),
        ("Subject", subject),
    ]
])
body_html = body_text.replace("\n", "<br>")
signature_html = st.session_state.user_signature_edp or ""

components.html(
    f"""
    <div style="background:#f5f5f5;border:1px solid #ddd;border-radius:6px;
                padding:16px;font-family:Arial,sans-serif;font-size:13px;">
      <div style="border-bottom:1px solid #ddd;padding-bottom:10px;margin-bottom:12px;">{header_html}</div>
      <div style="color:#222;line-height:1.6;margin-bottom:16px;white-space:pre-line;">{body_html}</div>
      <div style="border-top:1px solid #eee;padding-top:12px;margin-top:8px;">{signature_html}</div>
    </div>
    """,
    height=320,
    scrolling=True,
)

display_df = pd.DataFrame([
    {k: v for k, v in row.items() if not k.startswith("_")}
    for row in candidate_rows
])
st.caption("Candidate table that will be included in email")
st.dataframe(display_df, width="stretch", hide_index=True)

# ── send ──────────────────────────────────────────────────────────────────────

if st.button("Send Email", type="primary", use_container_width=True):
    draft_payload = {
        "JR Number": selected_jr,
        "Job Title": job_title,
        "Client Recruiter Name": recruiter_name,
        "Email To": email_to,
        "CC": cc_value,
        "Email From": user.get("email", ""),
        "Subject": subject,
        "Email Body": body_text,
    }

    # Download resumes for attachments
    attachments = []
    missing_files = []
    with st.spinner("Preparing attachments..."):
        for rec in rows_for_jr:
            fname = str(rec.get("file_name", "")).strip()
            link = str(rec.get("resume_link", "")).strip()
            if not fname:
                continue
            content = _download_resume(user["access_token"], link)
            if content:
                attachments.append({"name": fname, "content": content})
            else:
                missing_files.append(fname)

    if missing_files:
        st.warning(
            f"Could not download {len(missing_files)} file(s): {', '.join(missing_files)}. "
            f"This is usually caused by an expired login session — try signing out and back in, then retry."
        )

    user_to_send = {**user, "signature": st.session_state.user_signature_edp or user.get("signature", "")}
    with st.spinner("Sending email..."):
        ok, msg = send_client_email(
            user=user_to_send,
            draft=draft_payload,
            candidate_rows=candidate_rows,
            attachments=attachments,
        )

    if ok:
        record_ids = [r["_record_id"] for r in candidate_rows if r.get("_record_id")]
        try:
            mark_client_email_sent(record_ids)
        except Exception as e:
            st.warning(f"Email sent but failed to update DB: {e}")
        st.session_state.edp_send_status = f"ok::{msg}"
        # Remove draft key so next visit re-initialises
        st.session_state.pop(draft_key, None)
        st.rerun()
    else:
        st.session_state.edp_send_status = f"err::{msg}"
        st.rerun()

if st.session_state.edp_send_status:
    state, text = st.session_state.edp_send_status.split("::", 1)
    if state == "ok":
        st.success(text)
    else:
        st.error(text)
