from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from webdriver_manager.chrome import ChromeDriverManager
import time


class SAPBot:

    def __init__(self):
        self.driver = None
        self.wait = None

    # =========================
    # ðŸ”¹ SETUP & LOGIN
    # =========================
    def start(self):
        self.driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))
        self.driver.maximize_window()
        self.wait = WebDriverWait(self.driver, 20)
        self.driver.get("https://agencysvc44.sapsf.com")

    def login(self):
        """Fully automated login using credentials from .env"""
        import os
        from dotenv import load_dotenv
        load_dotenv()

        company_id = os.getenv("SAP_COMPANY_ID")
        agency_id  = os.getenv("SAP_AGENCY_ID")
        email      = os.getenv("SAP_EMAIL")
        password   = os.getenv("SAP_PASSWORD")

        if not all([company_id, agency_id, email, password]):
            raise Exception("Missing SAP credentials in .env â€” need SAP_COMPANY_ID, SAP_AGENCY_ID, SAP_EMAIL, SAP_PASSWORD")

        # Step 1: Enter Company ID
        print("ðŸ” Step 1: Entering Company ID...")
        company_input = self.wait.until(EC.presence_of_element_located(
            (By.XPATH, "//input[@placeholder='Enter Company ID' or @id='companyId' or @name='company']")
        ))
        company_input.clear()
        company_input.send_keys(company_id)

        continue_btn = self.wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[normalize-space()='Continue'] | //input[@value='Continue']")
        ))
        continue_btn.click()
        print("âœ… Company ID submitted")

        # Step 2: Enter Agency ID, Email, Password
        print("ðŸ” Step 2: Entering Agency credentials...")
        self.wait.until(EC.presence_of_element_located(
            (By.XPATH, "//input[@id='agencyId' or @name='agencyId' or @placeholder='Agency ID']")
        )).send_keys(agency_id)

        self.wait.until(EC.presence_of_element_located(
            (By.XPATH, "//input[@type='email' or @id='userEmail' or @placeholder='User Email']")
        )).send_keys(email)

        self.wait.until(EC.presence_of_element_located(
            (By.XPATH, "//input[@type='password']")
        )).send_keys(password)

        login_btn = self.wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[normalize-space()='Log in'] | //input[@value='Log in']")
        ))
        login_btn.click()
        print("âœ… Credentials submitted")

        # Step 3: Dismiss "Change your password" Chrome popup if it appears
        try:
            ok_btn = WebDriverWait(self.driver, 5).until(EC.element_to_be_clickable(
                (By.XPATH, "//button[normalize-space()='OK'] | //button[normalize-space()='Dismiss']")
            ))
            ok_btn.click()
            print("âœ… Dismissed password warning popup")
        except:
            pass  # Popup didn't appear, that's fine

        # Step 4: Wait for home page
        self.wait.until(lambda d: "home" in d.current_url or "job" in d.current_url)
        time.sleep(2)
        print("âœ… Logged in successfully")

    def wait_for_login(self):
        """Fallback: wait for manual login completion."""
        self.wait.until(lambda d: "home" in d.current_url or "job" in d.current_url)
        time.sleep(2)

    def close(self):
        if self.driver:
            self.driver.quit()
            self.driver = None

    # =========================
    # ðŸ”¹ INTERNAL HELPERS
    # =========================
    def _fill(self, xpath, value):
        el = self.wait.until(EC.presence_of_element_located((By.XPATH, xpath)))
        el.clear()
        el.send_keys(value)

    def _sap_select(self, control_id, value, by="key"):
        match_expr = (
            f"i.getKey() === '{value}'" if by == "key"
            else f"i.getText().trim() === '{value}' || i.getText().includes('{value}')"
        )
        return self.driver.execute_script(f"""
            try {{
                var c = sap.ui.getCore().byId('{control_id}');
                if (!c) return 'not_found';
                var match = c.getItems().find(i => {match_expr});
                if (!match) return 'item_not_found';
                {'c.setSelectedKey(\'' + value + '\');' if by == 'key' else 'c.setSelectedItem(match);'}
                c.fireChange({{selectedItem: c.getSelectedItem()}});
                return 'ok:' + c.getSelectedItem().getText();
            }} catch(e) {{ return 'error:' + e.message; }}
        """)

    def _action_click(self, element):
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        time.sleep(0.3)
        ActionChains(self.driver).move_to_element(element).click().perform()

    # =========================
    # ðŸ”¹ FIND & OPEN JOB
    # =========================
    def find_and_open_job(self, req_id):
        print(f"ðŸ” Searching Requisition ID: {req_id}")
        container = self.wait.until(EC.presence_of_element_located(
            (By.XPATH, "//section[contains(@class,'sapMPageEnableScrolling')]")
        ))
        for i in range(50):
            jobs = self.driver.find_elements(By.XPATH, "//li[contains(@class,'sapMLIB')]")
            for job in jobs:
                try:
                    if req_id in job.text.strip().replace(" ", ""):
                        self.driver.execute_script("arguments[0].scrollIntoView(true);", job)
                        time.sleep(1)
                        try:
                            job.click()
                        except:
                            self.driver.execute_script("arguments[0].click();", job)
                        self.wait.until(lambda d: req_id in d.page_source)
                        time.sleep(2)
                        print(f"âœ… Opened Requisition ID: {req_id}")
                        return True
                except:
                    continue
            self.driver.execute_script("arguments[0].scrollBy(0, 300);", container)
            time.sleep(1.5)
        print(f"âŒ Requisition ID {req_id} not found")
        return False

    # =========================
    # ðŸ”¹ OPEN ACTIONS MENU
    # =========================
    def _open_add_candidate_form(self, jr_number):
        self.wait.until(lambda d: jr_number in d.page_source)

        menu_btn = self.wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//span[@aria-label='Actions']")
        ))
        self._action_click(menu_btn)
        self.wait.until(EC.presence_of_element_located(
            (By.XPATH, "//div[contains(@class,'sapMPopup')] | //div[contains(@class,'sapMPopover')]")
        ))

        submit_btn = self.wait.until(EC.presence_of_element_located(
            (By.XPATH, "//span[normalize-space()='Submit New Candidate']")
        ))
        self._action_click(submit_btn)

        self.wait.until(EC.presence_of_element_located(
            (By.XPATH, "//input[@placeholder='Please enter first name.']")
        ))
        print("âœ… Add Candidate form opened")

    # =========================
    # ðŸ”¹ FILL & SUBMIT FORM
    # =========================
    def upload_candidate(self, data):
        """
        data keys: jr_number, first_name, last_name, email, phone,
                   country_code (+91), country (India), resume_path
        """
        jr = data["jr_number"]

        if not self.find_and_open_job(jr):
            raise Exception(f"Requisition ID {jr} not found")

        self._open_add_candidate_form(jr)

        # Fill text fields
        self._fill("//input[@placeholder='Please enter first name.']",   data["first_name"])
        self._fill("//input[@placeholder='Please enter last name.']",    data["last_name"])
        self._fill("//input[@placeholder='Please enter email.']",        data["email"])
        self._fill("//input[@placeholder='Re-enter the email address']", data["email"])
        self._fill("//input[@placeholder='Please enter phone number']",  data["phone"])

        # Dropdowns
        r1 = self._sap_select("phoneCodeDlgFld", data.get("country_code", "+91"), by="key")
        print(f"ðŸŒ Country code: {r1}")

        r2 = self._sap_select("countryDlgFld", data.get("country", "India"), by="text")
        print(f"ðŸŒ Country: {r2}")

        # Resume upload
        self.driver.find_element(By.XPATH, "//input[@type='file']").send_keys(data["resume_path"])
        time.sleep(1)
        print("âœ… Resume uploaded")

        # Checkbox
        try:
            dialog = self.driver.find_element(
                By.XPATH,
                "//section[contains(@class,'sapMDialogSection')] | "
                "//div[contains(@class,'sapMDialogScrollCont')]"
            )
            self.driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", dialog)
            time.sleep(0.5)
        except:
            pass

        cb = self.wait.until(EC.presence_of_element_located((By.XPATH, "//div[@role='checkbox']")))
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cb)
        time.sleep(0.5)
        ActionChains(self.driver).move_to_element(cb).pause(0.3).click().perform()
        time.sleep(0.5)
        print(f"âœ… Checkbox: aria-checked={cb.get_attribute('aria-checked')}")

        # Pause so the form can be reviewed before final action
        time.sleep(5)

        # Submit or Cancel
        if data.get("submit", True):
            add_btn = self.wait.until(EC.element_to_be_clickable(
                (By.XPATH,
                 "//button[normalize-space()='Add Candidate'] | "
                 "//bdi[normalize-space()='Add Candidate']/ancestor::button")
            ))
            self._action_click(add_btn)
            try:
                self.wait.until(EC.invisibility_of_element_located(
                    (By.XPATH, "//div[contains(@class,'sapMDialog')]")
                ))
                print(f"âœ… Candidate submitted for JR {jr}")
            except:
                raise Exception("Dialog did not close after submission â€” verify manually")
        else:
            cancel_btn = self.wait.until(EC.element_to_be_clickable(
                (By.XPATH,
                 "//button[normalize-space()='Cancel'] | "
                 "//bdi[normalize-space()='Cancel']/ancestor::button")
            ))
            self._action_click(cancel_btn)
            print(f"ðŸš« Cancelled form for JR {jr} (dry run)")
