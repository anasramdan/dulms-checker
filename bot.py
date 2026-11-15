import os
import json
import time
import html
import hmac
import hashlib
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright


# ================= الإعدادات =================
def _load_local_env(path=".env.local"):
    """للتشغيل على جهازك بس: بيقرا القيم من ملف .env.local (مش بيترفع على GitHub)"""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_local_env()


def need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"المتغير {name} ناقص. ضيفه في GitHub Secrets (أو في ملف .env.local على جهازك).")
    return v


DULMS_USER = need("DULMS_USER")
DULMS_PASS = need("DULMS_PASS")
GREEN_ID = need("GREEN_INSTANCE_ID")
GREEN_TOKEN = need("GREEN_API_TOKEN")
_raw_number = need("WHATSAPP_NUMBER")

# تنظيف الرقم: لو كتبته 010... بيتحول لـ 2010... تلقائياً
WHATSAPP_NUMBER = "".join(ch for ch in _raw_number if ch.isdigit())
if WHATSAPP_NUMBER.startswith("0"):
    WHATSAPP_NUMBER = "20" + WHATSAPP_NUMBER[1:]

CHAT_ID = f"{WHATSAPP_NUMBER}@c.us"
API_URL = os.environ.get("GREEN_API_URL", "").strip() or f"https://{GREEN_ID[:4]}.api.greenapi.com"
MEDIA_URL = os.environ.get("GREEN_MEDIA_URL", "").strip() or API_URL
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
FORCE_SEND = os.environ.get("FORCE_SEND", "false").lower() == "true"

BASE = "https://dulms.deltauniv.edu.eg"
STATE_FILE = "state.json"
FAIL_ALERT_AFTER = 3   # يبعتلك تنبيه خطأ بعد 3 فشلات متتالية بس (علشان ميزعجكش كل 5 دقايق)


def scrub(text):
    """يشيل الأسرار من أي رسالة قبل ما تتبعت أو تتطبع"""
    for secret in (GREEN_TOKEN, DULMS_PASS, WHATSAPP_NUMBER, _raw_number):
        if secret:
            text = text.replace(secret, "***")
    return text


# ================= الواتس اب =================
def send_text(message):
    url = f"{API_URL}/waInstance{GREEN_ID}/sendMessage/{GREEN_TOKEN}"
    r = requests.post(url, json={"chatId": CHAT_ID, "message": message}, timeout=60)
    print(f"[WA] text -> {r.status_code}")
    return r.ok


def send_pdf(path, caption):
    url = f"{MEDIA_URL}/waInstance{GREEN_ID}/sendFileByUpload/{GREEN_TOKEN}"
    with open(path, "rb") as f:
        r = requests.post(
            url,
            data={"chatId": CHAT_ID, "caption": caption},
            files={"file": (os.path.basename(path), f, "application/pdf")},
            timeout=180,
        )
    print(f"[WA] {path} -> {r.status_code}")
    return r.ok


