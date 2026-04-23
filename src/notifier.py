import base64
import hashlib
import os
import requests
from datetime import datetime
from dotenv import load_dotenv
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError


# Import resume_repository lazily to avoid circular imports if any
# from resume_repository import ... (not needed here yet)

load_dotenv()

import os

def _secret(name: str, *fallback_names: str) -> str:
    # 1. Try environment variables first (GitHub Actions)
    for key in (name, *fallback_names):
        value = os.environ.get(key)
        if value:
            return value

    # 2. Try Streamlit secrets (only in Streamlit context)
    try:
        import streamlit as st
        from streamlit.errors import StreamlitSecretNotFoundError
        for key in (name, *fallback_names):
            try:
                value = st.secrets.get(key)
                if value:
                    return str(value)
            except StreamlitSecretNotFoundError:
                pass
            except Exception:
                pass
    except Exception:
        pass

    return ""
TENANT_ID = _secret("MICROSOFT_TENANT_ID", "AZURE_TENANT_ID")
CLIENT_ID = _secret("MICROSOFT_CLIENT_ID", "AZURE_CLIENT_ID")
CLIENT_SECRET = _secret("MICROSOFT_CLIENT_SECRET", "AZURE_CLIENT_SECRET")
SENDER_EMAIL = _secret("SENDER_EMAIL")   # e.g. HEAD.HR@VOLIBITS.COM
REPORT_SENDER_EMAIL = _secret("INBOX_EMAIL")


def _friendly_graph_error(resp) -> str:
    try:
        data = resp.json()
    except Exception:
        data = {}

    error = data.get("error", {}) if isinstance(data, dict) else {}
    code = error.get("code")
    message = error.get("message")

    if resp.status_code == 403:
        return f"Email sending is not authorized for {SENDER_EMAIL}. {message or 'Access is denied.'}"
    if code or message:
        return f"Email sending failed: {message or code}"
    return f"Email sending failed with status {resp.status_code}"


def _get_app_token() -> str:
    """
    Get an app-level access token using client credentials flow.
    This does NOT require user approval — admin grants Mail.Send
    as an Application permission once in Azure portal.
    """
    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
        raise Exception("Missing AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET in .env")

    resp = requests.post(
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope":         "https://graph.microsoft.com/.default",
            "grant_type":    "client_credentials",
        }
    )
    data = resp.json()
    if "access_token" not in data:
        raise Exception(f"Token error: {data.get('error_description', data)}")
    return data["access_token"]


def pretty_user_name(u: dict) -> str:
    display = (u.get("name") or "").strip()
    if display and "@" not in display:
        return " ".join(part.capitalize() for part in display.replace(".", " ").split())
    email = (u.get("email") or "").split("@", 1)[0]
    return " ".join(part.capitalize() for part in email.replace(".", " ").replace("_", " ").split())


def _upload_report_status(status: str) -> str:
    status_text = str(status or "").strip()
    status_lower = status_text.lower()

    if status_text == "Success":
        return "Success"
    if "job id not found" in status_lower:
        return "Job id not found"
    if "job not found" in status_lower:
        return "Job id not found"
    if "requisition id" in status_lower and "not found" in status_lower:
        return "Job id not found"
    if "not found in job list" in status_lower:
        return "Job id not found"
    return "Failed"


