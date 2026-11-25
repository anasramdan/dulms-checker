import os
import re
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
TELEGRAM_TOKEN = need("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = need("TELEGRAM_CHAT_ID")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
CAIRO = ZoneInfo("Africa/Cairo")
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
FORCE_SEND = os.environ.get("FORCE_SEND", "false").lower() == "true"

BASE = "https://dulms.deltauniv.edu.eg"
STATE_FILE = "state.json"
FAIL_ALERT_AFTER = 3        # يبعتلك تنبيه خطأ بعد 3 فشلات متتالية بس
STATE_VERSION = 2           # نسخة التشفير: اتغيرت لما انتقلنا من واتساب لتليجرام
PDF_EVERY_DAYS = 7          # تقرير الـ PDF مرة واحدة في الأسبوع
MAX_ITEM_MSGS = 40          # أقصى عدد رسايل "عنصر جديد" في الفحص الواحد
MAX_REMINDER_MSGS = 20      # أقصى عدد رسايل تذكير مواعيد في الفحص الواحد
# تنبيهات المواعيد: بتتبعت قبل القفل بـ يوم، وبعدين ساعتين، وبعدين ساعة
# (لكل كويز وواجب لسه ماعملتوش، وكل تنبيه بيتبعت مرة واحدة بس)
REMINDER_WINDOWS = [
    ("d1", 24 * 3600, "يوم"),
    ("h2", 2 * 3600, "ساعتين"),
    ("h1", 3600, "ساعة"),
]
NAG_INTERVAL = 600          # في آخر ساعة قبل قفل الواجب: زنّ كل 10 دقايق لحد ما يتسلّم
QUIZ_LAST_CALL = 300        # تنبيه أخير للكويز قبل الموعد بـ 5 دقايق


def scrub(text):
    """يشيل الأسرار من أي رسالة قبل ما تتبعت أو تتطبع"""
    for secret in (TELEGRAM_TOKEN, DULMS_PASS, TELEGRAM_CHAT_ID):
        if secret:
            text = text.replace(secret, "***")
    return text


def now_cairo():
    return datetime.now(CAIRO)


# ================= تليجرام =================
def _chunks(message, limit=3800):
    """تليجرام بيقبل 4096 حرف في الرسالة، فبنقصّر على حدود السطور"""
    out, cur = [], ""
    for line in message.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            if cur:
                out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [message[:limit]]


def send_text(message):
    ok = True
    for chunk in _chunks(message):
        r = requests.post(
            f"{TG_API}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk},
            timeout=60,
        )
        print(f"[TG] text -> {r.status_code}")
        if not r.ok:
            print(f"[TG] {scrub(r.text[:200])}")
        ok = r.ok and ok
        time.sleep(0.4)   # احترام حدود تليجرام بين الرسايل
    return ok


def send_pdf(path, caption):
    with open(path, "rb") as f:
        r = requests.post(
            f"{TG_API}/sendDocument",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000]},
            files={"document": (os.path.basename(path), f, "application/pdf")},
            timeout=180,
        )
    print(f"[TG] {path} -> {r.status_code}")
    if not r.ok:
        print(f"[TG] {scrub(r.text[:200])}")
    return r.ok


# ================= حفظ ومقارنة الحالة (مشفّرة) =================
def _h(s):
    """بصمة مشفّرة: ملف state.json مش بيحتوي درجات حقيقية، حتى لو الـ repo Public"""
    return hmac.new(TELEGRAM_TOKEN.encode(), s.encode("utf-8"), hashlib.sha256).hexdigest()[:20]


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
    """بيرجع قاموس مفتاح->قيمة للمقارنة، وقاموس مفتاح->الصف الخام علشان نبني منه الرسايل"""
    out, rows_by_key, seen = {}, {}, {}
    for c in courses:
        for r in c["rows"]:
            k = f"{c['courseName']} | {key_fn(r)}"
            seen[k] = seen.get(k, 0) + 1
            if seen[k] > 1:
                k += f" #{seen[k]}"
            out[k] = val_fn(r)
            rows_by_key[k] = (c["courseName"], r)
    return out, rows_by_key


def diff(old_hashed, new_plain):
    added, changed = [], []
    for k, v in new_plain.items():
        hk = _h(k)
        if hk not in old_hashed:
            added.append(k)
        elif old_hashed[hk] != _h(v):
            changed.append(k)
    return added, changed


