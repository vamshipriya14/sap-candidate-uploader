import re
import sys
import os
from datetime import date

import pandas as pd
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
    update_resume_record_fields,
    download_resume,
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


def _get_default_signature_template(user_dict: dict) -> str:
    name = user_dict.get("name", "Name")
    job_title = user_dict.get("job_title") or "job_title"
    email = user_dict.get("email", "Email")
    phone = user_dict.get("phone") or "+91 0000000000"

    _logo = "data:image/jpeg;base64,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"

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
            _social_icon("https://www.youtube.com/channel/UCmSl5A2JfguK3PtcUdiI8-A", "YouTube", "#FF0000",
                         short_label="&#9654;")
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


def build_email_body(recruiter_name: str, job_title: str) -> str:
    return (
        f"Hi {recruiter_name or 'Team'},\n\n"
        f"Please find attached profiles for {job_title}\n\n"
        f"Regards,"
    )


def update_email_body_greeting(body_text: str, recruiter_name: str) -> str:
    greeting = f"Hi {recruiter_name or 'Team'},"
    body = str(body_text or "")
    if re.match(r"^Hi\s+.*?,", body):
        return re.sub(r"^Hi\s+.*?,", greeting, body, count=1)
    return f"{greeting}\n\n{body}" if body else greeting


def _pending_candidate_editor_key(selected_jr: str) -> str:
    return f"edp_candidates_editor_{selected_jr}"


# ── signature ─────────────────────────────────────────────────────────────────

if "user_signature_edp" not in st.session_state:
    try:
        db_sig = get_user_signature(user["email"])
        st.session_state.user_signature_edp = db_sig or _get_default_signature_template(user)
    except Exception:
        st.session_state.user_signature_edp = _get_default_signature_template(user)

with st.expander("Manage Your Email Signature", expanded=False):
    if "sig_name_edp" not in st.session_state:
        st.session_state.sig_name_edp = pretty_user_name(user)
    if "sig_job_title_edp" not in st.session_state:
        st.session_state.sig_job_title_edp = user.get("job_title") or ""
    if "sig_phone_edp" not in st.session_state:
        st.session_state.sig_phone_edp = user.get("phone") or ""

    sig_col1, sig_col2 = st.columns([1, 1], gap="large")
    with sig_col1:
        st.caption("Fill in your details below. The signature preview updates automatically.")
        st.markdown("**Your Details**")
        sig_name = st.text_input("Full Name", value=st.session_state.sig_name_edp, key="sig_name_input_edp")
        sig_job_title = st.text_input("Job Title", value=st.session_state.sig_job_title_edp,
                                      key="sig_job_title_input_edp")
        sig_phone = st.text_input("Phone Number", value=st.session_state.sig_phone_edp, key="sig_phone_input_edp")
        preview_html = _get_default_signature_template(
            {**user, "name": sig_name or pretty_user_name(user), "job_title": sig_job_title, "phone": sig_phone}
        )
        save_col1, save_col2 = st.columns(2)
        with save_col1:
            if st.button("Save Signature", use_container_width=True):
                try:
                    st.session_state.sig_name_edp = sig_name
                    st.session_state.sig_job_title_edp = sig_job_title
                    st.session_state.sig_phone_edp = sig_phone
                    save_user_signature(user["email"], preview_html)
                    st.session_state.user_signature_edp = preview_html
                    st.success("Signature saved")
                except Exception as e:
                    st.error(f"Failed to save: {e}")
        with save_col2:
            if st.button("Reset Fields", use_container_width=True):
                st.session_state.sig_name_edp = pretty_user_name(user)
                st.session_state.sig_job_title_edp = user.get("job_title") or ""
                st.session_state.sig_phone_edp = user.get("phone") or ""
                st.rerun()
    with sig_col2:
        st.markdown("**Signature Preview**")
        components.html(
            f"""<div style="font-family:Arial,sans-serif; padding:4px;">
                <p style="font-size:12px; color:#888; margin:0 0 8px 0;">— Regards,</p>
                {preview_html}
            </div>""",
            height=165,
            scrolling=False,
        )

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

# ── group by JR Number ────────────────────────────────────────────────────────

grouped: dict[str, list[dict]] = {}
for rec in unsent_records:
    jr = str(rec.get("jr_number", "")).strip()
    grouped.setdefault(jr, []).append(rec)

# ── recruiter filter ──────────────────────────────────────────────────────────

all_recruiters = sorted({
    str(rec.get("recruiter_email", "")).strip()
    for recs in grouped.values()
    for rec in recs
    if str(rec.get("recruiter_email", "")).strip()
})

current_user_email = user.get("email", "").strip()
default_recruiter = current_user_email if current_user_email in all_recruiters else "All"
recruiter_options = ["All"] + all_recruiters

if "edp_recruiter_filter" not in st.session_state:
    st.session_state.edp_recruiter_filter = default_recruiter

selected_recruiter = st.selectbox(
    "Filter by Recruiter",
    options=recruiter_options,
    key="edp_recruiter_filter",
    help="Defaults to your own pending emails. Select 'All' to see everyone's.",
)