def send_upload_notification(access_token, user, results, submit_mode, attachments=None, cc=None):
    """
    Sends an upload summary email via Microsoft Graph API using
    client credentials (app token) — no user approval popup needed.

    access_token param kept for API compatibility but not used here.
    """
    if not REPORT_SENDER_EMAIL:
        return False, "Upload report sender email is not configured."

    display_results = [
        {**r, "Status": _upload_report_status(r.get("Status", ""))}
        for r in results
    ]
    success    = [r for r in display_results if r["Status"] == "Success"]
    failed     = [r for r in display_results if r["Status"] != "Success"]
    mode_label = "Live Submit" if submit_mode else "Dry Run"
    timestamp  = datetime.now().strftime("%d %b %Y, %I:%M %p")

    # Build results table rows
    rows_html = ""
    for r in display_results:
        is_ok = r["Status"] == "Success"
        bg    = "#d4edda" if is_ok else "#f8d7da"
        icon  = "✅" if is_ok else "❌"
        rows_html += f"""
        <tr style='background:{bg}'>
            <td style='padding:8px 12px; border:1px solid #dee2e6'>{r['File']}</td>
            <td style='padding:8px 12px; border:1px solid #dee2e6'>{icon} {r['Status']}</td>
        </tr>"""

    summary_bg   = "#d4edda" if not failed else "#fff3cd"
    summary_text = (
        f"All {len(success)} candidates uploaded successfully! 🎉"
        if not failed
        else f"{len(success)} succeeded, {len(failed)} failed."
    )

    failed_note = ""
    if failed:
        failed_note = "<p style='margin-top:12px; color:#856404;'>Please refer to attached screenshots for more details.</p>"

    html_body = f"""
    <html><body style='font-family: Segoe UI, Arial, sans-serif; color: #333; max-width: 600px; margin: auto'>
        <div style='background:#0078d4; padding:24px; border-radius:8px 8px 0 0'>
            <h2 style='color:white; margin:0'>📄 SAP Upload Report</h2>
            <p style='color:#cce4ff; margin:4px 0 0'>{timestamp} &nbsp;·&nbsp; {mode_label}</p>
        </div>
        <div style='border:1px solid #dee2e6; border-top:none; padding:24px; border-radius:0 0 8px 8px'>
            <p>Hi <strong>{pretty_user_name(user)}</strong>,</p>
            <p>Here's the summary of your SAP upload session:</p>
            <div style='background:{summary_bg}; padding:12px 16px; border-radius:6px; margin:16px 0'>
                {summary_text}
            </div>
            {failed_note}
            <table style='width:100%; border-collapse:collapse; margin-top:16px'>
                <thead>
                    <tr style='background:#f8f9fa'>
                        <th style='padding:8px 12px; border:1px solid #dee2e6; text-align:left'>File</th>
                        <th style='padding:8px 12px; border:1px solid #dee2e6; text-align:left'>Status</th>
                    </tr>
                </thead>
                <tbody>{rows_html}</tbody>
            </table>
            <p style='color:gray; font-size:12px; margin-top:24px'>
                Sent automatically by the Resume → SAP Upload tool.
            </p>
        </div>
    </body></html>
    """

    try:
        token = _get_app_token()
        graph_attachments = []
        seen_attachment_keys = set()
        for attachment in attachments or []:
            name = str(attachment.get("name", "")).strip()
            content = attachment.get("content")
            if not name or content is None:
                continue
            content_bytes = bytes(content)
            attachment_key = hashlib.sha256(content_bytes).hexdigest()
            if attachment_key in seen_attachment_keys:
                continue
            seen_attachment_keys.add(attachment_key)
            graph_attachments.append(
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": name,
                    "contentBytes": base64.b64encode(content_bytes).decode("ascii"),
                }
            )

        payload = {
            "message": {
                "subject": f"SAP Upload Report — {len(success)}/{len(results)} succeeded ({mode_label})",
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [
                    {"emailAddress": {"address": user["email"], "name": user["name"]}}
                ],
                "attachments": graph_attachments,
            },
            "saveToSentItems": True
        }

        if cc:
            payload["message"]["ccRecipients"] = [
                {"emailAddress": {"address": email}} for email in cc
            ]


        # Send upload report from the fixed hrvolibot mailbox.
        resp = requests.post(
            f"https://graph.microsoft.com/v1.0/users/{REPORT_SENDER_EMAIL}/sendMail",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            json=payload
        )

        if resp.status_code == 202:
            print(f"✅ Email sent to {user['email']}")
            return True, "Email sent successfully"
        else:
            return False, _friendly_graph_error(resp)

    except Exception as e:
        return False, str(e)


def _parse_recipients(raw: str) -> list:
    items = []
    for part in str(raw or "").replace(";", ",").split(","):
        email = part.strip()
        if email and "@" in email and email not in items:
            items.append(email)
    return items