# ================= الوقت المتبقي =================
# ملحوظة مهمة: البيانات اللي بنسحبها من DULMS فيها خانة نص واحدة بس للموعد
# (زي "Due: 30/11/2026 11:59 PM" مثلاً). مفيش حقول منفصلة لتاريخ الفتح أو القفل.
# الكود بيحاول يفهم الخانة دي: لو لقى فيها تاريخ يفهمه بيحسب الوقت المتبقي،
# ولو مفيش بيبعت الرسالة من غير سطر الوقت خالص (مش بيخترع مواعيد).
DATE_RE = re.compile(
    r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})"
    r"(\s+\d{1,2}:\d{2}(\s*(AM|PM|am|pm))?)?"
)

DATE_FORMATS = [
    "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p",
    "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M %p", "%d/%m/%Y %I:%M%p",
    "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M",
    "%d-%m-%Y %H:%M", "%d-%m-%Y %I:%M %p",
    "%d %b %Y %I:%M %p", "%d %B %Y %I:%M %p", "%d %b %Y", "%d %B %Y",
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
]


def parse_due(text):
    """بيحاول يطلّع تاريخ من نص الموعد. بيرجع None لو ملقاش تاريخ يفهمه."""
    if not text:
        return None
    m = DATE_RE.search(text)
    if not m:
        return None
    s = re.sub(r"\s+", " ", m.group(0)).strip()
    for fmt in DATE_FORMATS:
        try:
            # الموقع بيشتغل بتوقيت القاهرة (المتصفح متظبط على Africa/Cairo)
            return datetime.strptime(s, fmt).replace(tzinfo=CAIRO)
        except ValueError:
            continue
    return None


def _unit(n, one, two, many):
    if n == 1:
        return one
    if n == 2:
        return two
    if 3 <= n <= 10:
        return f"{n} {many}"
    return f"{n} {one}"


def fmt_remaining(dt, now):
    """صياغة الوقت المتبقي بالمصري: 'باقي يومين و 3 ساعات' أو 'فات من ساعة'"""
    secs = int((dt - now).total_seconds())
    past = secs < 0
    secs = abs(secs)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(_unit(days, "يوم", "يومين", "أيام"))
    if hours and len(parts) < 2:
        parts.append(_unit(hours, "ساعة", "ساعتين", "ساعات"))
    if not parts:
        parts.append(_unit(minutes, "دقيقة", "دقيقتين", "دقايق") if minutes else "أقل من دقيقة")
    text = " و ".join(parts[:2])
    return f"فات من {text}" if past else f"باقي {text}"


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
    now = now_cairo().strftime("%Y-%m-%d  %I:%M %p")
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
    """بيعمل ملفات الـ PDF للأقسام المطلوبة بس (التقرير الأسبوعي، مش كل فحص)"""
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


# ================= تذكير المواعيد القريبة =================
def is_done(status, grade):
    """بيقرر لو الكويز/الواجب اتعمل فعلاً، علشان التذكير يتبعت بس للي لسه ماعملتوش"""
    t = (status or "").lower()
    if any(w in t for w in ["not submitted", "not attempted", "no attempt", "missing", "لم"]):
        return False
    if any(w in t for w in ["submitted", "attempted", "finished", "completed", "done", "تم"]):
        return True
    g = (grade or "").strip().lower()
    if g and g not in ("--", "-", "not graded"):
        return True    # ليه درجة حقيقية يبقى اتعمل خلاص
    return False       # مفيش دليل إنه اتعمل: نفترض إنه لسه ونفكّرك


def reminder_msg(kind, course, row, label, dt, now):
    """رسالة تذكير بموعد قريب"""
    icon = "📝" if kind == "كويز" else "📚"
    word = "موعد" if kind == "كويز" else "قفل"
    lines = [f"⏰ فاضل {label} على {word} {kind} في مادة {course}", f"{icon} {row[0]}"]
    if row[2]:
        lines.append(f"الموعد: {row[2]}")
    lines.append(f"⏳ {fmt_remaining(dt, now)}")
    lines.append("⚠️ لسه ماعملتوش، متنساش")
    return "\n".join(lines)


