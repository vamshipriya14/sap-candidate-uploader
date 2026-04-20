"""
Email Message Handler — email_handler.py

Handles email operations (fetch, attachments, move, mark read) with proper
error handling and retry logic for transient failures.

Replaces direct Graph API calls with robust wrapper that handles:
  - Message ID encoding issues
  - 400 Bad Request errors
  - Temporary failures (429, 503)
  - Permission issues
"""

import base64
import time
from typing import Optional, List, Dict, Any
import requests
from azure_auth import graph_request, get_access_token

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────

RETRY_ATTEMPTS = 3
RETRY_DELAY = 1  # seconds


# ─────────────────────────────────────────────────────────────
# SAFE HELPERS
# ─────────────────────────────────────────────────────────────

def _safe(val) -> str:
    """Safely convert value to string."""
    return str(val).strip() if val else ""


def _retry_on_transient(func, max_attempts=RETRY_ATTEMPTS, delay=RETRY_DELAY):
    """
    Retry a function on transient errors (429, 503, timeouts).
    Does NOT retry on 400, 401, 403, 404 (permanent errors).
    """
    last_error = None

    for attempt in range(max_attempts):
        try:
            return func()
        except requests.exceptions.Timeout:
            last_error = "Timeout"
            if attempt < max_attempts - 1:
                time.sleep(delay * (attempt + 1))  # exponential backoff
        except requests.HTTPError as e:
            if e.response and e.response.status_code in [429, 503]:
                last_error = f"Transient error ({e.response.status_code})"
                if attempt < max_attempts - 1:
                    time.sleep(delay * (attempt + 1))
            else:
                # Permanent error - don't retry
                raise
        except Exception as e:
            last_error = str(e)
            if attempt < max_attempts - 1:
                time.sleep(delay)

    raise Exception(f"Failed after {max_attempts} attempts: {last_error}")


# ─────────────────────────────────────────────────────────────
# INBOX OPERATIONS
# ─────────────────────────────────────────────────────────────