def _html_escape(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_body_with_table(body_text: str, table_html: str) -> str:
    if not table_html:
        return _html_escape(body_text).replace("\n", "<br>")

    lines = str(body_text or "").splitlines()
    regards_index = next((idx for idx, line in enumerate(lines) if line.strip().lower().startswith("regards")), None)
    if regards_index is None:
        escaped = _html_escape(body_text).replace("\n", "<br>")
        return f"{escaped}<br>{table_html}"

    before = "\n".join(lines[:regards_index]).strip()
    after = "\n".join(lines[regards_index:]).strip()

    parts = []
    if before:
        parts.append(_html_escape(before).replace("\n", "<br>"))
    parts.append(table_html)
    if after:
        parts.append(_html_escape(after).replace("\n", "<br>"))
    return "<br>".join(parts)


def send_client_email(user: dict, draft: dict, candidate_rows: list, attachments: list | None = None):
    sender_email = str(user.get("email", "")).strip()
    if not sender_email:
        return False, "Logged-in user email is missing."

    to_list = _parse_recipients(draft.get("Email To", ""))
    if not to_list:
        return False, "Email To is empty. Recruiter email was not extracted."

    cc_input = draft.get("CC", "")
    cc_list = _parse_recipients(cc_input)
    # Removing forced inclusion of rec_team@volibits.com to allow user removal.
    # It should be added as a default in the draft generation instead.
    # if "rec_team@volibits.com" not in [x.lower() for x in cc_list]:
      #  cc_list.insert(0, "rec_team@volibits.com")


    subject = str(draft.get("Subject", "")).strip() or "BS:"
    body_text = str(draft.get("Email Body", "")).strip()

    rows_html = ""
    for row in candidate_rows:
        values = [
            row.get("JR Number", ""),
            row.get("Date", ""),
            row.get("Skill", ""),
            row.get("Candidate Name", ""),
            row.get("Contact Number", ""),
            row.get("Email ID", ""),
            row.get("Current Company", ""),
            row.get("Total Experience", ""),
            row.get("Relevant Experience", ""),
            row.get("Current CTC", ""),
            row.get("Expected CTC", ""),
            row.get("Notice Period", ""),
            row.get("Current Location", ""),
            row.get("Preferred Location", ""),
            row.get("comments/Availability", ""),
        ]
        tds = "".join(f"<td style='padding:6px;border:1px solid #ddd'>{_html_escape(v)}</td>" for v in values)
        rows_html += f"<tr>{tds}</tr>"

    table_html = ""
    if rows_html:
        headers = [
            "JR No",
            "Date",
            "Skill",
            "Candidate Name",
            "Contact Number",
            "Email ID",
            "Current Company",
            "Total Experience",
            "Relevant Experience",
            "Current CTC",
            "Expected CTC",
            "Notice Period",
            "Current Location",
            "Preferred Location",
            "comments/Availability",
        ]
        ths = "".join(f"<th style='padding:8px 6px;border:1px solid #0056a0;background:#0078d4;color:#ffffff;font-weight:600'>{h}</th>" for h in headers)
        table_html = (
            "<table style='border-collapse:collapse;font-family:Segoe UI,Arial,sans-serif;font-size:12px;margin-top:16px'>"
            f"<thead><tr>{ths}</tr></thead><tbody>{rows_html}</tbody></table>"
        )
    body_html = _build_body_with_table(body_text, table_html)
    user_signature = user.get("signature")
    if user_signature:
        # Check if signature is plain text or HTML. Simple heuristic: look for tags.
        if "<" not in user_signature:
             user_signature = _html_escape(user_signature).replace("\n", "<br>")
        # If the body already ends with "Regards,", we don't want a huge gap.
        if body_html.strip().endswith("Regards,") or body_html.strip().endswith("Regards"):
            body_html += f"<br>{user_signature}"
        else:
            body_html += f"<br><br>{user_signature}"

    html = f"""
    <html><body style='font-family:Segoe UI,Arial,sans-serif;color:#222'>
    <div>{body_html}</div>
    </body></html>
    """

    graph_attachments = []
    for attachment in attachments or []:
        name = str(attachment.get("name", "")).strip()
        content = attachment.get("content")
        if not name or content is None:
            continue
        graph_attachments.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name,
                "contentBytes": base64.b64encode(content).decode("ascii"),
            }
        )

    try:
        token = _get_app_token()
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html},
                "toRecipients": [{"emailAddress": {"address": e}} for e in to_list],
                "ccRecipients": [{"emailAddress": {"address": e}} for e in cc_list],
                "attachments": graph_attachments,
            },
            "saveToSentItems": True,
        }
        resp = requests.post(
            f"https://graph.microsoft.com/v1.0/users/{sender_email}/sendMail",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code == 202:
            return True, "Client email sent"
        return False, _friendly_graph_error(resp)
    except Exception as e:
        return False, f"Client email send failed: {e}"