def nag_msg(course, row, dt, now):
    """رسالة الزن في آخر ساعة قبل قفل الواجب"""
    lines = [f"🚨 الواجب في مادة {course} لسه ماتسلّمش!", f"📚 {row[0]}"]
    if row[2]:
        lines.append(f"الموعد: {row[2]}")
    lines.append(f"⏳ {fmt_remaining(dt, now)} وهيقفل")
    lines.append("سلّمه دلوقتي وهبطّل زن 🙏")
    return "\n".join(lines)


def due_reminder(section, key, kind, course, row, reminders_state, now):
    """بيقرر لو العنصر ده محتاج تذكير دلوقتي، وبيسجّل اللي اتبعت علشان ميتكررش.
    بيرجع قائمة رسايل (ممكن تكون فاضية)."""
    dt = parse_due(row[2])
    if not dt:
        return []    # التاريخ مش مفهوم: منقدرش نحسب موعد، فمنبعتش تذكير
    hk = _h(f"{section} | {key}")
    sent = list(reminders_state.get(hk, []))
    remaining = (dt - now).total_seconds()
    if remaining <= 0:
        # الموعد فات: سجّل إنه خلص علشان منفكّركش فيه تاني
        if "x" not in sent:
            reminders_state[hk] = sent + ["x"]
        return []
    if is_done(row[1], row[3]):
        return []

    msgs = []
    # 1) التنبيهات الأساسية: يوم / ساعتين / ساعة (كل واحدة مرة بس)
    idx = None
    for i, (_, window, _) in enumerate(REMINDER_WINDOWS):
        if remaining <= window:
            idx = i
    if idx is not None:
        marker, _, label = REMINDER_WINDOWS[idx]
        if marker not in sent:
            msgs.append(reminder_msg(kind, course, row, label, dt, now))
            # سجّل كل النوافذ اللي عدّت لحد هنا علشان منبعتش تذكير قديم بعدين
            sent = sorted(set(sent) | {m for m, _, _ in REMINDER_WINDOWS[: idx + 1]})
            if kind == "واجب" and marker == "h1":
                sent.append(f"nag@{int(now.timestamp())}")   # تنبيه الساعة بيحسب كأول زنّة

    # 2) الزن في آخر ساعة للواجب: كل شوية لحد ما يتسلّم فعلاً
    if kind == "واجب" and remaining <= 3600:
        last_nag = 0
        for m in sent:
            if m.startswith("nag@"):
                try:
                    last_nag = max(last_nag, int(m[4:]))
                except ValueError:
                    pass
        if now.timestamp() - last_nag >= NAG_INTERVAL:
            if not msgs:   # لو لسه باعث تنبيه الساعة في نفس الفحص، متزنّش تاني في نفس اللحظة
                msgs.append(nag_msg(course, row, dt, now))
            sent = [m for m in sent if not m.startswith("nag@")]
            sent.append(f"nag@{int(now.timestamp())}")

    # 3) الكويز: تنبيه أخير قبل الموعد بـ 5 دقايق
    if kind == "كويز" and remaining <= QUIZ_LAST_CALL and "m5" not in sent:
        msgs.append(reminder_msg(kind, course, row, "5 دقايق", dt, now))
        sent.append("m5")

    reminders_state[hk] = sent
    return msgs


# ================= رسايل العناصر الجديدة =================
def quiz_msg(course, row):
    """رسالة كويز جديد: الاسم والحالة والموعد، والوقت المتبقي لو التاريخ اتفهم"""
    lines = [f"🆕 كويز جديد في مادة {course}", f"📝 {row[0]}"]
    if row[1]:
        lines.append(f"الحالة: {row[1]}")
    if row[2]:
        lines.append(f"الموعد: {row[2]}")
    dt = parse_due(row[2])
    if dt:
        lines.append(f"⏳ {fmt_remaining(dt, now_cairo())}")
    if row[3] and row[3] != "--":
        lines.append(f"الدرجة: {row[3]}")
    return "\n".join(lines)


def assignment_msg(course, row):
    """رسالة واجب جديد: الاسم والحالة والموعد، وفاضل قد إيه يقفل (أو يفتح لو التاريخ لسه جاي)"""
    lines = [f"🆕 واجب جديد في مادة {course}", f"📚 {row[0]}"]
    if row[1]:
        lines.append(f"الحالة: {row[1]}")
    if row[2]:
        lines.append(f"الموعد: {row[2]}")
    dt = parse_due(row[2])
    if dt:
        lines.append(f"⏳ {fmt_remaining(dt, now_cairo())} على الموعد")
    if row[3] and row[3] not in ("--", "Not Graded"):
        lines.append(f"التقييم: {row[3]}")
    return "\n".join(lines)