# ================= حفظ ومقارنة الحالة (مشفّرة) =================
def _h(s):
    """بصمة مشفّرة: ملف state.json مش بيحتوي درجات حقيقية، حتى لو الـ repo Public"""
    return hmac.new(GREEN_TOKEN.encode(), s.encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def flatten(courses, key_fn, val_fn):
    out, seen = {}, {}
    for c in courses:
        for r in c["rows"]:
            k = f"{c['courseName']} | {key_fn(r)}"
            seen[k] = seen.get(k, 0) + 1
            if seen[k] > 1:
                k += f" #{seen[k]}"
            out[k] = val_fn(r)
    return out


def diff(old_hashed, new_plain):
    added, changed = [], []
    for k, v in new_plain.items():
        hk = _h(k)
        if hk not in old_hashed:
            added.append((k, v))
        elif old_hashed[hk] != _h(v):
            changed.append((k, v))
    return added, changed


# ================= تصميم الـ PDF =================
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700&display=swap');
* { box-sizing: border-box; }
body { font-family: 'Cairo','Noto Sans Arabic','Noto Naskh Arabic',Arial,sans-serif;
       direction: rtl; color: #1f2937; margin: 0; font-size: 12px; }
.header { background: linear-gradient(135deg,#1e3a8a,#3b82f6); color: #fff;
          padding: 18px 22px; border-radius: 12px; margin-bottom: 14px; }
.header h1 { margin: 0 0 4px; font-size: 22px; }
.header p { margin: 0; opacity: .9; font-size: 11px; }
.summary { display: flex; gap: 10px; margin-bottom: 14px; }
.summary div { flex: 1; background: #eff6ff; border: 1px solid #bfdbfe;
               border-radius: 10px; padding: 8px 12px; text-align: center; }
.summary b { display: block; font-size: 18px; color: #1e3a8a; }
.course { margin-bottom: 16px; page-break-inside: avoid; }
.course h2 { background: #1e3a8a; color: #fff; margin: 0; padding: 8px 14px;
             font-size: 14px; border-radius: 8px 8px 0 0; }
table { width: 100%; border-collapse: collapse; }
th { background: #dbeafe; color: #1e3a8a; padding: 7px 8px; font-size: 11px; text-align: right; }
td { padding: 6px 8px; border-bottom: 1px solid #e5e7eb; }
tr:nth-child(even) td { background: #f9fafb; }
.good { color: #047857; font-weight: 700; }
.mid  { color: #b45309; font-weight: 700; }
.bad  { color: #b91c1c; font-weight: 700; }
.empty { text-align: center; color: #6b7280; padding: 30px; }
"""


def status_class(text):
    t = (text or "").lower()
    if any(w in t for w in ["not submitted", "missing", "late", "overdue", "لم"]):
        return "bad"
    if any(w in t for w in ["submitted", "graded", "attempted", "finished", "تم"]):
        return "good"
    return ""


def percent_class(text):
    try:
        v = float("".join(c for c in text if c.isdigit() or c == "."))
    except ValueError:
        return ""
    return "good" if v >= 75 else "mid" if v >= 50 else "bad"


def build_html(title, courses, headers, class_fns, total_label):
    now = datetime.now(ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d  %I:%M %p")
    total_rows = sum(len(c["rows"]) for c in courses)
    body = ""
    for c in courses:
        body += f"<div class='course'><h2>{html.escape(c['courseName'])}</h2><table><tr>"
        body += "".join(f"<th>{h}</th>" for h in headers) + "</tr>"
        for row in c["rows"]:
            body += "<tr>"
            for i, cell in enumerate(row):
                fn = class_fns[i] if i < len(class_fns) else None
                cls = fn(cell) if fn else ""
                body += f"<td class='{cls}'>{html.escape(cell or '--')}</td>"
            body += "</tr>"
        body += "</table></div>"
    if not courses:
        body = "<div class='empty'>لا توجد بيانات متاحة حالياً</div>"
    return f"""<html lang='ar' dir='rtl'><head><meta charset='utf-8'><style>{CSS}</style></head><body>
    <div class='header'><h1>{title}</h1><p>Delta University LMS — آخر تحديث: {now}</p></div>
    <div class='summary'>
      <div><b>{len(courses)}</b>عدد المواد</div>
      <div><b>{total_rows}</b>{total_label}</div>
    </div>{body}</body></html>"""


def render_pdfs(sections):
    """بيعمل ملفات الـ PDF للأقسام المطلوبة بس (مش في كل فحص، علشان يوفر وقت)"""
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)   # الـ PDF بيشتغل في headless بس
        context = browser.new_context(locale="ar-EG", timezone_id="Africa/Cairo")
        try:
            for s in sections:
                pg = context.new_page()
                pg.set_content(s["html"], wait_until="networkidle")
                pg.pdf(
                    path=s["pdf"], format="A4", print_background=True,
                    margin={"top": "14mm", "bottom": "16mm", "left": "10mm", "right": "10mm"},
                    display_header_footer=True,
                    header_template="<span></span>",
                    footer_template="<div style='font-size:9px;width:100%;text-align:center;color:#6b7280'>"
                                    "<span class='pageNumber'></span> / <span class='totalPages'></span></div>",
                )
                pg.close()
                print(f"[+] تم إنشاء {s['pdf']}")
                out.append((s["pdf"], s["caption"]))
        finally:
            browser.close()
    return out


# ================= السحب من الموقع =================
def open_page(page, path, selector):
    page.goto(f"{BASE}{path}", timeout=60000)
    try:
        page.wait_for_selector(selector, timeout=15000)
    except Exception:
        pass
    time.sleep(3)


GRADES_JS = """() => {
    let courses = [];
    document.querySelectorAll('.grp-item').forEach(item => {
        let nameEl = item.querySelector('.grp-name');
        if (!nameEl) return;
        let rows = [];
        item.querySelectorAll('tbody tr').forEach(row => {
            let c = row.querySelectorAll('td');
            if (c.length >= 9) rows.push([
                c[2].innerText.trim(), c[3].innerText.trim(), c[5].innerText.trim(),
                c[4].innerText.trim(), c[6].innerText.trim()
            ]);
        });
        if (rows.length) courses.push({courseName: nameEl.textContent.trim(), rows});
    });
    return courses;
}"""


def items_js(item_sel, item_name_sel, default_name, default_grade, date_sel):
    return f"""() => {{
    let courses = [];
    document.querySelectorAll('.course-item').forEach(item => {{
        let nameEl = item.querySelector('.course-name');
        if (!nameEl) return;
        let rows = [];
        item.querySelectorAll('{item_sel}').forEach(q => {{
            rows.push([
                q.querySelector('{item_name_sel}')?.innerText.trim() || '{default_name}',
                q.querySelector('.submit-status')?.innerText.trim() || '',
                q.querySelector('{date_sel}')?.innerText.trim() || '',
                q.querySelector('.graded-status')?.innerText.trim() || '{default_grade}'
            ]);
        }});
        courses.push({{courseName: nameEl.textContent.trim(), rows}});
    }});
    return courses;
}}"""


def scrape_all():
    sections = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(locale="ar-EG", timezone_id="Africa/Cairo")
        page = context.new_page()
        try:
            print("[*] تسجيل الدخول...")
            page.goto(BASE, timeout=60000)
            page.wait_for_load_state("networkidle")
            page.fill("input[placeholder*='User Name']", DULMS_USER)
            page.fill("input[placeholder*='Password']", DULMS_PASS)
            page.get_by_text("Login", exact=True).click()
            page.wait_for_load_state("networkidle")
            print("[+] تم تسجيل الدخول")

            # الدرجات
            open_page(page, "/GradeBook/StudentGradeBook", ".grp-item")
            grades = page.evaluate(GRADES_JS)
            sections["grades"] = dict(
                courses=grades, title="📊 الدرجات", caption="📊 تقرير الدرجات", pdf="grades_report.pdf",
                html=build_html("📊 تقرير الدرجات", grades,
                                ["النوع", "الاسم", "درجتك", "الدرجة الكاملة", "النسبة"],
                                [None, None, None, None, percent_class], "عدد التقييمات"),
                key_fn=lambda r: f"{r[0]} - {r[1]}",
                val_fn=lambda r: f"{r[2] or '--'} / {r[3]} ({r[4]})",
            )

            # الكويزات
            open_page(page, "/Quizzes/StudentQuizzes", ".course-item")
            quizzes = page.evaluate(items_js(".quiz-item", ".quiz-name", "Quiz", "--", ".quiz-status"))
            sections["quizzes"] = dict(
                courses=quizzes, title="📝 الكويزات", caption="📝 تقرير الكويزات", pdf="quizzes_report.pdf",
                html=build_html("📝 تقرير الكويزات", quizzes,
                                ["الكويز", "الحالة", "الموعد", "الدرجة"],
                                [None, status_class, None, None], "عدد الكويزات"),
                key_fn=lambda r: r[0],
                val_fn=lambda r: f"{r[1] or '--'} | الدرجة: {r[3]}",   # من غير الموعد عمداً
            )

            # الواجبات
            open_page(page, "/Assignment/AssignmentStudentList", ".course-item")
            assigns = page.evaluate(items_js(".assignment-item", ".assign-name", "Assignment", "Not Graded", ".assign-status"))
            sections["assignments"] = dict(
                courses=assigns, title="📚 الواجبات", caption="📚 تقرير الواجبات", pdf="assignments_report.pdf",
                html=build_html("📚 تقرير الواجبات", assigns,
                                ["الواجب", "الحالة", "الموعد", "التقييم"],
                                [None, status_class, None, None], "عدد الواجبات"),
                key_fn=lambda r: r[0],
                val_fn=lambda r: f"{r[1] or '--'} | التقييم: {r[3]}",
            )
        finally:
            browser.close()
    return sections


# ================= المنطق الرئيسي =================
def run():
    sections = scrape_all()

    if not any(s["courses"] for s in sections.values()):
        raise RuntimeError("ما قدرتش أسحب أي بيانات (ممكن الموقع واقف أو تسجيل الدخول فشل)")

    state = load_state() or {}
    first_run = not any(name in state for name in sections)
    prev_fails = state.get("_fails", 0)
    new_state = dict(state)
    new_state["_fails"] = 0

    lines, to_send = [], []
    for name, s in sections.items():
        if not s["courses"]:
            print(f"[!] قسم {name} رجع فاضي، هتجاهله ومش هغيّر حالته القديمة")
            continue
        plain = flatten(s["courses"], s["key_fn"], s["val_fn"])
        added, changed = diff(state.get(name, {}), plain)
        new_state[name] = {_h(k): _h(v) for k, v in plain.items()}

        if first_run or FORCE_SEND or added or changed:
            to_send.append(s)
            if not first_run:
                lines.append(f"\n*{s['title']}*")
                for k, v in added:
                    lines.append(f"🆕 {k}\n   {v}")
                for k, v in changed:
                    lines.append(f"🔄 {k}\n   بقى: {v}")

    recovered = prev_fails >= FAIL_ALERT_AFTER

    if not to_send:
        print("[=] مفيش جديد، مش هبعت حاجة.")
        if recovered:
            send_text("✅ البوت رجع يشتغل طبيعي بعد ما كان واقف.")
        save_state(new_state)
        return

    if first_run:
        text = "👋 أهلاً يا أنس! البوت اشتغل لأول مرة، دي كل بياناتك الحالية. من النهاردة هبعتلك بس لما يظهر جديد."
    else:
        text = "🔔 فيه تحديثات جديدة في DULMS:\n" + "\n".join(lines[:60])
        if len(lines) > 60:
            text += "\n... وغيرها (شوف الملفات)"
    if recovered:
        text = "✅ البوت رجع يشتغل بعد ما كان واقف.\n\n" + text

    pdfs = render_pdfs(to_send)
    ok = send_text(text)
    for path, caption in pdfs:
        ok = send_pdf(path, caption) and ok

    if ok:
        save_state(new_state)   # بنحفظ الحالة بس لو الإرسال نجح
    else:
        raise RuntimeError("فشل إرسال الواتس اب، هيحاول تاني في الفحص الجاي")


def handle_failure(msg):
    """يعد الفشلات المتتالية، ويبعت تنبيه مرة واحدة بس بعد 3 فشلات"""
    state = load_state() or {}
    prev = state.get("_fails", 0)
    fails = min(prev + 1, FAIL_ALERT_AFTER)
    state["_fails"] = fails
    save_state(state)
    if prev == FAIL_ALERT_AFTER - 1:
        send_text(f"⚠️ بوت DULMS بيفشل من {FAIL_ALERT_AFTER} فحوصات ورا بعض:\n{msg[:300]}")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        msg = scrub(str(e))
        print(f"[-] خطأ: {msg}")
        try:
            handle_failure(msg)
        except Exception as e2:
            print(f"[-] فشل تسجيل الخطأ: {scrub(str(e2))}")
        # في التشغيل اليدوي بنظهر الفشل بالأحمر، وفي الأوتوماتيك لأ (علشان GitHub ميبعتش إيميلات كل 5 دقايق)
        if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
            raise SystemExit(1)