from pathlib import Path
import re
import time

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


class SAPBot:
    def __init__(self):
        self.driver = None
        self.wait = None
        self.run_started_at = time.strftime("%Y%m%d_%H%M%S")
        self.screenshot_dir = Path.cwd() / "screenshots" / self.run_started_at
        self.screenshot_counter = 0

    # =========================
    # SETUP & LOGIN
    # =========================
    def start(self):
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--log-level=3")
        options.add_experimental_option("excludeSwitches", ["enable-logging"])

        import shutil

        if shutil.which("chromium-browser"):
            options.binary_location = "/usr/bin/chromium-browser"
            service = Service("/usr/bin/chromedriver")
        else:
            service = Service(ChromeDriverManager().install())

        self.driver = webdriver.Chrome(service=service, options=options)
        self.wait = WebDriverWait(self.driver, 20)
        self.driver.get("https://agencysvc44.sapsf.com")

    def login(self):
        import os
        from dotenv import load_dotenv

        load_dotenv()

        company_id = os.getenv("SAP_COMPANY_ID")
        agency_id = os.getenv("SAP_AGENCY_ID")
        email = os.getenv("SAP_EMAIL")
        password = os.getenv("SAP_PASSWORD")

        if not all([company_id, agency_id, email, password]):
            raise Exception(
                "Missing SAP credentials - need SAP_COMPANY_ID, SAP_AGENCY_ID, SAP_EMAIL, SAP_PASSWORD"
            )

        print("Step 1: entering Company ID")
        time.sleep(2)
        self.wait.until(EC.presence_of_element_located((By.NAME, "companyId"))).send_keys(company_id)
        self.driver.find_element(By.CSS_SELECTOR, "button[id*='continueButton']").click()
        time.sleep(3)
        self._screenshot("00_company_id_submitted")

        print("Step 2: entering credentials")
        self.wait.until(EC.presence_of_element_located((By.XPATH, "//input[contains(@placeholder,'Agency')]"))).send_keys(
            agency_id
        )
        self.driver.find_element(By.XPATH, "//input[contains(@placeholder,'Email')]").send_keys(email)
        self.driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(password)
        self.driver.find_element(By.CSS_SELECTOR, "button[id*='login']").click()
        time.sleep(5)

        if "login" in self.driver.current_url.lower():
            raise Exception("Login failed - check SAP credentials")

        print("Logged in successfully")
        self._screenshot("01_logged_in")

    def close(self):
        if self.driver:
            self.driver.quit()
            self.driver = None

    # =========================
    # INTERNAL HELPERS
    # =========================
    def _fill(self, xpath, value):
        el = self.wait.until(EC.presence_of_element_located((By.XPATH, xpath)))
        self.driver.execute_script("arguments[0].click();", el)
        self.driver.execute_script(
            """
            var el = arguments[0];
            var val = arguments[1];
            var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeInputValueSetter.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
            """,
            el,
            value,
        )
        time.sleep(0.2)

    def _sap_select(self, control_id, value, by="key"):
        match_expr = (
            f"i.getKey() === '{value}'"
            if by == "key"
            else f"i.getText().trim() === '{value}' || i.getText().includes('{value}')"
        )
        select_stmt = f"c.setSelectedKey('{value}');" if by == "key" else "c.setSelectedItem(match);"
        return self.driver.execute_script(
            """
            try {{
                var c = sap.ui.getCore().byId('{control_id}');
                if (!c) return 'not_found';
                var match = c.getItems().find(i => {match_expr});
                if (!match) return 'item_not_found';
                {select_stmt}
                c.fireChange({{selectedItem: c.getSelectedItem()}});
                return 'ok:' + c.getSelectedItem().getText();
            }} catch(e) {{ return 'error:' + e.message; }}
            """.format(control_id=control_id, match_expr=match_expr, select_stmt=select_stmt)
        )

    def _action_click(self, element):
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        time.sleep(0.3)
        ActionChains(self.driver).move_to_element(element).click().perform()

    def _screenshot(self, name):
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_counter += 1
        path = self.screenshot_dir / f"{self.screenshot_counter:02d}_{name}.png"
        self.driver.save_screenshot(str(path))
        print(f"Screenshot saved: {path}")
        return path

    def _details_panel_state(self, req_id=None):
        return self.driver.execute_script(
            """
            var wanted = arguments[0] ? String(arguments[0]).replace(/\\s+/g, '').toLowerCase() : null;
            function visible(el) {
                if (!el) return false;
                var style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                var rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }
            var nodes = Array.from(document.querySelectorAll('section,div,span,h1,h2,h3,h4,bdi,label'));
            var snippets = [];
            for (var node of nodes) {
                if (!visible(node)) continue;
                if (node.closest('li.sapMLIB')) continue;
                var text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!text || text.length < 8) continue;
                if (text.toLowerCase().includes('requisition id')) {
                    var normalized = text.replace(/\\s+/g, '').toLowerCase();
                    if (wanted && normalized.includes(wanted)) {
                        return {matched: true, snippet: text.slice(0, 250)};
                    }
                    snippets.push(text.slice(0, 250));
                }
            }
            return {matched: false, snippet: snippets[0] || ''};
            """,
            req_id,
        )

    def _wait_for_details_panel(self, req_id, timeout=15):
        end = time.time() + timeout
        last_snippet = ""
        while time.time() < end:
            state = self._details_panel_state(req_id)
            last_snippet = state.get("snippet", "")
            if state.get("matched"):
                return True, last_snippet
            time.sleep(0.6)
        return False, last_snippet

    def _extract_job_panel_details(self):
        return self.driver.execute_script(
            """
            function visible(el) {
                if (!el) return false;
                var style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                var rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }
            function clean(text) {
                return (text || '').replace(/\\s+/g, ' ').trim();
            }
            function normalizeBlock(text) {
                return String(text || '')
                    .replace(/\\r/g, '')
                    .replace(/[ \\t]+\\n/g, '\\n')
                    .replace(/\\n[ \\t]+/g, '\\n')
                    .trim();
            }
            function sanitizePerson(text) {
                return clean(text)
                    .replace(/^(recruiter|client recruiter|agency contact)\\s*:?/i, '')
                    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/ig, ' ')
                    .replace(/\\s*copyright.*$/i, '')
                    .replace(/\\s*job details.*$/i, '')
                    .replace(/[\\uE000-\\uF8FF]+/g, '')
                    .replace(/[^A-Za-z0-9@._+\\-\\s]/g, ' ')
                    .replace(/\\s+/g, ' ')
                    .trim();
            }
            function nextValue(lines, index) {
                for (var j = index + 1; j < lines.length; j++) {
                    var candidate = clean(lines[j]);
                    if (!candidate) continue;
                    if (/^(Requisition ID|Posting Start Date|Posting End Date|Recruiter|Client Recruiter|Agency Contact|Job Details)$/i.test(candidate)) {
                        continue;
                    }
                    return candidate;
                }
                return '';
            }
            function extractByLabel(lines, labels) {
                for (var i = 0; i < lines.length; i++) {
                    var line = lines[i];
                    for (var j = 0; j < labels.length; j++) {
                        var label = labels[j];
                        if (new RegExp('^' + label + '$', 'i').test(line)) {
                            return nextValue(lines, i);
                        }
                        var inlineMatch = line.match(new RegExp('^' + label + '\\s*:?\\s*(.+)$', 'i'));
                        if (inlineMatch) return clean(inlineMatch[1]);
                    }
                }
                return '';
            }
            function parseSummaryText(text) {
                var raw = normalizeBlock(text);
                var lines = raw
                    .split(/\\n+/)
                    .map(clean)
                    .filter(Boolean);
                var flattened = clean(raw.replace(/\\n+/g, ' '));
                var data = {
                    title: '',
                    requisition_id: '',
                    posting_start_date: '',
                    posting_end_date: '',
                    recruiter_name: '',
                    recruiter_email: ''
                };
                data.title = lines.find(function (line) {
                    return !/^(Requisition ID|Posting Start Date|Posting End Date|Recruiter|Client Recruiter|Agency Contact|Job Details)$/i.test(line);
                }) || '';
                for (var i = 0; i < lines.length; i++) {
                    var line = lines[i];
                    var reqMatch = line.match(/^Requisition ID\\s*:?\\s*(.+)$/i);
                    var startMatch = line.match(/^Posting Start Date\\s*:?\\s*(.+)$/i);
                    var endMatch = line.match(/^Posting End Date\\s*:?\\s*(.+)$/i);
                    var recruiterMatch = line.match(/^(Recruiter|Client Recruiter|Agency Contact)\\s*:?\\s*(.+)$/i);
                    if (/^Requisition ID$/i.test(line)) data.requisition_id = nextValue(lines, i) || data.requisition_id;
                    else if (reqMatch) data.requisition_id = clean(reqMatch[1]) || data.requisition_id;
                    if (/^Posting Start Date$/i.test(line)) data.posting_start_date = nextValue(lines, i) || data.posting_start_date;
                    else if (startMatch) data.posting_start_date = clean(startMatch[1]) || data.posting_start_date;
                    if (/^Posting End Date$/i.test(line)) data.posting_end_date = nextValue(lines, i) || data.posting_end_date;
                    else if (endMatch) data.posting_end_date = clean(endMatch[1]) || data.posting_end_date;
                    if (/^(Recruiter|Client Recruiter|Agency Contact)$/i.test(line)) {
                        data.recruiter_name = sanitizePerson(nextValue(lines, i) || data.recruiter_name);
                    } else if (recruiterMatch) {
                        data.recruiter_name = sanitizePerson(recruiterMatch[2] || data.recruiter_name);
                    }
                    var emailMatch = line.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i);
                    if (emailMatch && !data.recruiter_email) data.recruiter_email = emailMatch[0];
                }
                if (!data.recruiter_name) {
                    var recruiterInline = raw.match(/(?:Recruiter|Client Recruiter|Agency Contact)\\s*:?\\s*([^\\n]+)/i);
                    if (recruiterInline) data.recruiter_name = sanitizePerson(recruiterInline[1]);
                }
                if (!data.recruiter_name && flattened) {
                    var flatRecruiter = flattened.match(/(?:Recruiter|Client Recruiter|Agency Contact)\\s*:?\\s*([A-Za-z][A-Za-z .,'()&-]{1,80})/i);
                    if (flatRecruiter) data.recruiter_name = sanitizePerson(flatRecruiter[1]);
                }
                if (!data.title && flattened) {
                    var titleMatch = flattened.match(/^(.*?)\\s+Requisition ID\\b/i);
                    if (titleMatch) data.title = clean(titleMatch[1]);
                }
                return data;
            }
            function summarizeCandidate(el) {
                var rect = el.getBoundingClientRect();
                var rawText = normalizeBlock(el.innerText);
                var text = clean(rawText);
                return {
                    el: el,
                    text: text,
                    rawText: rawText,
                    top: rect.top,
                    left: rect.left,
                    width: rect.width,
                    height: rect.height,
                    area: rect.width * rect.height
                };
            }

            var candidates = Array.from(document.querySelectorAll('section, div'))
                .filter(visible)
                .filter(function (el) { return !el.closest('li.sapMLIB'); })
                .map(summarizeCandidate)
                .filter(function (item) {
                    return item.text &&
                        item.text.indexOf('JOB DETAILS') >= 0 &&
                        item.text.indexOf('Requisition ID') >= 0 &&
                        item.text.indexOf('Recruiter') >= 0;
                })
                .sort(function (a, b) {
                    var aRightPane = a.left > 250 ? 0 : 1;
                    var bRightPane = b.left > 250 ? 0 : 1;
                    if (aRightPane !== bRightPane) return aRightPane - bRightPane;
                    if (a.area !== b.area) return a.area - b.area;
                    if (a.top !== b.top) return a.top - b.top;
                    return a.left - b.left;
                });

            var panelText = candidates.length ? candidates[0].rawText : '';
            var summaryText = panelText ? panelText.split(/JOB DETAILS/i)[0] : '';
            var parsed = parseSummaryText(summaryText);
            var summaryLines = summaryText.split(/\\n+/).map(clean).filter(Boolean);

            if (summaryLines.length) {
                var summaryTitle = summaryLines[0];
                if (summaryTitle && !/agency access/i.test(summaryTitle)) {
                    parsed.title = summaryTitle;
                }
                if (!parsed.requisition_id) parsed.requisition_id = extractByLabel(summaryLines, ['Requisition ID']);
                if (!parsed.posting_start_date) parsed.posting_start_date = extractByLabel(summaryLines, ['Posting Start Date']);
                if (!parsed.posting_end_date) parsed.posting_end_date = extractByLabel(summaryLines, ['Posting End Date']);
                if (!parsed.recruiter_name) parsed.recruiter_name = sanitizePerson(extractByLabel(summaryLines, ['Recruiter', 'Client Recruiter', 'Agency Contact']));
            }

            return {
                title: parsed.title,
                recruiter_name: parsed.recruiter_name,
                recruiter_email: parsed.recruiter_email
            };
            """
        )

    def _extract_recruiter_from_panel_text(self):
        return self.driver.execute_script(
            """
            function visible(el) {
                if (!el) return false;
                var style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                var rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }
            function clean(text) {
                return (text || '').replace(/\\s+/g, ' ').trim();
            }
            function normalizeBlock(text) {
                return String(text || '')
                    .replace(/\\r/g, '')
                    .replace(/[ \\t]+\\n/g, '\\n')
                    .replace(/\\n[ \\t]+/g, '\\n')
                    .trim();
            }
            function sanitizePerson(text) {
                return clean(text)
                    .replace(/^(recruiter|client recruiter|agency contact)\\s*:?/i, '')
                    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/ig, ' ')
                    .replace(/\\s*copyright.*$/i, '')
                    .replace(/\\s*job details.*$/i, '')
                    .replace(/[\\uE000-\\uF8FF]+/g, '')
                    .replace(/[^A-Za-z0-9@._+\\-\\s]/g, ' ')
                    .replace(/\\s+/g, ' ')
                    .trim();
            }
            function nextValue(lines, index) {
                for (var j = index + 1; j < lines.length; j++) {
                    var candidate = clean(lines[j]);
                    if (!candidate) continue;
                    if (/^(Requisition ID|Posting Start Date|Posting End Date|Recruiter|Client Recruiter|Agency Contact|Job Details)$/i.test(candidate)) {
                        continue;
                    }
                    return candidate;
                }
                return '';
            }
            function summarizeCandidate(el) {
                var rect = el.getBoundingClientRect();
                var rawText = normalizeBlock(el.innerText);
                var text = clean(rawText);
                return {
                    text: text,
                    rawText: rawText,
                    top: rect.top,
                    left: rect.left,
                    area: rect.width * rect.height
                };
            }
            function parseSummaryText(text) {
                var raw = normalizeBlock(text);
                var lines = raw
                    .split(/\\n+/)
                    .map(clean)
                    .filter(Boolean);
                var flattened = clean(raw.replace(/\\n+/g, ' '));
                var title = lines.find(function (line) {
                    return !/^(Requisition ID|Posting Start Date|Posting End Date|Recruiter|Client Recruiter|Agency Contact|Job Details)$/i.test(line);
                }) || '';
                var recruiterName = '';
                var recruiterEmail = '';
                for (var i = 0; i < lines.length; i++) {
                    var line = lines[i];
                    var recruiterMatch = line.match(/^(Recruiter|Client Recruiter|Agency Contact)\\s*:?\\s*(.+)$/i);
                    if (/^(Recruiter|Client Recruiter|Agency Contact)$/i.test(line)) {
                        recruiterName = sanitizePerson(nextValue(lines, i) || '');
                    } else if (recruiterMatch) {
                        recruiterName = sanitizePerson(recruiterMatch[2] || '');
                    }
                    var emailMatch = line.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i);
                    if (emailMatch && !recruiterEmail) recruiterEmail = emailMatch[0];
                }
                if (!recruiterName) {
                    var recruiterInline = raw.match(/(?:Recruiter|Client Recruiter|Agency Contact)\\s*:?\\s*([^\\n]+)/i);
                    if (recruiterInline) recruiterName = sanitizePerson(recruiterInline[1]);
                }
                if (!recruiterName && flattened) {
                    var flatRecruiter = flattened.match(/(?:Recruiter|Client Recruiter|Agency Contact)\\s*:?\\s*([A-Za-z][A-Za-z .,'()&-]{1,80})/i);
                    if (flatRecruiter) recruiterName = sanitizePerson(flatRecruiter[1]);
                }
                if (!title && flattened) {
                    var titleMatch = flattened.match(/^(.*?)\\s+Requisition ID\\b/i);
                    if (titleMatch) title = clean(titleMatch[1]);
                }
                return {title: title, recruiter_name: recruiterName, recruiter_email: recruiterEmail};
            }

            var candidates = Array.from(document.querySelectorAll('section, div'))
                .filter(visible)
                .filter(function (el) { return !el.closest('li.sapMLIB'); })
                .map(summarizeCandidate)
                .filter(function (item) {
                    return item.text &&
                        item.text.indexOf('JOB DETAILS') >= 0 &&
                        item.text.indexOf('Requisition ID') >= 0 &&
                        item.text.indexOf('Recruiter') >= 0;
                })
                .sort(function (a, b) {
                    var aRightPane = a.left > 250 ? 0 : 1;
                    var bRightPane = b.left > 250 ? 0 : 1;
                    if (aRightPane !== bRightPane) return aRightPane - bRightPane;
                    if (a.area !== b.area) return a.area - b.area;
                    if (a.top !== b.top) return a.top - b.top;
                    return a.left - b.left;
                });

            var bestText = candidates.length ? candidates[0].rawText.split(/JOB DETAILS/i)[0] : '';
            var parsed = parseSummaryText(bestText);

            return {
                text: bestText,
                title: parsed.title,
                recruiter_name: parsed.recruiter_name,
                recruiter_email: parsed.recruiter_email
            };
            """
        )

    def _extract_recruiter_from_sap_controls(self):
        return self.driver.execute_script(
            """
            try {
                function clean(text) {
                    return (text || '').replace(/\\s+/g, ' ').trim();
                }
                function visibleDom(dom) {
                    if (!dom) return false;
                    var style = window.getComputedStyle(dom);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    var rect = dom.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }
                var entries = [];
                var recruiterName = '';
                var recruiterEmail = '';
                var elements = Object.values((window.sap && sap.ui && sap.ui.getCore && sap.ui.getCore().mElements) || {});

                for (var i = 0; i < elements.length; i++) {
                    var ctrl = elements[i];
                    if (!ctrl || !ctrl.getMetadata) continue;
                    var dom = ctrl.getDomRef ? ctrl.getDomRef() : null;
                    if (!visibleDom(dom)) continue;
                    if (dom && dom.closest && dom.closest('li.sapMLIB')) continue;

                    var value = '';
                    if (!value && ctrl.getText) value = ctrl.getText();
                    if (!value && ctrl.getTitle) value = ctrl.getTitle();
                    if (!value && ctrl.getValue) value = ctrl.getValue();
                    if (!value && dom) value = dom.innerText || dom.textContent || '';
                    value = clean(value);
                    if (!value) continue;

                    entries.push({
                        id: ctrl.getId ? ctrl.getId() : '',
                        type: ctrl.getMetadata().getName(),
                        text: value
                    });

                    if (!recruiterName && /recruiter|client recruiter|agency contact/i.test(value) && value.length < 200) {
                        var nameMatch = value.match(/(?:Recruiter|Client Recruiter|Agency Contact)\\s*:?\\s*(.+)$/i);
                        if (nameMatch) recruiterName = clean(nameMatch[1]);
                    }
                    if (!recruiterEmail) {
                        var emailMatch = value.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i);
                        if (emailMatch) recruiterEmail = emailMatch[0];
                    }
                }

                return {
                    recruiter_name: recruiterName,
                    recruiter_email: recruiterEmail,
                    entries: entries.slice(0, 80)
                };
            } catch (e) {
                return {recruiter_name: '', recruiter_email: '', entries: [], error: e.message};
            }
            """
        )

    def _wait_for_recruiter_details(self, timeout=8):
        end = time.time() + timeout
        latest = {
            "panel": {"title": "", "recruiter_name": "", "recruiter_email": "", "text": ""},
            "sap": {"recruiter_name": "", "recruiter_email": "", "entries": []},
        }
        while time.time() < end:
            try:
                panel = self._extract_recruiter_from_panel_text()
            except Exception:
                panel = {"title": "", "recruiter_name": "", "recruiter_email": "", "text": ""}
            try:
                sap = self._extract_recruiter_from_sap_controls()
            except Exception:
                sap = {"recruiter_name": "", "recruiter_email": "", "entries": []}

            latest = {"panel": panel, "sap": sap}
            if (
                panel.get("recruiter_name")
                or panel.get("recruiter_email")
                or sap.get("recruiter_name")
                or sap.get("recruiter_email")
            ):
                return latest

            try:
                self.driver.execute_script("window.scrollBy(0, 250);")
            except Exception:
                pass
            time.sleep(0.7)
        return latest

    def _open_recruiter_contact_card(self, recruiter_name):
        sap_result = self.driver.execute_script(
            """
            try {
                function visible(el) {
                    if (!el) return false;
                    var style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    var rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }
                function clickLikeUser(el) {
                    if (!el) return false;
                    el.scrollIntoView({block: 'center'});
                    ['mousedown', 'mouseup', 'click'].forEach(function (evt) {
                        if (typeof el.dispatchEvent === 'function') {
                            el.dispatchEvent(new MouseEvent(evt, {bubbles: true, cancelable: true, view: window}));
                        }
                    });
                    if (typeof el.click === 'function') el.click();
                    return true;
                }
                function clickableAncestor(node) {
                    var current = node;
                    while (current) {
                        var tag = (current.tagName || '').toLowerCase();
                        var role = current.getAttribute ? current.getAttribute('role') : '';
                        var cls = (current.className || '').toLowerCase();
                        if (tag === 'button' || tag === 'a' || role === 'button' || cls.indexOf('sapuiicon') >= 0 || cls.indexOf('sapmlnk') >= 0) {
                            return current;
                        }
                        current = current.parentElement;
                    }
                    return node;
                }
                function fireSapControl(node) {
                    var current = node;
                    while (current) {
                        if (current.id && window.sap && sap.ui && sap.ui.getCore) {
                            var ctrl = sap.ui.getCore().byId(current.id);
                            if (ctrl) {
                                if (ctrl.firePress) {
                                    try {
                                        ctrl.firePress();
                                        return true;
                                    } catch (e) {}
                                }
                                if (ctrl.ontap) {
                                    try {
                                        ctrl.ontap({
                                            srcControl: ctrl,
                                            target: current,
                                            setMarked: function () {},
                                            preventDefault: function () {},
                                            stopPropagation: function () {},
                                            isMarked: function () { return false; },
                                        });
                                        return true;
                                    } catch (e) {}
                                }
                                if (ctrl.onclick) {
                                    try {
                                        ctrl.onclick({
                                            srcControl: ctrl,
                                            target: current,
                                            setMarked: function () {},
                                            preventDefault: function () {},
                                            stopPropagation: function () {},
                                        });
                                        return true;
                                    } catch (e) {}
                                }
                            }
                        }
                        current = current.parentElement;
                    }
                    return false;
                }
                function tryQuickViewControl() {
                    var quickNodes = Array.from(document.querySelectorAll(
                        "#__xmlview1--quickViewDetails, [id$='quickViewDetails'], [id*='quickViewDetails'], [aria-label='Contact'], [title='Contact'], [aria-label='Contact Card'], [title='Contact Card']"
                    )).filter(visible);
                    if (!quickNodes.length) {
                        return {ok: false, reason: 'quick_view_not_found'};
                    }

                    quickNodes = quickNodes
                        .map(function (el) {
                            var host = el.closest('div, section, article') || el.parentElement || el;
                            var hostText = clean(host.innerText || '');
                            var rect = el.getBoundingClientRect();
                            return {el: el, host: host, hostText: hostText.toLowerCase(), left: rect.left, top: rect.top};
                        })
                        .filter(function (item) {
                            return !wantedName || item.hostText.indexOf(wantedName) >= 0;
                        })
                        .sort(function (a, b) {
                            if (a.top !== b.top) return a.top - b.top;
                            return a.left - b.left;
                        });

                    if (!quickNodes.length) {
                        return {ok: false, reason: 'quick_view_not_near_recruiter'};
                    }

                    var target = quickNodes[0].el;
                    if (fireSapControl(target) || clickLikeUser(target) || fireSapControl(target.parentElement) || clickLikeUser(target.parentElement)) {
                        return {
                            ok: true,
                            source: 'quick_view_control',
                            id: target.id || '',
                            aria: (target.getAttribute && target.getAttribute('aria-label')) || ''
                        };
                    }
                    return {ok: false, reason: 'quick_view_click_failed', id: target.id || ''};
                }
                function tryOpenFromRecruiterRow() {
                    var labels = Array.from(document.querySelectorAll('span, div, label, bdi'))
                        .filter(visible)
                        .filter(function (el) {
                            var text = clean(el.innerText || '').toLowerCase();
                            return text === 'recruiter' || text === 'client recruiter' || text === 'agency contact';
                        });
                    for (var i = 0; i < labels.length; i++) {
                        var row = labels[i].closest('div, section, article') || labels[i].parentElement;
                        if (!row || !visible(row)) continue;
                        var rowText = clean(row.innerText || '');
                        if (wantedName && rowText.toLowerCase().indexOf(wantedName) < 0) continue;

                        var descendants = Array.from(row.querySelectorAll('*')).filter(visible);
                        var icon = descendants.find(function (el) {
                            var text = clean(el.innerText || '');
                            var cls = (el.className || '').toLowerCase();
                            var title = ((el.getAttribute && (el.getAttribute('title') || el.getAttribute('aria-label'))) || '').toLowerCase();
                            var rect = el.getBoundingClientRect();
                            return (
                                title.indexOf('contact') >= 0 ||
                                title.indexOf('card') >= 0 ||
                                cls.indexOf('sapuiicon') >= 0 ||
                                cls.indexOf('sapmlnk') >= 0 ||
                                (rect.width <= 24 && rect.height <= 24 && text === '')
                            );
                        });
                        if (!icon && wantedName) {
                            var nameNode = descendants.find(function (el) {
                                return clean(el.innerText || '').toLowerCase() === wantedName;
                            });
                            if (nameNode) {
                                var rowChildren = Array.from((nameNode.parentElement || row).children || []).filter(visible);
                                var nameRect = nameNode.getBoundingClientRect();
                                icon = rowChildren
                                    .map(function (el) {
                                        var rect = el.getBoundingClientRect();
                                        return {el: el, rect: rect, text: clean(el.innerText || ''), cls: (el.className || '').toLowerCase()};
                                    })
                                    .filter(function (item) {
                                        return item.el !== nameNode &&
                                            item.rect.left >= nameRect.right - 4 &&
                                            item.rect.width > 0 &&
                                            item.rect.height > 0 &&
                                            item.rect.width <= 32 &&
                                            item.rect.height <= 32 &&
                                            item.text === '';
                                    })
                                    .sort(function (a, b) {
                                        return a.rect.left - b.rect.left;
                                    })
                                    .map(function (item) { return item.el; })[0] || null;
                            }
                        }
                        if (!icon) continue;
                        var target = clickableAncestor(icon);
                        if (fireSapControl(target) || clickLikeUser(target)) {
                            return {ok: true, source: 'recruiter_row_icon'};
                        }
                    }
                    return {ok: false, reason: 'recruiter_row_icon_not_found'};
                }
                function clean(text) {
                    return String(text || '').replace(/\\s+/g, ' ').trim();
                }
                function isContactCandidate(el) {
                    var text = ((el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('title'))) || el.innerText || '').toLowerCase();
                    var cls = (el.className || '').toLowerCase();
                    var rect = el.getBoundingClientRect();
                    var smallInline = rect.width <= 40 && rect.height <= 40;
                    return text.indexOf('contact') >= 0 ||
                        text.indexOf('card') >= 0 ||
                        text.indexOf('business') >= 0 ||
                        text.indexOf('employee') >= 0 ||
                        text.indexOf('email') >= 0 ||
                        text.indexOf('mail') >= 0 ||
                        cls.indexOf('sapuiicon') >= 0 ||
                        cls.indexOf('sapmobjstatus') >= 0 ||
                        cls.indexOf('sapmlnk') >= 0 ||
                        (smallInline && (cls.indexOf('icon') >= 0 || cls.indexOf('link') >= 0 || cls.indexOf('sap') >= 0));
                }
                function nearestCandidates(anchor) {
                    var anchorRect = anchor.getBoundingClientRect();
                    return Array.from(document.querySelectorAll('[role="button"], button, a, span, i, bdi, .sapUiIconPointer, .sapUiIcon, .sapMBtn, .sapMLnk'))
                        .filter(visible)
                        .map(function (el) {
                            var rect = el.getBoundingClientRect();
                            var dx = Math.abs(rect.left - anchorRect.right);
                            var dy = Math.abs(rect.top - anchorRect.top);
                            return {el: el, dx: dx, dy: dy, width: rect.width, height: rect.height};
                        })
                        .filter(function (item) {
                            return item.dy < 60 &&
                                item.dx < 180 &&
                                item.width > 0 &&
                                item.height > 0 &&
                                item.el !== anchor;
                        })
                        .sort(function (a, b) {
                            if (a.dy !== b.dy) return a.dy - b.dy;
                            return a.dx - b.dx;
                        })
                        .map(function (item) { return item.el; });
                }
                function findRecruiterValueAnchors(name) {
                    if (!name) return [];
                    return Array.from(document.querySelectorAll('span, div, label, bdi, a'))
                        .filter(visible)
                        .filter(function (el) {
                            if (el.closest && el.closest('li.sapMLIB')) return false;
                            var text = (el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                            return text === name || text.indexOf(name) >= 0;
                        })
                        .sort(function (a, b) {
                            var ar = a.getBoundingClientRect();
                            var br = b.getBoundingClientRect();
                            if (ar.top !== br.top) return ar.top - br.top;
                            return ar.left - br.left;
                        });
                }

                var wantedName = String(arguments[0] || '').trim().toLowerCase();
                var quickViewResult = tryQuickViewControl();
                if (quickViewResult.ok) return quickViewResult;
                var rowResult = tryOpenFromRecruiterRow();
                if (rowResult.ok) return rowResult;
                if (wantedName) {
                    var directIconResult = (function () {
                        var allNodes = Array.from(document.querySelectorAll('div, span, a, button')).filter(visible);
                        var nameNode = allNodes.find(function (el) {
                            return clean(el.innerText || '').toLowerCase() === wantedName;
                        });
                        if (!nameNode) return {ok: false, reason: 'name_node_not_found'};
                        var base = nameNode.parentElement || nameNode;
                        var candidates = Array.from(base.querySelectorAll('div, span, a, button')).filter(visible)
                            .map(function (el) {
                                var rect = el.getBoundingClientRect();
                                return {el: el, rect: rect, text: clean(el.innerText || '')};
                            })
                            .filter(function (item) {
                                return item.el !== nameNode &&
                                    item.rect.left >= nameNode.getBoundingClientRect().right - 4 &&
                                    item.rect.width <= 32 &&
                                    item.rect.height <= 32 &&
                                    item.text === '';
                            })
                            .sort(function (a, b) { return a.rect.left - b.rect.left; });
                        if (!candidates.length) return {ok: false, reason: 'direct_icon_not_found'};
                        var node = clickableAncestor(candidates[0].el);
                        if (fireSapControl(node) || clickLikeUser(node)) {
                            return {ok: true, source: 'direct_adjacent_icon'};
                        }
                        return {ok: false, reason: 'direct_adjacent_icon_click_failed'};
                    })();
                    if (directIconResult.ok) return directIconResult;
                }
                var nameAnchors = findRecruiterValueAnchors(wantedName);
                for (var a = 0; a < nameAnchors.length; a++) {
                    var aroundName = nearestCandidates(nameAnchors[a]).filter(isContactCandidate);
                    if (!aroundName.length) aroundName = nearestCandidates(nameAnchors[a]);
                    for (var x = 0; x < aroundName.length; x++) {
                        if (fireSapControl(aroundName[x]) || clickLikeUser(aroundName[x])) {
                            return {ok: true, source: 'sap_name_anchor_click'};
                        }
                    }
                }

                var labels = Array.from(document.querySelectorAll('span, div, label, bdi'))
                    .filter(visible)
                    .filter(function (el) {
                        var text = (el.innerText || '').trim().toLowerCase();
                        return text === 'recruiter' || text === 'client recruiter' || text === 'agency contact';
                    });

                for (var i = 0; i < labels.length; i++) {
                    var host = labels[i].closest('div, section, article, li') || labels[i].parentElement;
                    if (!host || !visible(host)) continue;
                    var hostText = (host.innerText || '').toLowerCase();
                    if (wantedName && hostText.indexOf(wantedName) < 0) continue;

                    var candidates = Array.from(host.querySelectorAll('[role="button"], button, a, .sapUiIconPointer, .sapUiIcon, .sapMBtn'))
                        .filter(visible)
                        .filter(isContactCandidate);
                    if (!candidates.length) {
                        candidates = Array.from(host.querySelectorAll('[role="button"], button, a, .sapUiIconPointer, .sapUiIcon, .sapMBtn'))
                            .filter(visible);
                    }
                    for (var c = 0; c < candidates.length; c++) {
                        if (fireSapControl(candidates[c]) || clickLikeUser(candidates[c])) {
                            return {ok: true, source: 'sap_host_click'};
                        }
                    }

                    var nearby = nearestCandidates(labels[i]).filter(isContactCandidate);
                    if (!nearby.length) nearby = nearestCandidates(labels[i]);
                    for (var n = 0; n < nearby.length; n++) {
                        if (fireSapControl(nearby[n]) || clickLikeUser(nearby[n])) {
                            return {ok: true, source: 'sap_nearby_click'};
                        }
                    }
                }
                return {ok: false, reason: 'sap_host_icon_not_found'};
            } catch (e) {
                return {ok: false, reason: e.message};
            }
            """,
            recruiter_name,
        )
        print(f"Recruiter contact card SAP click result: {sap_result}")
        self._screenshot("02a_before_contact_wait")
        for _ in range(4):
            popovers = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class,'sapMPopover') or contains(@class,'sapMPopup')][not(contains(@style,'display: none'))]",
            )
            if any(pop.is_displayed() for pop in popovers):
                self._screenshot("02a_contact_popover_opened")
                return True
            time.sleep(0.4)

        if recruiter_name:
            selectors = [
                (By.ID, "__xmlview1--quickViewDetails"),
                (By.CSS_SELECTOR, "[id*='quickViewDetails']"),
                (By.CSS_SELECTOR, "[aria-label='Contact Card']"),
                (By.XPATH, "//*[@aria-label='Contact' or @title='Contact' or @aria-label='Contact Card' or @title='Contact Card']"),
                (
                    By.XPATH,
                    f"//*[normalize-space()='Recruiter']/following::*[contains(normalize-space(), '{recruiter_name}')][1]/following::*[contains(@class,'sapUiIcon') or self::a or @role='button'][1]",
                ),
                (By.XPATH, f"//*[normalize-space()='{recruiter_name}']"),
                (By.XPATH, f"//*[contains(normalize-space(), '{recruiter_name}')]"),
                (By.XPATH, "//*[normalize-space()='Recruiter']/following::*[@role='button' or self::button or contains(@class,'sapUiIcon')][1]"),
                (By.XPATH, "//*[contains(@class,'sapUiIcon') and (@title='Contact Card' or @aria-label='Contact Card')]"),
            ]
        else:
            selectors = [
                (By.ID, "__xmlview1--quickViewDetails"),
                (By.CSS_SELECTOR, "[id*='quickViewDetails']"),
                (By.CSS_SELECTOR, "[aria-label='Contact Card']"),
                (By.XPATH, "//*[@aria-label='Contact' or @title='Contact' or @aria-label='Contact Card' or @title='Contact Card']"),
                (By.XPATH, "//*[normalize-space()='Recruiter']/following::*[@role='button' or self::button or contains(@class,'sapUiIcon')][1]"),
                (By.XPATH, "//*[contains(@class,'sapUiIcon') and (@title='Contact Card' or @aria-label='Contact Card')]"),
            ]

        for by, selector in selectors:
            matches = self.driver.find_elements(by, selector)
            for element in matches:
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                    try:
                        ActionChains(self.driver).move_to_element(element).pause(0.2).click().perform()
                    except Exception:
                        self.driver.execute_script(
                            """
                            var el = arguments[0];
                            if (el && typeof el.click === 'function') {
                                el.click();
                            } else if (el && typeof el.dispatchEvent === 'function') {
                                ['mousedown', 'mouseup', 'click'].forEach(function (evt) {
                                    el.dispatchEvent(new MouseEvent(evt, {bubbles: true, cancelable: true, view: window}));
                                });
                            }
                            """,
                            element,
                        )
                    time.sleep(1.2)
                    popovers = self.driver.find_elements(
                        By.XPATH,
                        "//div[contains(@class,'sapMPopover')] | //div[contains(@class,'sapMPopup')]",
                    )
                    if any(pop.is_displayed() for pop in popovers):
                        self._screenshot("02a_contact_popover_opened")
                        return True
                except Exception:
                    continue

        exact_quick_view = self.driver.find_elements(By.ID, "__xmlview1--quickViewDetails")
        for element in exact_quick_view:
            try:
                self.driver.execute_script(
                    """
                    var el = arguments[0];
                    if (!el) return;
                    el.scrollIntoView({block:'center'});
                    el.focus && el.focus();
                    ['mouseover', 'mousedown', 'mouseup', 'click'].forEach(function (evt) {
                        el.dispatchEvent(new MouseEvent(evt, {bubbles: true, cancelable: true, view: window}));
                    });
                    el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true}));
                    el.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', bubbles: true}));
                    if (typeof el.click === 'function') el.click();
                    """,
                    element,
                )
                time.sleep(1.2)
                popovers = self.driver.find_elements(
                    By.XPATH,
                    "//div[contains(@class,'sapMPopover')] | //div[contains(@class,'sapMPopup')] | //*[@role='dialog']",
                )
                if any(pop.is_displayed() for pop in popovers):
                    self._screenshot("02a_contact_popover_opened")
                    return True
            except Exception:
                continue
        return False

    def _extract_contact_from_popover(self):
        return self.driver.execute_script(
            """
            function clean(text) {
                return (text || '').replace(/\\s+/g, ' ').trim();
            }
            function normalizeBlock(text) {
                return String(text || '')
                    .replace(/\\r/g, '')
                    .replace(/[ \\t]+\\n/g, '\\n')
                    .replace(/\\n[ \\t]+/g, '\\n')
                    .trim();
            }
            function visible(el) {
                if (!el) return false;
                var style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                var rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }

            var popovers = Array.from(document.querySelectorAll('.sapMPopover, .sapMPopup, [role="dialog"]'))
                .filter(visible);
            if (!popovers.length) return {name: '', email: '', text: ''};

            var pop = popovers[popovers.length - 1];
            var rawText = normalizeBlock(pop.innerText);
            var text = clean(rawText);
            var lines = rawText.split(/\\n+/).map(clean).filter(Boolean);
            var email = '';
            for (var idx = 0; idx < lines.length; idx++) {
                var current = lines[idx];
                if (/^Email Address:?$/i.test(current)) {
                    for (var j = idx + 1; j < lines.length; j++) {
                        if (lines[j] && !/^Mobile:?$/i.test(lines[j])) {
                            var explicitMatch = lines[j].match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i);
                            if (explicitMatch) {
                                email = explicitMatch[0];
                                break;
                            }
                        }
                    }
                }
                if (email) break;
            }
            var emailMatch = email ? [email] : text.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i);
            var name = '';
            for (var i = 0; i < lines.length; i++) {
                var line = lines[i];
                if (/email address/i.test(line)) continue;
                if (/mobile|phone/i.test(line)) continue;
                if (/contact card|employee details|business card/i.test(line)) continue;
                if (emailMatch && line.toLowerCase() === emailMatch[0].toLowerCase()) continue;
                if (line.length > 2) {
                    name = line;
                    break;
                }
            }
            return {
                name: name,
                email: emailMatch ? emailMatch[0] : '',
                text: text
            };
            """
        )

    def get_job_email_details(self, req_id):
        req_id = str(req_id).strip()
        if not self.find_and_open_job(req_id):
            raise Exception(f"Requisition ID {req_id} not found in job list")

        details = self._extract_job_panel_details()
        recruiter_sources = self._wait_for_recruiter_details(timeout=8)
        panel_fallback = recruiter_sources.get("panel", {})
        sap_fallback = recruiter_sources.get("sap", {})

        recruiter_name = (
            details.get("recruiter_name", "").strip()
            or panel_fallback.get("recruiter_name", "").strip()
            or sap_fallback.get("recruiter_name", "").strip()
        )
        contact = {"name": "", "email": ""}
        contact_opened = False
        for _ in range(2):
            contact_opened = self._open_recruiter_contact_card(recruiter_name) or contact_opened
            contact = self._extract_contact_from_popover()
            if contact.get("email"):
                break
            time.sleep(0.5)

        final_name = (
            contact.get("name", "").strip()
            or recruiter_name
            or details.get("recruiter_name", "").strip()
            or panel_fallback.get("recruiter_name", "").strip()
            or sap_fallback.get("recruiter_name", "").strip()
        )
        final_email = (
            contact.get("email", "").strip()
            or details.get("recruiter_email", "").strip()
            or panel_fallback.get("recruiter_email", "").strip()
            or sap_fallback.get("recruiter_email", "").strip()
        )

        print(
            "Recruiter extraction:",
            {
                "details": details,
                "panel": panel_fallback,
                "sap_name": sap_fallback.get("recruiter_name", ""),
                "sap_email": sap_fallback.get("recruiter_email", ""),
                "contact": contact,
                "final_name": final_name,
                "final_email": final_email,
            },
        )
        self._screenshot("02a_recruiter_contact_details")
        return {
            "jr_number": req_id,
            "job_title": details.get("title", "").strip() or panel_fallback.get("title", "").strip(),
            "client_recruiter_name": final_name,
            "email_to": final_email,
            "contact_card_opened": contact_opened,
        }

    def _activate_sap_control_from_element(self, element):
        return self.driver.execute_script(
            """
            try {
                var node = arguments[0];
                while (node) {
                    if (node.id) {
                        var ctrl = sap.ui.getCore().byId(node.id);
                        if (ctrl) {
                            if (ctrl.firePress) {
                                ctrl.firePress();
                                return 'firePress:' + node.id;
                            }
                            if (ctrl.ontap) {
                                ctrl.ontap({srcControl: ctrl});
                                return 'ontap:' + node.id;
                            }
                        }
                    }
                    node = node.parentElement;
                }
                return 'control_not_found';
            } catch (e) {
                return 'error:' + e.message;
            }
            """,
            element,
        )

    def _set_terms_checkbox(self):
        try:
            dialog = self.driver.find_element(
                By.XPATH,
                "//section[contains(@class,'sapMDialogSection')] | //div[contains(@class,'sapMDialogScrollCont')]",
            )
            self.driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", dialog)
            time.sleep(0.8)
        except Exception:
            pass

        result = self.driver.execute_script(
            """
            try {
                function visible(el) {
                    if (!el) return false;
                    var style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    var rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }
                function inDialog(el) {
                    return !!(el && el.closest && el.closest('.sapMDialog, [role="dialog"]'));
                }
                function clickLikeUser(el) {
                    if (!el) return false;
                    el.scrollIntoView({block: 'center'});
                    ['mousedown', 'mouseup', 'click'].forEach(function (evt) {
                        el.dispatchEvent(new MouseEvent(evt, {bubbles: true, cancelable: true, view: window}));
                    });
                    if (el.click) el.click();
                    return true;
                }

                var controls = Object.values(sap.ui.getCore().mElements || {});
                var target = null;
                for (var c of controls) {
                    if (!c.getMetadata) continue;
                    var name = c.getMetadata().getName();
                    if (name !== 'sap.m.CheckBox' && name !== 'sap.m.CheckBoxListItem') continue;
                    if (c.getVisible && c.getVisible() === false) continue;
                    var dom = c.getDomRef ? c.getDomRef() : null;
                    if (!visible(dom) || !inDialog(dom)) continue;
                    var text = ((c.getText && c.getText()) || (dom.innerText || '')).toLowerCase();
                    if (!target) target = c;
                    if (text.includes('term') || text.includes('agree') || text.includes('consent') || text.includes('condition') || text.includes('privacy')) {
                        target = c;
                        break;
                    }
                }
                if (target) {
                    if (target.setSelected) target.setSelected(true);
                    if (target.fireSelect) target.fireSelect({selected: true});
                    if (target.firePress) target.firePress();
                    return {
                        ok: true,
                        source: 'sap_control',
                        id: target.getId ? target.getId() : null,
                        selected: target.getSelected ? target.getSelected() : null
                    };
                }

                var dialogCheckboxes = Array.from(document.querySelectorAll(
                    '.sapMDialog [role="checkbox"], [role="dialog"] [role="checkbox"], ' +
                    '.sapMDialog input[type="checkbox"], [role="dialog"] input[type="checkbox"], ' +
                    '.sapMDialog .sapMCb, [role="dialog"] .sapMCb, ' +
                    '.sapMDialog .sapMCbBg, [role="dialog"] .sapMCbBg, ' +
                    '.sapMDialog [class*="sapMCb"], [role="dialog"] [class*="sapMCb"]'
                )).filter(visible);
                if (!dialogCheckboxes.length) {
                    return {
                        ok: false,
                        reason: 'checkbox_not_found',
                        dialogText: (document.querySelector('.sapMDialog, [role="dialog"]') || {}).innerText || ''
                    };
                }

                var domTarget = dialogCheckboxes.find(function (el) {
                    var holder = el.closest('label,div,section,li');
                    var text = ((el.innerText || '') + ' ' + (holder ? holder.innerText || '' : '')).toLowerCase();
                    return text.includes('term') || text.includes('agree') || text.includes('consent') || text.includes('condition') || text.includes('privacy');
                }) || dialogCheckboxes[dialogCheckboxes.length - 1];

                clickLikeUser(domTarget);

                var nestedInput = domTarget.matches('input[type="checkbox"]')
                    ? domTarget
                    : (domTarget.querySelector ? domTarget.querySelector('input[type="checkbox"]') : null);
                if (nestedInput) {
                    nestedInput.checked = true;
                    nestedInput.dispatchEvent(new Event('input', {bubbles: true}));
                    nestedInput.dispatchEvent(new Event('change', {bubbles: true}));
                }

                return {
                    ok: true,
                    source: 'dom_checkbox',
                    tag: domTarget.tagName,
                    classes: domTarget.className || '',
                    checked:
                        domTarget.getAttribute('aria-checked') === 'true' ||
                        !!domTarget.checked ||
                        ((domTarget.className || '').indexOf('sapMCbMarkChecked') >= 0) ||
                        ((domTarget.outerHTML || '').indexOf('sapMCbMarkChecked') >= 0) ||
                        (nestedInput ? !!nestedInput.checked : false)
                };
            } catch (e) {
                return {ok: false, reason: e.message};
            }
            """
        )
        print(f"   Checkbox SAP API result: {result}")

        if isinstance(result, dict) and result.get("checked") is True:
            print("Checkbox accepted via single-click in-page handler")
            return

        checkbox = self.wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "(//div[contains(@class,'sapMDialog')]//*[@role='checkbox'] | "
                    "//div[@role='dialog']//*[@role='checkbox'] | "
                    "//div[contains(@class,'sapMDialog')]//input[@type='checkbox'] | "
                    "//div[@role='dialog']//input[@type='checkbox'] | "
                    "//div[contains(@class,'sapMDialog')]//*[contains(@class,'sapMCb')] | "
                    "//div[@role='dialog']//*[contains(@class,'sapMCb')])[last()]",
                )
            )
        )
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", checkbox)
        time.sleep(0.5)

        def is_checked():
            aria_checked = checkbox.get_attribute("aria-checked")
            dom_checked = checkbox.get_attribute("checked")
            css_class = checkbox.get_attribute("class") or ""
            parent_class = checkbox.get_attribute("outerHTML") or ""
            return (
                aria_checked == "true"
                or dom_checked is not None
                or "sapMCbMarkChecked" in css_class
                or "sapMCbMarkChecked" in parent_class
            )

        if not is_checked():
            inner_spans = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class,'sapMDialog')]//*[contains(@class,'sapMCbBg') or contains(@class,'sapMCbMark') or contains(@class,'sapMCb')] | "
                "//div[@role='dialog']//*[contains(@class,'sapMCbBg') or contains(@class,'sapMCbMark') or contains(@class,'sapMCb')]",
            )
            if inner_spans:
                self.driver.execute_script(
                    """
                    var el = arguments[0];
                    if (el && typeof el.dispatchEvent === 'function') {
                        ['mousedown', 'mouseup', 'click'].forEach(function (evt) {
                            el.dispatchEvent(new MouseEvent(evt, {bubbles: true, cancelable: true, view: window}));
                        });
                    }
                    if (el && typeof el.click === 'function') el.click();
                    """,
                    inner_spans[-1],
                )
                time.sleep(0.4)

        if not is_checked():
            try:
                ActionChains(self.driver).move_to_element(checkbox).pause(0.2).click().perform()
            except Exception:
                self.driver.execute_script("arguments[0].click();", checkbox)
            time.sleep(0.5)

        if not is_checked():
            terms_targets = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class,'sapMDialog')]//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'i understand and agree')] | "
                "//div[contains(@class,'sapMDialog')]//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'terms and conditions')]",
            )
            for target in terms_targets:
                try:
                    clickables = self.driver.find_elements(
                        By.XPATH,
                        ".//*[contains(@class,'sapMCb') or contains(@class,'sapMCbBg') or contains(@class,'sapMCbMark')]",
                    )
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
                    ActionChains(self.driver).move_to_element(target).move_by_offset(-20, 0).click().perform()
                    time.sleep(0.4)
                    if is_checked():
                        break
                except Exception:
                    continue

        if not is_checked():
            label_candidates = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class,'sapMDialog')]//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'term') "
                "or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'agree') "
                "or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'consent') "
                "or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'privacy')]",
            )
            for candidate in reversed(label_candidates):
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", candidate)
                    ActionChains(self.driver).move_to_element(candidate).pause(0.2).click().perform()
                    time.sleep(0.4)
                    if is_checked():
                        break
                except Exception:
                    continue

        if not is_checked():
            self._screenshot("06_terms_checkbox_error")
            raise Exception("Terms checkbox remained unchecked after SAP API and DOM click attempts")

        print(
            "Checkbox state: "
            f"aria-checked={checkbox.get_attribute('aria-checked')} "
            f"checked={checkbox.get_attribute('checked')}"
        )

    def _press_dialog_button(self, text):
        result = self.driver.execute_script(
            """
            try {
                function visible(el) {
                    if (!el) return false;
                    var style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    var rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }
                var wanted = arguments[0];
                var allControls = Object.values(sap.ui.getCore().mElements || {});
                for (var c of allControls) {
                    if (!c.getMetadata || c.getMetadata().getName() !== 'sap.m.Button') continue;
                    if (!c.getText || c.getText().trim() !== wanted) continue;
                    if (c.getVisible && c.getVisible() === false) continue;
                    if (c.getEnabled && c.getEnabled() === false) continue;
                    var dom = c.getDomRef ? c.getDomRef() : null;
                    if (!visible(dom)) continue;
                    c.firePress();
                    return 'firePress:' + (c.getId ? c.getId() : wanted);
                }
                return 'not_found';
            } catch (e) {
                return 'error:' + e.message;
            }
            """,
            text,
        )
        print(f"Dialog button '{text}' result: {result}")
        if "firePress:" in str(result):
            return result

        js_dom_result = self.driver.execute_script(
            """
            try {
                function visible(el) {
                    if (!el) return false;
                    var style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    var rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }
                function labelOf(el) {
                    return (
                        el.getAttribute('aria-label') ||
                        el.getAttribute('title') ||
                        el.getAttribute('value') ||
                        el.innerText ||
                        el.textContent ||
                        ''
                    ).replace(/\\s+/g, ' ').trim();
                }
                function clickLikeUser(el) {
                    if (!el) return false;
                    el.scrollIntoView({block: 'center'});
                    ['mousedown', 'mouseup', 'click'].forEach(function (evt) {
                        if (typeof el.dispatchEvent === 'function') {
                            el.dispatchEvent(new MouseEvent(evt, {bubbles: true, cancelable: true, view: window}));
                        }
                    });
                    if (typeof el.click === 'function') el.click();
                    return true;
                }

                var wanted = String(arguments[0]).trim().toLowerCase();
                var dialog = document.querySelector('.sapMDialog, [role="dialog"]');
                if (!dialog) return {ok: false, reason: 'dialog_not_found'};

                var nodes = Array.from(dialog.querySelectorAll(
                    'button, input[type="button"], input[type="submit"], [role="button"], .sapMBtn, .sapMBtnBase'
                )).filter(visible);

                var labels = nodes.map(function (el) { return labelOf(el); }).filter(Boolean);
                var target = nodes.find(function (el) {
                    return labelOf(el).toLowerCase() === wanted;
                });
                if (!target) {
                    target = nodes.find(function (el) {
                        return labelOf(el).toLowerCase().includes(wanted);
                    });
                }
                if (!target) {
                    return {ok: false, reason: 'dom_not_found', labels: labels};
                }

                clickLikeUser(target);
                return {ok: true, source: 'dom_scan', label: labelOf(target)};
            } catch (e) {
                return {ok: false, reason: e.message};
            }
            """,
            text,
        )
        print(f"Dialog button '{text}' DOM scan result: {js_dom_result}")
        if isinstance(js_dom_result, dict) and js_dom_result.get("ok"):
            return js_dom_result

        js_page_result = self.driver.execute_script(
            """
            try {
                function visible(el) {
                    if (!el) return false;
                    var style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    var rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }
                function labelOf(el) {
                    return (
                        el.getAttribute('aria-label') ||
                        el.getAttribute('title') ||
                        el.getAttribute('value') ||
                        el.innerText ||
                        el.textContent ||
                        ''
                    ).replace(/\\s+/g, ' ').trim();
                }
                function clickableOf(el) {
                    var node = el;
                    while (node) {
                        var tag = (node.tagName || '').toLowerCase();
                        var role = node.getAttribute ? node.getAttribute('role') : null;
                        var cls = node.className || '';
                        if (
                            tag === 'button' ||
                            tag === 'a' ||
                            tag === 'input' ||
                            role === 'button' ||
                            (typeof cls === 'string' && (cls.indexOf('sapMBtn') >= 0 || cls.indexOf('sapMBtnBase') >= 0))
                        ) {
                            return node;
                        }
                        node = node.parentElement;
                    }
                    return el;
                }
                function clickLikeUser(el) {
                    if (!el) return false;
                    el.scrollIntoView({block: 'center'});
                    ['mousedown', 'mouseup', 'click'].forEach(function (evt) {
                        if (typeof el.dispatchEvent === 'function') {
                            el.dispatchEvent(new MouseEvent(evt, {bubbles: true, cancelable: true, view: window}));
                        }
                    });
                    if (typeof el.click === 'function') el.click();
                    return true;
                }

                var wanted = String(arguments[0]).trim().toLowerCase();
                var candidates = Array.from(document.querySelectorAll('button, input, a, span, div, bdi'))
                    .filter(visible)
                    .map(function (el) {
                        var label = labelOf(el);
                        var rect = el.getBoundingClientRect();
                        return {el: el, label: label, top: rect.top, left: rect.left};
                    })
                    .filter(function (item) {
                        var label = item.label.toLowerCase();
                        return label === wanted || label.indexOf(wanted) >= 0;
                    });

                var labels = candidates.map(function (item) { return item.label; });
                if (!candidates.length) {
                    return {ok: false, reason: 'page_not_found', labels: labels};
                }

                candidates.sort(function (a, b) {
                    if (b.top !== a.top) return b.top - a.top;
                    return b.left - a.left;
                });

                var target = clickableOf(candidates[0].el);
                clickLikeUser(target);
                return {
                    ok: true,
                    source: 'page_scan',
                    label: candidates[0].label,
                    top: candidates[0].top,
                    left: candidates[0].left
                };
            } catch (e) {
                return {ok: false, reason: e.message};
            }
            """,
            text,
        )
        print(f"Dialog button '{text}' page scan result: {js_page_result}")
        if isinstance(js_page_result, dict) and js_page_result.get("ok"):
            return js_page_result

        dom_buttons = self.driver.find_elements(
            By.XPATH,
            f"//div[contains(@class,'sapMDialog')]//button[normalize-space()='{text}'] | "
            f"//div[contains(@class,'sapMDialog')]//*[normalize-space()='{text}']/ancestor::button[1] | "
            f"//div[contains(@class,'sapMDialog')]//bdi[normalize-space()='{text}']/ancestor::button[1] | "
            f"//div[contains(@class,'sapMDialog')]//span[normalize-space()='{text}']/ancestor::button[1] | "
            f"//div[contains(@class,'sapMDialog')]//input[@type='button' and @value='{text}'] | "
            f"//div[contains(@class,'sapMDialog')]//*[@role='button' and normalize-space()='{text}'] | "
            f"//div[contains(@class,'sapMDialog')]//*[@role='button'][.//*[normalize-space()='{text}']]",
        )
        if not dom_buttons:
            raise Exception(f"Unable to locate dialog button '{text}'")

        button = dom_buttons[0]
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
        time.sleep(0.3)
        try:
            ActionChains(self.driver).move_to_element(button).pause(0.2).click().perform()
        except Exception:
            self.driver.execute_script(
                """
                var el = arguments[0];
                ['mousedown', 'mouseup', 'click'].forEach(function (evt) {
                    if (el && typeof el.dispatchEvent === 'function') {
                        el.dispatchEvent(new MouseEvent(evt, {bubbles: true, cancelable: true, view: window}));
                    }
                });
                if (el && typeof el.click === 'function') el.click();
                """,
                button,
            )
        return "dom_click"

    # =========================
    # FIND & OPEN JOB
    # =========================
    def find_and_open_job(self, req_id):
        req_id = str(req_id).strip()
        normalized_req = re.sub(r"\s+", "", req_id).lower()
        print(f"Searching Requisition ID: {req_id}")

        container = self.wait.until(
            EC.presence_of_element_located((By.XPATH, "//section[contains(@class,'sapMPageEnableScrolling')]"))
        )

        for i in range(50):
            jobs = self.driver.find_elements(By.XPATH, "//li[contains(@class,'sapMLIB')]")
            print(f"Iteration {i + 1} | Jobs visible: {len(jobs)}")

            target_idx = None
            for idx, job in enumerate(jobs):
                try:
                    job_text = re.sub(r"\s+", "", job.text or "").lower()
                    if normalized_req in job_text:
                        target_idx = idx
                        print(f"Found JR {req_id} at index {idx}")
                        break
                except Exception:
                    continue

            if target_idx is not None:
                target = jobs[target_idx]
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target)
                time.sleep(0.5)

                activation = self._activate_sap_control_from_element(target)
                print(f"SAP control activation: {activation}")

                try:
                    ActionChains(self.driver).move_to_element(target).pause(0.2).click().perform()
                except Exception:
                    pass

                self.driver.execute_script(
                    """
                    var items = document.querySelectorAll("li.sapMLIB");
                    var el = items[arguments[0]];
                    if (el) {
                        el.scrollIntoView({block: 'center'});
                        el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                        el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                        el.click();
                    }
                    """,
                    target_idx,
                )

                matched, snippet = self._wait_for_details_panel(req_id, timeout=15)
                if matched:
                    print(f"Opened Requisition ID: {req_id}")
                    print(f"Details panel: {snippet}")
                    self._screenshot("02_job_opened_and_verified")
                    return True

                print(f"Details panel did not update for JR {req_id}. Last snippet: {snippet}")
                self._screenshot("02_job_open_failed")
                return False

            self.driver.execute_script("arguments[0].scrollBy(0, 300);", container)
            time.sleep(1.5)

        print(f"Requisition ID {req_id} not found after scrolling")
        self._screenshot("02_job_not_found")
        return False

    def _open_add_candidate_form(self, jr_number):
        matched, snippet = self._wait_for_details_panel(jr_number, timeout=10)
        if not matched:
            raise Exception(f"Right panel did not show JR {jr_number}. Last panel text: {snippet}")
        time.sleep(2)

        all_btns = self.driver.find_elements(By.XPATH, "//button | //*[@role='button']")
        print(f"Buttons on page: {len(all_btns)}")

        try:
            menu_btn = None
            selectors = [
                (By.XPATH, "//span[@aria-label='Actions']"),
                (By.XPATH, "//button[contains(@id,'overflowButton')]"),
                (By.XPATH, "//button[contains(@id,'action')]"),
                (By.XPATH, "//button[contains(@class,'sapMBtn')][contains(@id,'action')]"),
                (By.XPATH, "//*[contains(@class,'sapUiIcon')][contains(@src,'overflow') or contains(@data-sap-ui,'overflow')]"),
                (By.CSS_SELECTOR, "button[id*='action'], button[id*='Action'], button[id*='overflow']"),
            ]
            for by, selector in selectors:
                matches = self.driver.find_elements(by, selector)
                if matches:
                    menu_btn = matches[0]
                    print(f"Actions button found via: {selector}")
                    break

            if not menu_btn:
                panel_btns = self.driver.find_elements(
                    By.XPATH, "//div[contains(@class,'sapMFlexBox') or contains(@class,'sapMPage')]//button"
                )
                if panel_btns:
                    menu_btn = panel_btns[-1]
                    print(f"Using last panel button: {menu_btn.get_attribute('id')}")

            if not menu_btn:
                self._screenshot("03_actions_button_error")
                raise Exception("Cannot find Actions button with any selector")

            self.driver.execute_script(
                """
                arguments[0].dispatchEvent(new MouseEvent('click', {
                    bubbles: true, cancelable: true, view: window
                }));
                """,
                menu_btn,
            )
            time.sleep(2)
            self._screenshot("03_actions_menu_opened")
        except Exception as e:
            self._screenshot("03_actions_button_error")
            raise Exception(f"Actions button not found: {e}")

        try:
            submit_el = self.wait.until(
                EC.presence_of_element_located((By.XPATH, "//*[contains(text(),'Submit New Candidate')]"))
            )
            clickable = self.driver.execute_script(
                """
                var el = arguments[0];
                while (el) {
                    var tag = el.tagName.toLowerCase();
                    var role = el.getAttribute('role');
                    if (tag === 'button' || tag === 'a' || role === 'button' || role === 'menuitem' || role === 'option') {
                        return el;
                    }
                    el = el.parentElement;
                }
                return arguments[0];
                """,
                submit_el,
            )

            ctrl_id = clickable.get_attribute("id")
            result = self.driver.execute_script(
                f"""
                try {{
                    var ctrl = sap.ui.getCore().byId('{ctrl_id}');
                    if (ctrl && ctrl.firePress) {{
                        ctrl.firePress();
                        return 'firePress:ok';
                    }}
                    if (ctrl && ctrl.ontap) {{
                        ctrl.ontap({{srcControl: ctrl}});
                        return 'ontap:ok';
                    }}
                    return 'ctrl_not_found';
                }} catch(e) {{ return 'error:' + e.message; }}
                """
            )
            print(f"Submit New Candidate click result: {result}")
            self.driver.execute_script("arguments[0].click();", clickable)
            time.sleep(3)
            self._screenshot("04_submit_new_candidate_clicked")
        except Exception as e:
            self._screenshot("04_submit_new_candidate_error")
            raise Exception(f"Submit New Candidate click failed: {e}")

        try:
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.XPATH, "//input[@placeholder='Please enter first name.']"))
            )
        except Exception as e:
            self._screenshot("04_form_not_opened")
            raise Exception(f"Add Candidate form did not open: {e}")

        print("Add Candidate form opened")

    # =========================
    # FILL & SUBMIT FORM
    # =========================
    def upload_candidate(self, data):
        """
        data keys: jr_number, first_name, last_name, email, phone,
                   country_code (+91), country (India), resume_path
        """
        jr = str(data["jr_number"]).strip()

        if not self.find_and_open_job(jr):
            raise Exception(f"Requisition ID {jr} not found in job list")

        try:
            self._open_add_candidate_form(jr)
        except Exception as e:
            raise Exception(f"Failed to open Add Candidate form: {e}")

        try:
            self._fill("//input[@placeholder='Please enter first name.']", data["first_name"])
            self._fill("//input[@placeholder='Please enter last name.']", data["last_name"])
            self._fill("//input[@placeholder='Please enter email.']", data["email"])
            self._fill("//input[@placeholder='Re-enter the email address']", data["email"])
            self._fill("//input[@placeholder='Please enter phone number']", data["phone"])
        except Exception as e:
            raise Exception(f"Failed to fill text fields: {e}")

        try:
            r1 = self._sap_select("phoneCodeDlgFld", data.get("country_code", "+91"), by="key")
            print(f"Country code: {r1}")
            r2 = self._sap_select("countryDlgFld", data.get("country", "India"), by="text")
            print(f"Country: {r2}")
        except Exception as e:
            raise Exception(f"Failed to set dropdowns: {e}")

        try:
            self.driver.find_element(By.XPATH, "//input[@type='file']").send_keys(data["resume_path"])
            time.sleep(1)
            print("Resume uploaded")
            self._screenshot("05_form_filled_resume_uploaded")
        except Exception as e:
            raise Exception(f"Failed to upload resume: {e}")

        try:
            self._set_terms_checkbox()
            self._screenshot("06_terms_checked")
        except Exception as e:
            raise Exception(f"Failed to check terms checkbox: {e}")

        time.sleep(5)

        if data.get("submit", True):
            self._screenshot("07_before_add_candidate")
            self._press_dialog_button("Add Candidate")
            try:
                WebDriverWait(self.driver, 15).until(
                    EC.invisibility_of_element_located((By.XPATH, "//div[contains(@class,'sapMDialog')]"))
                )
                print(f"Candidate submitted for JR {jr}")
                self._screenshot("08_after_add_candidate")
            except Exception:
                raise Exception("Dialog did not close after submission - verify manually")
        else:
            self._screenshot("07_before_cancel")
            self._press_dialog_button("Cancel")
            try:
                WebDriverWait(self.driver, 15).until(
                    EC.invisibility_of_element_located((By.XPATH, "//div[contains(@class,'sapMDialog')]"))
                )
            except Exception:
                raise Exception("Dialog did not close after cancel - verify manually")
            print(f"Cancelled form for JR {jr} (dry run)")
            self._screenshot("08_after_cancel")
