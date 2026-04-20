import time
import io
import requests

from resume_repository import _headers, SUPABASE_URL, SUPABASE_TABLE, BUCKET
from sap_bot_headless import SAPBot
from uploader import upload_to_sap


# ⏱ CONFIG
RETRY_INTERVAL = 300   # seconds (5 mins)
MAX_RETRIES = 3
BATCH_SIZE = 10


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def fetch_retry_records():
    url = (
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
        f"?upload_to_sap=neq.Done"
        f"&select=*"
        f"&limit={BATCH_SIZE}"
    )

    resp = requests.get(url, headers=_headers(), timeout=30)

    if resp.status_code != 200:
        print("Fetch error:", resp.text)
        return []

    return resp.json()


def download_resume(path):
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}"

    resp = requests.get(url, headers=_headers(json=False), timeout=30)

    if resp.status_code != 200:
        raise Exception(f"Download failed: {resp.text}")

    return resp.content


def update_record(record_id, fields):
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id=eq.{record_id}"

    resp = requests.patch(url, headers=_headers(), json=fields, timeout=15)

    if resp.status_code not in (200, 204):
        print("Update failed:", resp.text)


def is_session_dead(err):
    msg = str(err).lower()
    return "invalid session id" in msg or "disconnected" in msg


# ─────────────────────────────────────────
# MAIN WORKER LOOP
# ─────────────────────────────────────────

def run_worker():
    print("🚀 SAP Retry Worker started...")

    bot = None

    while True:
        try:
            records = fetch_retry_records()

            if not records:
                print("✅ No pending records. Sleeping...")
                time.sleep(RETRY_INTERVAL)
                continue

            print(f"🔁 Found {len(records)} records to retry")

            # Start SAP session
            if not bot:
                bot = SAPBot()
                bot.start()
                bot.login()
                print("🔐 SAP connected")

            for rec in records:
                record_id = rec.get("id")
                retry_count = rec.get("retry_count", 0)

                if retry_count >= MAX_RETRIES:
                    print(f"⏭ Skipping {record_id} (max retries reached)")
                    continue

                try:
                    resume_path = rec.get("resume_path")
                    file_name = rec.get("file_name")

                    if not resume_path:
                        print(f"⚠️ Missing resume_path for {record_id}")
                        continue

                    file_bytes = download_resume(resume_path)

                    file_obj = io.BytesIO(file_bytes)
                    file_obj.name = file_name

                    upload_to_sap(
                        bot,
                        {
                            "jr_number": rec.get("jr_number"),
                            "first_name": rec.get("first_name"),
                            "last_name": rec.get("last_name"),
                            "submit": True,
                            "email": rec.get("email"),
                            "phone": rec.get("phone"),
                            "country_code": "+91",
                            "country": "India",
                            "resume_file": file_obj,
                        },
                    )

                    update_record(record_id, {
                        "upload_to_sap": "Done",
                        "error_message": "",
                        "retry_count": retry_count
                    })

                    print(f"✅ Success: {file_name}")

                except Exception as e:
                    err = str(e)

                    # 🔥 Restart SAP if session dead
                    if is_session_dead(e):
                        print("⚠️ SAP session lost. Restarting...")

                        try:
                            bot.close()
                        except:
                            pass

                        bot = SAPBot()
                        bot.start()
                        bot.login()
                        print("🔁 SAP restarted")

                    update_record(record_id, {
                        "upload_to_sap": "Failed",
                        "error_message": err[:500],
                        "retry_count": retry_count + 1
                    })

                    print(f"❌ Failed: {file_name} → {err}")

            print(f"⏳ Sleeping {RETRY_INTERVAL}s...\n")
            time.sleep(RETRY_INTERVAL)

        except Exception as e:
            print("💥 Worker crashed:", e)
            time.sleep(60)


if __name__ == "__main__":
    run_worker()