def grade_msg(course, row):
    """رسالة تقييم/درجة جديدة"""
    lines = [f"🆕 تقييم جديد في مادة {course}", f"📊 {row[0]} - {row[1]}",
             f"درجتك: {row[2] or '--'} من {row[3] or '--'} ({row[4] or '--'})"]
    return "\n".join(lines)


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
                courses=grades, title="📊 الدرجات", caption="📊 تقرير الدرجات الأسبوعي", pdf="grades_report.pdf",
                html=build_html("📊 تقرير الدرجات", grades,
                                ["النوع", "الاسم", "درجتك", "الدرجة الكاملة", "النسبة"],
                                [None, None, None, None, percent_class], "عدد التقييمات"),
                key_fn=lambda r: f"{r[0]} - {r[1]}",
                val_fn=lambda r: f"{r[2] or '--'} / {r[3]} ({r[4]})",
                msg_fn=grade_msg,
            )

            # الكويزات
            open_page(page, "/Quizzes/StudentQuizzes", ".course-item")
            quizzes = page.evaluate(items_js(".quiz-item", ".quiz-name", "Quiz", "--", ".quiz-status"))
            sections["quizzes"] = dict(
                courses=quizzes, title="📝 الكويزات", caption="📝 تقرير الكويزات الأسبوعي", pdf="quizzes_report.pdf",
                html=build_html("📝 تقرير الكويزات", quizzes,
                                ["الكويز", "الحالة", "الموعد", "الدرجة"],
                                [None, status_class, None, None], "عدد الكويزات"),
                key_fn=lambda r: r[0],
                val_fn=lambda r: f"{r[1] or '--'} | الدرجة: {r[3]}",   # من غير الموعد عمداً
                msg_fn=quiz_msg,
            )

            # الواجبات
            open_page(page, "/Assignment/AssignmentStudentList", ".course-item")
            assigns = page.evaluate(items_js(".assignment-item", ".assign-name", "Assignment", "Not Graded", ".assign-status"))
            sections["assignments"] = dict(
                courses=assigns, title="📚 الواجبات", caption="📚 تقرير الواجبات الأسبوعي", pdf="assignments_report.pdf",
                html=build_html("📚 تقرير الواجبات", assigns,
                                ["الواجب", "الحالة", "الموعد", "التقييم"],
                                [None, status_class, None, None], "عدد الواجبات"),
                key_fn=lambda r: r[0],
                val_fn=lambda r: f"{r[1] or '--'} | التقييم: {r[3]}",
                msg_fn=assignment_msg,
            )
        finally:
            browser.close()
    return sections


# ================= المنطق الرئيسي =================
def weekly_pdf_due(state):
    """التقرير الأسبوعي مستحق لو مبعتوش من 7 أيام (أو خالص)"""
    last = state.get("_last_pdf")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (now_cairo() - last_dt).days >= PDF_EVERY_DAYS