# Apply filter
if selected_recruiter != "All":
    grouped = {
        jr: [r for r in recs if str(r.get("recruiter_email", "")).strip() == selected_recruiter]
        for jr, recs in grouped.items()
    }
    grouped = {jr: recs for jr, recs in grouped.items() if recs}

if not grouped:
    st.info(f"No pending emails found for **{selected_recruiter}**.")
    st.stop()

# ── build JR dropdown ─────────────────────────────────────────────────────────

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
recruiter_name_default = _safe(meta.get("client_recruiter"))
recruiter_email_default = _safe(meta.get("client_recruiter_email")) or _safe(meta.get("recruiter_email"))

for _r in rows_for_jr:
    if not recruiter_name_default:
        recruiter_name_default = _safe(_r.get("client_recruiter"))
    if not recruiter_email_default:
        recruiter_email_default = _safe(_r.get("client_recruiter_email")) or _safe(_r.get("recruiter_email"))
    if recruiter_name_default and recruiter_email_default:
        break

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
        "_resume_path": str(rec.get("resume_path", "")),
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
        st.session_state[f"edp_body_{selected_jr}"] = update_email_body_greeting(
            st.session_state.get(f"edp_body_{selected_jr}", d["body"]),
            selected_client_recruiter_name,
        )
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
editor_key = _pending_candidate_editor_key(selected_jr)
edited_display_df = st.data_editor(
    display_df,
    key=editor_key,
    width="stretch",
    hide_index=True,
    disabled=["JR Number", "Date", "Skill", "Email ID"],
)

# Merge edits into candidate_rows so Send Email always uses latest values,
# but do NOT auto-save to DB on every cell change.
has_edits = not edited_display_df.equals(display_df)
changed_candidate_rows = []
for original_row, edited_row in zip(candidate_rows, edited_display_df.to_dict(orient="records")):
    merged_row = original_row.copy()
    for field, value in edited_row.items():
        merged_row[field] = "" if pd.isna(value) else str(value).strip()
    changed_candidate_rows.append(merged_row)
candidate_rows = changed_candidate_rows

# Explicit save button — only writes to DB when user is done editing
save_col, _ = st.columns([1, 4])
with save_col:
    save_btn = st.button(
        "💾 Save Table Changes",
        disabled=not has_edits,
        help="Persist edits to the database. Unsaved changes are still included when sending the email.",
    )

if save_btn:
    changed_count = 0
    for changed_row in changed_candidate_rows:
        record_id = str(changed_row.get("_record_id", "")).strip()
        if not record_id:
            continue
        update_resume_record_fields(
            record_id,
            {
                "candidate_name": changed_row.get("Candidate Name", ""),
                "phone": changed_row.get("Contact Number", ""),
                "current_company": changed_row.get("Current Company", ""),
                "total_experience": changed_row.get("Total Experience", ""),
                "relevant_experience": changed_row.get("Relevant Experience", ""),
                "current_ctc": changed_row.get("Current CTC", ""),
                "expected_ctc": changed_row.get("Expected CTC", ""),
                "notice_period": changed_row.get("Notice Period", ""),
                "current_location": changed_row.get("Current Location", ""),
                "preferred_location": changed_row.get("Preferred Location", ""),
                "comments_availability": changed_row.get("comments/Availability", ""),
            },
        )
        changed_count += 1
    if changed_count:
        st.success(f"✅ Saved {changed_count} candidate row(s) to database.")

# ── send ──────────────────────────────────────────────────────────────────────

if st.button("Send Email", type="primary", use_container_width=True):
    required_draft_fields = {
        "JR Number": selected_jr,
        "Client Recruiter Name": recruiter_name,
        "Email To": email_to,
        "CC": cc_value,
        "Subject": subject,
        "Email Body": body_text,
    }
    missing_draft_fields = [name for name, value in required_draft_fields.items() if not str(value).strip()]
    if missing_draft_fields:
        st.error(f"Cannot send email. Missing draft fields: {', '.join(missing_draft_fields)}")
        st.stop()

    required_candidate_fields = list(display_df.columns)
    missing_candidate_messages = []
    for idx, row in enumerate(candidate_rows, start=1):
        missing_fields = [field for field in required_candidate_fields if not str(row.get(field, "")).strip()]
        if missing_fields:
            candidate_label = str(row.get("Candidate Name", "")).strip() or f"row {idx}"
            missing_candidate_messages.append(f"{candidate_label}: {', '.join(missing_fields)}")
    if missing_candidate_messages:
        st.error(
            "Cannot send email. Candidate table has missing values in: "
            + "; ".join(missing_candidate_messages)
        )
        st.stop()

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

    # Download resumes from Supabase Storage
    attachments = []
    missing_files = []
    with st.spinner("Preparing attachments..."):
        for rec in rows_for_jr:
            fname = str(rec.get("file_name", "")).strip()
            link = str(rec.get("resume_path", "")).strip()
            if not fname:
                continue
            try:
                content = download_resume(link) if link else None
            except Exception:
                content = None
            if content:
                attachments.append({"name": fname, "content": content})
            else:
                missing_files.append(fname)

    if missing_files:
        st.warning(
            f"Could not download {len(missing_files)} file(s): {', '.join(missing_files)}. "
            "Check that the file exists in Supabase Storage."
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