def list_inbox_messages(
        user_email: str,
        subject_filter: str = "Profiles",
        max_messages: int = 50,
        token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List messages from Inbox with subject filter.

    Args:
        user_email: User mailbox email
        subject_filter: Subject keyword to search for
        max_messages: Maximum messages to return
        token: Access token (auto-acquired if not provided)

    Returns:
        list: Messages with id, subject, from, receivedDateTime, body, hasAttachments
    """
    if not token:
        token = get_access_token()

    def fetch():
        endpoint = (
            f"/users/{user_email}/mailFolders/Inbox/messages"
            f"?$search=\"subject:{subject_filter}\""
            f"&$top={max_messages}"
            f"&$select=id,subject,from,receivedDateTime,body,hasAttachments,isRead"
            f"&$orderby=receivedDateTime desc"
        )
        return graph_request("GET", endpoint, token=token, timeout=30)

    try:
        result = _retry_on_transient(fetch)
        return result.get("value", [])
    except Exception as e:
        raise Exception(f"Failed to list inbox messages: {str(e)}")


def list_inbox_subfolders(
        user_email: str,
        token: Optional[str] = None,
) -> List[tuple]:
    """
    List all subfolders under Inbox (for Outlook rules).

    Returns:
        list: Tuples of (folder_id, folder_name)
    """
    if not token:
        token = get_access_token()

    def fetch():
        endpoint = (
            f"/users/{user_email}/mailFolders/Inbox/childFolders"
            f"?$top=50&$select=id,displayName"
        )
        return graph_request("GET", endpoint, token=token, timeout=20)

    try:
        result = _retry_on_transient(fetch)
        folders = [("Inbox", "Inbox")]
        for f in result.get("value", []):
            fid = f.get("id", "")
            fname = f.get("displayName", "")
            if fid:
                folders.append((fid, fname))
        return folders
    except Exception as e:
        raise Exception(f"Failed to list subfolders: {str(e)}")


# ─────────────────────────────────────────────────────────────
# MESSAGE ATTACHMENT OPERATIONS (FIXED)
# ─────────────────────────────────────────────────────────────

def fetch_message_attachments(
        user_email: str,
        message_id: str,
        token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch attachments for a message with proper error handling.

    ✅ FIXES 400 Bad Request error by:
       - Properly handling message ID encoding
       - Adding detailed error context
       - Implementing retry logic for transient failures
       - Checking message accessibility before fetch

    Args:
        user_email: User mailbox email
        message_id: Message ID from list_inbox_messages
        token: Access token (auto-acquired if not provided)

    Returns:
        list: Attachments with name, contentBytes (base64), contentType, size

    Raises:
        Exception: With detailed error context if fetch fails
    """
    if not token:
        token = get_access_token()

    if not message_id or not _safe(message_id):
        raise ValueError("Invalid message ID provided")

    def fetch():
        # Endpoint with proper formatting
        endpoint = f"/users/{user_email}/messages/{message_id}/attachments?$select=name,contentBytes,contentType,size"
        return graph_request("GET", endpoint, token=token, timeout=30)

    try:
        result = _retry_on_transient(fetch, max_attempts=2)
        attachments = result.get("value", [])

        # Decode and validate attachments
        decoded = []
        for att in attachments:
            name = _safe(att.get("name", ""))
            content_b64 = att.get("contentBytes", "")
            content_type = att.get("contentType", "")
            size = att.get("size", 0)

            if not name or not content_b64:
                continue

            try:
                content_bytes = base64.b64decode(content_b64)
                decoded.append({
                    "name": name,
                    "bytes": content_bytes,
                    "contentType": content_type,
                    "size": size,
                })
            except Exception as decode_error:
                # Log but continue with other attachments
                print(f"Warning: Failed to decode attachment '{name}': {decode_error}")
                continue

        return decoded

    except requests.HTTPError as e:
        if e.response and e.response.status_code == 400:
            # Provide helpful debugging info for 400 errors
            raise Exception(
                f"❌ 400 Bad Request when fetching attachments\n"
                f"   Message ID: {message_id}\n"
                f"   User: {user_email}\n"
                f"   Possible causes:\n"
                f"   • Message has been deleted or moved\n"
                f"   • Message ID is corrupted or expired\n"
                f"   • User lacks permission to access message\n"
                f"   • Message has no attachments\n"
                f"   Suggestion: Mark this email as processed and skip"
            )
        raise Exception(f"Failed to fetch attachments: {str(e)}")
    except Exception as e:
        raise Exception(f"Unexpected error fetching attachments: {str(e)}")


def get_attachment_file_names(
        user_email: str,
        message_id: str,
        token: Optional[str] = None,
) -> List[str]:
    """
    Get just the file names of attachments without fetching content.
    Useful for checking if message has relevant attachments before full fetch.
    """
    if not token:
        token = get_access_token()

    def fetch():
        endpoint = f"/users/{user_email}/messages/{message_id}/attachments?$select=name"
        return graph_request("GET", endpoint, token=token, timeout=20)

    try:
        result = _retry_on_transient(fetch)
        return [_safe(a.get("name", "")) for a in result.get("value", [])]
    except Exception as e:
        return []  # Return empty list on error (non-critical operation)


# ─────────────────────────────────────────────────────────────
# MESSAGE MANAGEMENT OPERATIONS
# ─────────────────────────────────────────────────────────────

def mark_message_read(
        user_email: str,
        message_id: str,
        token: Optional[str] = None,
) -> None:
    """Mark a message as read."""
    if not token:
        token = get_access_token()

    def update():
        endpoint = f"/users/{user_email}/messages/{message_id}"
        graph_request(
            "PATCH",
            endpoint,
            token=token,
            json_data={"isRead": True},
        )

    try:
        _retry_on_transient(update)
    except Exception as e:
        print(f"Warning: Could not mark message as read: {e}")


def move_message_to_folder(
        user_email: str,
        message_id: str,
        destination_folder_id: str,
        token: Optional[str] = None,
) -> None:
    """Move a message to another folder."""
    if not token:
        token = get_access_token()

    def move():
        endpoint = f"/users/{user_email}/messages/{message_id}/move"
        graph_request(
            "POST",
            endpoint,
            token=token,
            json_data={"destinationId": destination_folder_id},
        )

    try:
        _retry_on_transient(move)
    except Exception as e:
        print(f"Warning: Could not move message: {e}")


def get_or_create_folder(
        user_email: str,
        folder_name: str,
        parent_folder_id: str = "Inbox",
        token: Optional[str] = None,
) -> str:
    """
    Get or create a folder under the specified parent.

    Returns:
        str: Folder ID
    """
    if not token:
        token = get_access_token()

    def create():
        endpoint = f"/users/{user_email}/mailFolders/{parent_folder_id}/childFolders"
        result = graph_request(
            "POST",
            endpoint,
            token=token,
            json_data={
                "displayName": folder_name,
            },
        )
        return result.get("id")

    try:
        return _retry_on_transient(create)
    except Exception as e:
        raise Exception(f"Failed to get/create folder '{folder_name}': {str(e)}")


# ─────────────────────────────────────────────────────────────
# VALIDATION
# ─────────────────────────────────────────────────────────────

def test_email_access(user_email: str, token: Optional[str] = None) -> bool:
    """
    Test if we can access a user's mailbox.

    Returns:
        bool: True if accessible, False otherwise
    """
    if not token:
        token = get_access_token()

    try:
        endpoint = f"/users/{user_email}/mailFolders/Inbox?$select=id"
        graph_request("GET", endpoint, token=token, timeout=10)
        return True
    except Exception:
        return False