def run():
    sections = scrape_all()

    if not any(s["courses"] for s in sections.values()):
        raise RuntimeError("ما قدرتش أسحب أي بيانات (ممكن الموقع واقف أو تسجيل الدخول فشل)")

    state = load_state() or {}
    first_run = not any(name in state for name in sections)
    # التشفير اتغير مع الانتقال لتليجرام: الحالة القديمة مش هتتعرف،
    # فبنحدّث الأساس بهدوء من غير ما نغرقك برسايل "جديد" لكل حاجة قديمة
    migrated = not first_run and state.get("_v") != STATE_VERSION
    prev_fails = state.get("_fails", 0)
    new_state = dict(state)
    new_state["_fails"] = 0
    new_state["_v"] = STATE_VERSION

    item_msgs, changed_lines, reminder_msgs = [], [], []
    reminders_state = dict(state.get("_reminders", {}))
    now = now_cairo()
    for name, s in sections.items():
        if not s["courses"]:
            print(f"[!] قسم {name} رجع فاضي، هتجاهله ومش هغيّر حالته القديمة")
            continue
        plain, rows_by_key = flatten(s["courses"], s["key_fn"], s["val_fn"])
        added, changed = diff(state.get(name, {}), plain)
        new_state[name] = {_h(k): _h(v) for k, v in plain.items()}

        # تذكير المواعيد القريبة للكويزات والواجبات (بيشتغل حتى في أول تشغيل، لأنه مرتبط بالوقت مش بالجديد)
        if name in ("quizzes", "assignments"):
            kind = "كويز" if name == "quizzes" else "واجب"
            for k, (course, row) in rows_by_key.items():
                reminder_msgs.extend(due_reminder(name, k, kind, course, row, reminders_state, now))

        if first_run or migrated:
            continue   # أول تشغيل أو ترقية التشفير: نسجّل الأساس بس
        for k in added:
            course, row = rows_by_key[k]
            item_msgs.append(s["msg_fn"](course, row))
        for k in changed:
            changed_lines.append(f"🔄 {k}\n   بقى: {plain[k]}")

    new_state["_reminders"] = reminders_state
    recovered = prev_fails >= FAIL_ALERT_AFTER
    pdf_due = weekly_pdf_due(state)

    nothing_new = not item_msgs and not changed_lines and not reminder_msgs
    if nothing_new and not first_run and not migrated and not FORCE_SEND and not pdf_due:
        print("[=] مفيش جديد، مش هبعت حاجة.")
        if recovered:
            send_text("✅ البوت رجع يشتغل طبيعي بعد ما كان واقف.")
        save_state(new_state)
        return

    ok = True

    # 1) رسالة منفصلة لكل عنصر جديد (بالوقت المتبقي لو موجود)
    if not first_run and not migrated:
        if recovered:
            ok = send_text("✅ البوت رجع يشتغل بعد ما كان واقف.") and ok
        for msg in item_msgs[:MAX_ITEM_MSGS]:
            ok = send_text(msg) and ok
        if len(item_msgs) > MAX_ITEM_MSGS:
            ok = send_text(f"... وفيه {len(item_msgs) - MAX_ITEM_MSGS} حاجة جديدة كمان، شوف التقرير.") and ok
        if changed_lines:
            text = "🔔 فيه تحديثات في DULMS:\n\n" + "\n\n".join(changed_lines[:40])
            ok = send_text(text) and ok

    # 1ب) تذكيرات المواعيد: قبل القفل بـ يوم وساعتين وساعة، بس للي لسه ماعملتوش
    for msg in reminder_msgs[:MAX_REMINDER_MSGS]:
        ok = send_text(msg) and ok
    if len(reminder_msgs) > MAX_REMINDER_MSGS:
        ok = send_text(f"... وفيه {len(reminder_msgs) - MAX_REMINDER_MSGS} موعد قريب كمان، شوف التقرير.") and ok

    if first_run:
        ok = send_text("👋 أهلاً يا أنس! البوت اشتغل على تليجرام لأول مرة، وسجّل بياناتك الحالية كأساس. "
                       "من النهاردة هبعتلك كل حاجة جديدة في رسالة لوحدها، والتقرير الكامل مرة في الأسبوع.") and ok
    elif migrated:
        ok = send_text("🔄 البوت اتحدّث واتنقل من واتساب لتليجرام. سجّل بياناتك الحالية من جديد بهدوء "
                       "من غير ما يزعجك، ومن دلوقتي التنبيهات هتوصلك هنا.") and ok

    # 2) تقرير الـ PDF: مرة واحدة في الأسبوع بس (وفي أول تشغيل ولما FORCE_SEND=true)
    if pdf_due or first_run or FORCE_SEND:
        pdf_sections = [s for s in sections.values() if s["courses"]]
        pdfs = render_pdfs(pdf_sections)
        pdf_ok = True
        for path, caption in pdfs:
            pdf_ok = send_pdf(path, caption) and pdf_ok
        if pdf_ok:
            new_state["_last_pdf"] = now_cairo().isoformat()
        else:
            ok = False   # هيعيد محاولة التقرير في الفحص الجاي

    if ok:
        save_state(new_state)   # بنحفظ الحالة بس لو الإرسال نجح
    else:
        raise RuntimeError("فشل إرسال تليجرام، هيحاول تاني في الفحص الجاي")


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
        # في التشغيل اليدوي بنظهر الفشل بالأحمر، وفي الأوتوماتيك لأ (علشان GitHub ميبعتش إيميلات كل دقيقة)
        if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
            raise SystemExit(1)
