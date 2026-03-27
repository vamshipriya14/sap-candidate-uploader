from utils import save_temp_file


def upload_to_sap(bot, data):
    """
    Upload a single candidate using an already-started and logged-in SAPBot instance.

    Args:
        bot:  A SAPBot instance that has already called start() + wait_for_login()
        data: Dict with keys - jr_number, first_name, last_name, email, phone,
              country_code, country, resume_file (Streamlit UploadedFile)
    """
    resume_path = save_temp_file(data["resume_file"])
    bot.upload_candidate({
        **data,
        "resume_path": resume_path
    })