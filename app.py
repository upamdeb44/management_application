import json
import os
import time as _time_module
import streamlit as st
import streamlit.components.v1 as components
from streamlit_calendar import calendar
from datetime import date, datetime, timedelta, time

from database import (
    create_table, create_user, authenticate_user,
    get_all_works, add_work, mark_completed, delete_work,
    get_work_by_task_id, update_work,
    create_session, get_user_by_session, delete_session, SESSION_DAYS,
    get_active_notifications, dismiss_notification, dismiss_all_notifications,
    get_categories, add_category, delete_category
)
from logic import (
    parse_due_datetime, format_due_time, set_occurrence_status,
    generate_occurrences, get_all_occurrences, get_reminders_for_occurrence,
    max_span_days, default_span_days, fire_due_reminders
)

st.set_page_config(page_title="MyPlanner", page_icon="📅", layout="wide")


def _setting(name, default):
    """Read a setting from an environment variable or from Streamlit secrets."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


# Cloud servers run on UTC. Reminders compare against the clock, so run the app on YOUR timezone.
if hasattr(_time_module, "tzset"):      # Linux (the cloud); on Windows your PC's own timezone is used
    os.environ["TZ"] = _setting("APP_TIMEZONE", "Asia/Kolkata")
    _time_module.tzset()

create_table()

# =========================================================
# CONSTANTS
# =========================================================
TODO_OPTIONS = [
    "Assignment", "Project", "Exam", "Study", "Interview", "Meeting",
    "Presentation", "Report", "Submission", "Application", "Payment",
    "Bill", "Renewal", "Appointment", "Shopping", "Personal", "Workout",
    "Travel", "Birthday", "Other"
]
DEFAULT_CATEGORIES = ["Work", "Study", "Personal", "Finance", "Health", "Travel", "Other"]
NEW_CATEGORY_LABEL = "➕ New category…"
PRIORITIES = ["Low", "Medium", "High"]
RECURRENCE_OPTIONS = ["Does not repeat", "Every day", "Every week", "Every month", "Every year"]

DEFAULT_TIME = "06:00 PM"
COOKIE_NAME = "myplanner_session"

# =========================================================
# SESSION STATE
# =========================================================
defaults = {
    "logged_in": False,
    "user_id": None,
    "user_name": "",
    "user_email": "",
    "page": "Calendar",
    "selected_date": date.today().isoformat(),
    "last_cal_state": None,
    "cal_version": 0,        # bump to remount the calendar and drop its stale click state
    "form_version": 0,       # bump to reset the inline "Add Work" widgets
    "sidebar_version": 0,    # bump to reset the sidebar to-do widgets
    "pending_edit_id": None, # task to open in the edit dialog
    "auth_token": None,      # token of the current login session (also kept in a browser cookie)
    "clear_cookie": False,   # set on logout so the browser cookie gets removed
    "pending_add": None,     # (date, time) clicked on the calendar -> opens the add dialog
    "cal_initial_date": date.today().isoformat(),
}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value


# =========================================================
# HELPERS
# =========================================================
def clamp_end_date(candidate, start, rec_type):
    """Keep a 'repeat until' value inside the allowed [start, start + max] range."""
    return max(start, min(candidate, start + timedelta(days=max_span_days(rec_type))))


def to_time(value):
    for fmt in ("%I:%M %p", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(value), fmt).time()
        except ValueError:
            pass
    return time(18, 0)


def time_picker(label, current, key):
    """Any time of day, down to the minute (e.g. 06:15 PM). Returns a string like '06:15 PM'."""
    chosen = st.time_input(label, value=to_time(current), step=60, key=key)
    return chosen.strftime("%I:%M %p")


REPEAT_UNITS = {"Every day": "days", "Every week": "weeks", "Every month": "months", "Every year": "years"}


def recurrence_picker(key, start_date, rec_type="Does not repeat", interval=1,
                      end_date=None, no_end=False):
    """
    Returns (recurrence_type, interval, end_date_iso_or_None).
    rec_type / interval / end_date / no_end describe the task's CURRENT settings (when editing);
    they are only used as defaults while the same repeat type is selected.
    """
    idx = RECURRENCE_OPTIONS.index(rec_type) if rec_type in RECURRENCE_OPTIONS else 0
    rtype = st.selectbox("Repeat", RECURRENCE_OPTIONS, index=idx, key=f"{key}_rtype")
    if rtype == "Does not repeat":
        return rtype, 1, None

    same_type = rtype == rec_type
    try:
        interval = int(interval or 1) if same_type else 1
    except (ValueError, TypeError):
        interval = 1
    n = st.number_input(f"Repeat every (number of {REPEAT_UNITS[rtype]})", min_value=1,
                        max_value=365, value=interval, step=1, key=f"{key}_interval_{rtype}")

    max_days = max_span_days(rtype)
    years = max_days // 365
    forever = st.checkbox(f"No end date (keeps repeating for up to {years} years)",
                          value=bool(no_end and same_type), key=f"{key}_noend_{rtype}")
    if forever:
        return rtype, int(n), None

    # default end depends on the repeat type (a monthly task needs more than 30 days to repeat!)
    if end_date and same_type:
        default_end = end_date
    else:
        default_end = start_date + timedelta(days=default_span_days(rtype))
    end = st.date_input(
        f"Repeat until (max {years} years)",
        value=clamp_end_date(default_end, start_date, rtype),
        min_value=start_date,
        max_value=start_date + timedelta(days=max_days),
        key=f"{key}_end_{rtype}_{start_date.isoformat()}",  # new key when type/date changes -> fresh default
    )
    return rtype, int(n), end.isoformat()


def category_options(current=None):
    """Built-in categories + this user's own + (when editing) the task's current one."""
    options = list(DEFAULT_CATEGORIES)
    lowered = {o.lower() for o in options}
    for name in get_categories(st.session_state.user_id):
        if name.lower() not in lowered:
            options.append(name)
            lowered.add(name.lower())
    if current and current.lower() not in lowered:
        options.append(current)
    return options


def category_picker(key, current=None):
    """Dropdown of categories with a 'New category' option. Returns (name, is_new)."""
    options = category_options(current)
    default = current if current in options else "Other"
    choice = st.selectbox("Category", options + [NEW_CATEGORY_LABEL],
                          index=options.index(default), key=f"{key}_cat")
    if choice == NEW_CATEGORY_LABEL:
        name = st.text_input("New category name", max_chars=30,
                             placeholder="e.g. Fitness, Family, Side project",
                             key=f"{key}_newcat")
        return " ".join(name.split()), True
    return choice, False


def finalize_category(name, is_new):
    """Saves a newly typed category (if needed) and returns the name to store on the task."""
    if not is_new:
        return name
    for existing in category_options():
        if existing.lower() == name.lower():
            return existing
    add_category(st.session_state.user_id, name)
    return name


def add_category_cb():
    name = " ".join(st.session_state.get("mgr_new_cat", "").split())
    if not name:
        st.session_state.flash = "Type a category name first."
    elif name.lower() in [o.lower() for o in category_options()]:
        st.session_state.flash = f"'{name}' already exists."
    else:
        add_category(st.session_state.user_id, name)
        st.session_state.flash = f"Category '{name}' added."
    st.session_state.mgr_new_cat = ""


def delete_category_cb(name):
    delete_category(st.session_state.user_id, name)
    st.session_state.flash = f"Category '{name}' removed. Existing tasks keep it."


def reminder_picker(key, defaults=(False, False, False, False), at_time=True, custom_minutes=0):
    """Returns (3 days, 2 days, 1 day, 1 hour, at the due time, custom minutes before)."""
    c1, c2 = st.columns(2)
    with c1:
        r3 = st.checkbox("3 days before", value=bool(defaults[0]), key=f"{key}_r3")
        r2 = st.checkbox("2 days before", value=bool(defaults[1]), key=f"{key}_r2")
    with c2:
        r1 = st.checkbox("1 day before", value=bool(defaults[2]), key=f"{key}_r1")
        rh = st.checkbox("1 hour before", value=bool(defaults[3]), key=f"{key}_rh")
    r0 = st.checkbox("At the due time", value=bool(at_time), key=f"{key}_r0")
    rc = st.number_input("Extra reminder: minutes before (0 = off)", min_value=0, max_value=10080,
                         value=int(custom_minutes or 0), step=5, key=f"{key}_rc")
    return r3, r2, r1, rh, r0, int(rc)


def html_embed(html, height=0):
    """Run a small HTML/JS snippet. Uses st.iframe on new Streamlit, components.html on older ones."""
    if hasattr(st, "iframe"):
        st.iframe(html, height=max(int(height), 1))
    else:
        components.html(html, height=height)


def cookie_component(token=None):
    """Sets (or, with token=None, removes) the login cookie in the browser."""
    if token:
        value = f"{COOKIE_NAME}={token}; max-age={SESSION_DAYS * 86400}; path=/; SameSite=Lax"
    else:
        value = f"{COOKIE_NAME}=; max-age=0; path=/; SameSite=Lax"
    html_embed(
        "<script>try{window.parent.document.cookie='" + value + "'"
        "+(window.parent.location.protocol==='https:'?'; Secure':'');}catch(e){}</script>",
        height=0,
    )


def login_user(user_id, name, email):
    st.session_state.logged_in = True
    st.session_state.user_id = user_id
    st.session_state.user_name = name
    st.session_state.user_email = email
    st.session_state.page = "Calendar"
    st.session_state.auth_token = create_session(user_id)
    st.session_state.clear_cookie = False


def browser_notify(alerts):
    """Pop up a desktop notification (needs permission, see the sidebar button)."""
    payload = json.dumps([
        {"title": a["title"], "body": a["message"], "tag": a["tag"]} for a in alerts
    ]).replace("</", "<\\/")
    html_embed(
        "<script>const items=" + payload + ";"
        "try{const N=window.parent.Notification;"
        "if(N&&N.permission==='granted'){items.forEach(i=>new N('🔔 '+i.title,{body:i.body,tag:i.tag}));}"
        "}catch(e){}</script>",
        height=0,
    )


def desktop_permission_widget():
    html_embed("""
    <button id="b" style="padding:6px 10px;border-radius:6px;border:1px solid #888;
            background:transparent;color:#888;cursor:pointer;font-size:13px;">
        Enable desktop notifications</button>
    <span id="s" style="color:#888;font-size:13px;font-family:sans-serif;"></span>
    <script>
    const N = window.parent.Notification;
    const b = document.getElementById('b'), s = document.getElementById('s');
    function refresh() {
        if (!N) { b.style.display='none'; s.textContent='Not supported in this browser.'; return; }
        if (N.permission === 'granted') { b.style.display='none'; s.textContent='✅ Desktop notifications are on'; }
        else if (N.permission === 'denied') { b.style.display='none';
            s.textContent='Blocked. Allow notifications for this site in your browser settings.'; }
    }
    b.onclick = () => { N.requestPermission().then(refresh); };
    refresh();
    </script>
    """, height=48)


def send_test_notification():
    st.session_state["_test_alert"] = True


@st.fragment(run_every=20)
def notification_center():
    """Checks for due reminders every 20 s (and on every page load), alerts, and lists the inbox."""
    user_id = st.session_state.user_id
    new_alerts = []
    try:
        new_alerts = fire_due_reminders(user_id)
    except Exception as e:   # show the problem instead of failing silently
        st.error(f"Couldn't check reminders: {e}")

    if st.session_state.pop("_test_alert", False):
        new_alerts.append({
            "title": "Test notification",
            "message": "If you can see this, notifications work 🎉",
            "tag": f"test_{datetime.now().timestamp()}",
        })

    for a in new_alerts:
        st.toast(f"**{a['title']}** — {a['message']}", icon="🔔")
    if new_alerts:
        browser_notify(new_alerts)

    notes = get_active_notifications(user_id)
    label = f"🔔 Notifications ({len(notes)})" if notes else "🔔 Notifications"
    with st.expander(label, expanded=bool(notes)):
        if not notes:
            st.caption("No new reminders.")
        for n in notes[:10]:
            c1, c2 = st.columns([5, 1])
            with c1:
                st.markdown(f"**{n['title']}**")
                st.caption(n["message"])
            with c2:
                st.button("✕", key=f"dismiss_{n['id']}", help="Dismiss",
                          on_click=dismiss_notification, args=(n["id"], user_id))
        if notes:
            st.button("Dismiss all", key="dismiss_all", use_container_width=True,
                      on_click=dismiss_all_notifications, args=(user_id,))
        st.button("Send a test notification", key="test_notif", use_container_width=True,
                  on_click=send_test_notification)
        desktop_permission_widget()
        st.caption(f"Last checked {datetime.now().strftime('%I:%M:%S %p')} · re-checks every 20 s "
                   "while this tab is open")


# =========================================================
# RESTORE LOGIN AFTER A PAGE REFRESH (from the session cookie)
# =========================================================
if not st.session_state.logged_in:
    try:
        cookie_token = st.context.cookies.get(COOKIE_NAME)
    except Exception:
        cookie_token = None
    restored = get_user_by_session(cookie_token) if cookie_token else None
    if restored:
        st.session_state.logged_in = True
        st.session_state.user_id = restored["id"]
        st.session_state.user_name = restored["name"]
        st.session_state.user_email = restored["email"]
        st.session_state.auth_token = cookie_token
        st.session_state.clear_cookie = False
        st.session_state.page = "Calendar"


# =========================================================
# LOGIN / SIGNUP PAGE
# =========================================================
if not st.session_state.logged_in:
    if st.session_state.clear_cookie:
        cookie_component(None)
    st.title("📅 MyPlanner")
    st.caption("Your personal planner for tasks, deadlines and reminders.")
    st.divider()

    login_tab, signup_tab = st.tabs(["🔐 Login", "📝 Create Account"])

    with login_tab:
        st.subheader("Welcome back")
        login_email = st.text_input("Email", placeholder="Enter your email", key="login_email")
        login_password = st.text_input("Password", type="password", placeholder="Enter your password", key="login_password")

        if st.button("Login", type="primary", use_container_width=True):
            if not login_email.strip():
                st.error("Please enter your email.")
            elif not login_password:
                st.error("Please enter your password.")
            else:
                user = authenticate_user(login_email, login_password)
                if user:
                    login_user(user["id"], user["name"], user["email"])
                    st.rerun()
                else:
                    st.error("Invalid email or password.")

    with signup_tab:
        st.subheader("Create your account")
        signup_name = st.text_input("Full Name", placeholder="Enter your name", key="signup_name")
        signup_email = st.text_input("Email", placeholder="Enter your email", key="signup_email")
        signup_password = st.text_input("Password", type="password", placeholder="Create a password", key="signup_password")
        signup_confirm = st.text_input("Confirm Password", type="password", placeholder="Re-enter your password", key="signup_confirm")

        if st.button("Create Account", type="primary", use_container_width=True):
            if not signup_name.strip():
                st.error("Please enter your name.")
            elif not signup_email.strip():
                st.error("Please enter your email.")
            elif "@" not in signup_email:
                st.error("Please enter a valid email address.")
            elif len(signup_password) < 6:
                st.error("Password must contain at least 6 characters.")
            elif signup_password != signup_confirm:
                st.error("Passwords do not match.")
            else:
                success, user_id, message = create_user(signup_name, signup_email, signup_password)
                if success:
                    login_user(user_id, signup_name.strip(), signup_email.strip().lower())
                    st.rerun()
                else:
                    st.error(message)
    st.stop()

# Show a one-time toast set before the last rerun
if "flash" in st.session_state:
    st.toast(st.session_state.pop("flash"))


# =========================================================
# SIDEBAR
# =========================================================
with st.sidebar:
    st.markdown("# 📅 MyPlanner")
    st.caption(f"Welcome, {st.session_state.user_name}")
    st.divider()

    if st.button("📅 Calendar", use_container_width=True):
        st.session_state.page = "Calendar"
        st.rerun()
    if st.button("🔔 Upcoming & Reminders", use_container_width=True):
        st.session_state.page = "Upcoming & Reminders"
        st.rerun()

    st.divider()
    notification_center()

    st.divider()
    st.markdown("### ✅ To-Do List")
    st.caption("Add a task directly without selecting the calendar.")

    sv = st.session_state.sidebar_version
    selected_todo = st.selectbox("Task Type", TODO_OPTIONS, key=f"todo_type_{sv}")
    custom_task = st.text_input("Custom Task", placeholder="Type your own task", key=f"todo_custom_{sv}")
    todo_due_date = st.date_input("Due Date", value=date.today(), key=f"todo_due_{sv}")
    final_time_sidebar = time_picker("Due Time", DEFAULT_TIME, f"todo_time_{sv}")
    todo_cat, todo_cat_new = category_picker(f"todo_{sv}")

    st.markdown("**🔁 Repeat**")
    todo_recurrence, todo_interval, todo_end_iso = recurrence_picker(f"todo_rec_{sv}", todo_due_date)

    st.markdown("**🔔 Reminders**")
    t3, t2, t1, th, t0, tc = reminder_picker(f"todo_rem_{sv}")

    if st.button("➕ Add Task", type="primary", use_container_width=True):
        if todo_cat_new and not todo_cat:
            st.error("Enter a name for the new category.")
        else:
            todo_title = custom_task.strip() if custom_task.strip() else selected_todo
            add_work(
                user_id=st.session_state.user_id, title=todo_title, description="",
                category=finalize_category(todo_cat, todo_cat_new), priority="Medium",
                due_date=todo_due_date.isoformat(),
                due_time=final_time_sidebar, reminder_3_days=t3, reminder_2_days=t2,
                reminder_1_day=t1, reminder_1_hour=th, recurrence_type=todo_recurrence,
                recurrence_interval=todo_interval, recurrence_days="",
                recurrence_end_date=todo_end_iso,
                reminder_at_time=t0, reminder_custom_minutes=tc
            )
            st.session_state.flash = "Task added!"
            st.session_state.sidebar_version += 1   # clears the sidebar inputs
            st.session_state.cal_version += 1       # refreshes the calendar
            st.session_state.last_cal_state = None
            st.session_state.page = "Calendar"
            st.rerun()

    st.divider()
    with st.expander("🏷️ My categories"):
        my_categories = get_categories(st.session_state.user_id)
        if not my_categories:
            st.caption("No custom categories yet. Add one below, or pick “New category…” when creating a task.")
        for cat_name in my_categories:
            c1, c2 = st.columns([4, 1])
            with c1:
                st.write(cat_name)
            with c2:
                st.button("✕", key=f"delcat_{cat_name}", help="Remove category",
                          on_click=delete_category_cb, args=(cat_name,))
        st.text_input("New category", key="mgr_new_cat", max_chars=30, placeholder="e.g. Fitness")
        st.button("Add category", key="mgr_add_cat", use_container_width=True, on_click=add_category_cb)

    st.divider()
    st.markdown("### 👤 Account")
    st.caption(st.session_state.user_email)
    if st.button("🚪 Logout", use_container_width=True):
        delete_session(st.session_state.auth_token)
        for key in list(st.session_state.keys()):
            del st.session_state[key]   # defaults are restored at the top of the next run
        st.session_state.clear_cookie = True
        st.rerun()

    if st.session_state.auth_token:
        cookie_component(st.session_state.auth_token)


# =========================================================
# EDIT DIALOG
# (a dialog reruns on its own, so conditional widgets work without st.form)
# =========================================================
@st.dialog("Edit Task")
def edit_task_dialog(task_id):
    user_id = st.session_state.user_id
    work = get_work_by_task_id(task_id, user_id)
    if not work:
        st.error("Task not found.")
        return

    k = f"edit_{task_id}"

    title = st.text_input("Task Title", value=work["title"], key=f"{k}_title")
    description = st.text_area("Description", value=work["description"] or "", key=f"{k}_desc")

    try:
        current_due = datetime.strptime(work["due_date"][:10], "%Y-%m-%d").date()
    except ValueError:
        current_due = date.today()
    due_date = st.date_input("Due Date", value=current_due, key=f"{k}_due")
    final_time = time_picker("Due Time", format_due_time(work["due_time"]), f"{k}_time")

    category_name, category_is_new = category_picker(k, work["category"])
    priority = st.selectbox(
        "Priority", PRIORITIES,
        index=PRIORITIES.index(work["priority"]) if work["priority"] in PRIORITIES else 1,
        key=f"{k}_pri",
    )

    st.markdown("**🔁 Repeat**")
    existing_end = None
    if work["recurrence_end_date"]:
        try:
            existing_end = datetime.strptime(work["recurrence_end_date"], "%Y-%m-%d").date()
        except ValueError:
            existing_end = None
    rec_type, rec_interval, rec_end_iso = recurrence_picker(
        k, due_date, work["recurrence_type"] or "Does not repeat",
        work["recurrence_interval"], existing_end,
        no_end=(work["recurrence_type"] not in (None, "Does not repeat") and not work["recurrence_end_date"]),
    )
    st.caption("Changing the date or repeat pattern clears completion marks for this task's occurrences.")
    if (work["recurrence_type"] or "Does not repeat") != "Does not repeat":
        st.info("This is a repeating task: changes apply to every occurrence in the series.")

    st.markdown("**🔔 Reminders**")
    r3, r2, r1, rh, r0, rc = reminder_picker(
        k,
        (work["reminder_3_days"], work["reminder_2_days"],
         work["reminder_1_day"], work["reminder_1_hour"]),
        at_time=work["reminder_at_time"],
        custom_minutes=work["reminder_custom_minutes"],
    )

    is_recurring = (work["recurrence_type"] or "Does not repeat") != "Does not repeat"
    colA, colB = st.columns(2)
    with colA:
        if st.button("Update Task", type="primary", use_container_width=True, key=f"{k}_update"):
            if not title.strip():
                st.error("Task title cannot be empty.")
            elif category_is_new and not category_name:
                st.error("Enter a name for the new category.")
            else:
                update_work(
                    task_id=task_id, user_id=user_id, title=title.strip(), description=description,
                    due_date=due_date.isoformat(), due_time=final_time, category=finalize_category(category_name, category_is_new),
                    priority=priority, reminder_3_days=r3, reminder_2_days=r2,
                    reminder_1_day=r1, reminder_1_hour=rh, recurrence_type=rec_type,
                    recurrence_interval=rec_interval, recurrence_days=work["recurrence_days"] or "",
                    recurrence_end_date=rec_end_iso,
                    reminder_at_time=r0, reminder_custom_minutes=rc,
                )
                st.session_state.flash = "Task updated!"
                st.session_state.cal_version += 1
                st.session_state.last_cal_state = None
                st.rerun()
    with colB:
        if st.button("Delete Series" if is_recurring else "Delete Task",
                     use_container_width=True, key=f"{k}_delete"):
            delete_work(task_id, user_id)
            st.session_state.flash = "Task deleted."
            st.session_state.cal_version += 1
            st.session_state.last_cal_state = None
            st.rerun()


# =========================================================
# ADD DIALOG (opens when a date is clicked on the calendar)
# =========================================================
@st.dialog("Add Task")
def add_task_dialog(date_str, time_str):
    user_id = st.session_state.user_id
    clicked = datetime.strptime(date_str, "%Y-%m-%d").date()
    k = f"add_{date_str}_{st.session_state.form_version}"

    st.caption(f"📅 {clicked.strftime('%A, %d %B %Y')}")
    title = st.text_input("Task Title", placeholder="Enter task or event name", key=f"{k}_title")
    description = st.text_area("Description", placeholder="Add details (optional)", key=f"{k}_desc")

    due_date = st.date_input("Due Date", value=clicked, key=f"{k}_due")
    due_time = time_picker("Due Time (any time, e.g. 06:15 PM)", time_str, f"{k}_time")

    category_name, category_is_new = category_picker(k)
    priority = st.selectbox("Priority", PRIORITIES, index=1, key=f"{k}_pri")

    st.markdown("**🔁 Repeat**")
    rec_type, rec_interval, rec_end_iso = recurrence_picker(k, due_date)

    st.markdown("**🔔 Reminders**")
    r3, r2, r1, rh, r0, rc = reminder_picker(k)

    if st.button("➕ Add Task", type="primary", use_container_width=True, key=f"{k}_submit"):
        if not title.strip():
            st.error("Please enter a task title.")
        elif category_is_new and not category_name:
            st.error("Enter a name for the new category.")
        else:
            add_work(
                user_id=user_id, title=title.strip(), description=description,
                due_date=due_date.isoformat(), due_time=due_time, category=finalize_category(category_name, category_is_new),
                priority=priority, reminder_3_days=r3, reminder_2_days=r2,
                reminder_1_day=r1, reminder_1_hour=rh, recurrence_type=rec_type,
                recurrence_interval=rec_interval, recurrence_days="",
                recurrence_end_date=rec_end_iso,
                reminder_at_time=r0, reminder_custom_minutes=rc,
            )
            st.session_state.flash = "Task added!"
            st.session_state.form_version += 1
            st.session_state.cal_version += 1
            st.session_state.last_cal_state = None
            st.session_state.cal_initial_date = due_date.isoformat()
            st.rerun()


# =========================================================
# CALENDAR PAGE
# =========================================================
if st.session_state.page == "Calendar":
    st.title("📅 Calendar")
    st.caption("Click any date to add a task. Click an existing event to edit it.")

    all_occurrences = get_all_occurrences(st.session_state.user_id)
    calendar_events = []

    for occurrence in all_occurrences:
        display_title = ("✓ " if occurrence["completed"] else "") + occurrence["title"]
        due_dt = parse_due_datetime(occurrence["date"].isoformat(), occurrence["due_time"])
        event = {
            "id": f"{occurrence['task_id']}_{occurrence['date'].isoformat()}",
            "title": display_title,
            # real start time -> proper slots in the week / day views
            "start": due_dt.isoformat() if due_dt else occurrence["date"].isoformat(),
            "allDay": due_dt is None,
        }
        if occurrence["completed"]:
            event["backgroundColor"] = "#9e9e9e"
            event["borderColor"] = "#9e9e9e"
        calendar_events.append(event)

    calendar_options = {
        "initialView": "dayGridMonth",
        "initialDate": st.session_state.cal_initial_date,
        "selectable": True, "height": 560,
        "contentHeight": 520, "aspectRatio": 1.8, "timeZone": "local",
        "headerToolbar": {
            "left": "prev,next today", "center": "title",
            "right": "dayGridMonth,timeGridWeek,timeGridDay"
        }
    }

    calendar_data = calendar(
        events=calendar_events,
        options=calendar_options,
        callbacks=["dateClick", "eventClick"],
        key=f"my_calendar_{st.session_state.cal_version}",
    )

    if calendar_data and calendar_data != st.session_state.last_cal_state:
        st.session_state.last_cal_state = calendar_data

        if calendar_data.get("dateClick"):
            click_data = calendar_data["dateClick"]
            raw = click_data.get("dateStr") or click_data.get("date") or ""
            date_str = raw[:10]  # first 10 chars only, to avoid timezone shifts
            if date_str:
                # Week/day views include the clicked time ("2026-10-11T10:30:00+05:30")
                time_str = DEFAULT_TIME
                if len(raw) >= 16 and raw[10] == "T":
                    time_str = to_time(raw[11:16]).strftime("%I:%M %p")
                st.session_state.selected_date = date_str
                st.session_state.cal_initial_date = date_str
                st.session_state.pending_add = (date_str, time_str)
                # remount the calendar so the same date can be clicked again later
                st.session_state.cal_version += 1
                st.session_state.last_cal_state = None
                st.rerun()

        elif calendar_data.get("eventClick"):
            event_data = calendar_data["eventClick"].get("event", {})
            event_id_str = event_data.get("id", "")
            task_part, _, date_part = event_id_str.partition("_")
            try:
                clicked_task_id = int(task_part)
            except ValueError:
                clicked_task_id = None
            if clicked_task_id is not None:
                st.session_state.pending_edit_id = clicked_task_id
                if date_part:
                    st.session_state.cal_initial_date = date_part[:10]
                # remount the calendar so the same event can be clicked again later
                st.session_state.cal_version += 1
                st.session_state.last_cal_state = None
                st.rerun()

    if st.session_state.pending_add is not None:
        add_date, add_time = st.session_state.pending_add
        st.session_state.pending_add = None
        add_task_dialog(add_date, add_time)
    elif st.session_state.pending_edit_id is not None:
        pending_id = st.session_state.pending_edit_id
        st.session_state.pending_edit_id = None
        edit_task_dialog(pending_id)

    # =====================================================
    # TASK LIST
    # =====================================================
    st.divider()
    st.subheader("📋 Your Tasks")
    works = get_all_works(st.session_state.user_id)

    if not works:
        st.info("No tasks yet. Create one above.")
    else:
        for work in works:
            (task_id, title, description, due_date, due_time, category, priority,
             reminder_3_days, reminder_2_days, reminder_1_day, reminder_1_hour,
             completed, recurrence_type, recurrence_interval, recurrence_days,
             recurrence_end_date) = work[:16]

            due_time_display = format_due_time(due_time)

            if recurrence_type == "Does not repeat":
                with st.container(border=True):
                    col1, col2, col3, col4 = st.columns([5, 2, 1, 1])
                    with col1:
                        if completed:
                            st.markdown(f"### ~~{title}~~")
                        else:
                            st.markdown(f"### {title}")
                        st.caption(f"📅 {due_date}   ⏰ {due_time_display}")
                        if category:
                            st.caption(f"Category: {category} | Priority: {priority}")
                    with col2:
                        completed_value = st.checkbox("Completed", value=bool(completed), key=f"completed_{task_id}")
                        if completed_value != bool(completed):
                            mark_completed(task_id, st.session_state.user_id, completed_value)
                            st.session_state.cal_version += 1
                            st.session_state.last_cal_state = None
                            st.rerun()
                    with col3:
                        if st.button("✏️", key=f"list_edit_{task_id}", help="Edit task"):
                            edit_task_dialog(task_id)
                    with col4:
                        if st.button("🗑️", key=f"delete_{task_id}", help="Delete task"):
                            delete_work(task_id, st.session_state.user_id)
                            st.session_state.cal_version += 1
                            st.session_state.last_cal_state = None
                            st.rerun()
            else:
                head1, head2, head3 = st.columns([5, 2, 2])
                with head1:
                    st.markdown(f"### 🔁 {title}")
                    st.caption(f"{recurrence_type} · until {recurrence_end_date or 'no end date'}"
                               + (f" · {category}" if category else ""))
                with head2:
                    if st.button("✏️ Edit series", key=f"list_edit_series_{task_id}",
                                 help="Edit this task and all of its repeats"):
                        edit_task_dialog(task_id)
                with head3:
                    if st.button("🗑️ Delete series", key=f"delete_series_{task_id}",
                                 help="Deletes this task and ALL of its repeats"):
                        delete_work(task_id, st.session_state.user_id)
                        st.session_state.cal_version += 1
                        st.session_state.last_cal_state = None
                        st.rerun()

                occurrences = generate_occurrences(work, st.session_state.user_id)
                upcoming = [occ for occ in occurrences if occ["date"] >= date.today()]
                if not upcoming:
                    st.caption("No upcoming repeats. The repeat period has ended; use Edit series to extend it.")
                else:
                    st.caption(f"Showing the next {min(10, len(upcoming))} of {len(upcoming)} upcoming repeats")

                for occurrence in upcoming[:10]:
                    occurrence_key = f"{task_id}_{occurrence['date'].isoformat()}"
                    with st.container(border=True):
                        col1, col2, col3 = st.columns([5, 2, 1])
                        with col1:
                            if occurrence["completed"]:
                                st.markdown(f"~~{occurrence['title']}~~")
                            else:
                                st.markdown(f"**{occurrence['title']}**")
                            st.caption(f"📅 {occurrence['date'].strftime('%d %B %Y')}   ⏰ {occurrence['due_time']}")
                        with col2:
                            occurrence_completed = st.checkbox(
                                "Completed", value=occurrence["completed"],
                                key=f"occurrence_{occurrence_key}",
                            )
                            if occurrence_completed != occurrence["completed"]:
                                set_occurrence_status(
                                    task_id, st.session_state.user_id,
                                    occurrence["date"], recurrence_type, occurrence_completed,
                                )
                                st.session_state.cal_version += 1
                                st.session_state.last_cal_state = None
                                st.rerun()
                        with col3:
                            if st.button("✏️", key=f"list_edit_{occurrence_key}",
                                         help="Edit this task (the whole series)"):
                                edit_task_dialog(task_id)

# =========================================================
# UPCOMING & REMINDERS
# =========================================================
elif st.session_state.page == "Upcoming & Reminders":
    st.title("🔔 Upcoming & Reminders")
    st.caption("See your upcoming deadlines and scheduled reminders.")

    all_occurrences = get_all_occurrences(st.session_state.user_id)
    now = datetime.now()

    st.subheader("📌 Upcoming Tasks")
    upcoming_tasks = []
    for occurrence in all_occurrences:
        due_datetime = parse_due_datetime(occurrence["date"].isoformat(), occurrence["due_time"])
        if due_datetime and due_datetime >= now and not occurrence["completed"]:
            upcoming_tasks.append((due_datetime, occurrence))

    upcoming_tasks.sort(key=lambda x: x[0])

    if not upcoming_tasks:
        st.info("No upcoming tasks.")
    else:
        for due_datetime, occurrence in upcoming_tasks[:20]:
            days_left = (due_datetime.date() - date.today()).days
            with st.container(border=True):
                col1, col2 = st.columns([5, 2])
                with col1:
                    st.markdown(f"### {occurrence['title']}")
                    st.caption(f"📅 {due_datetime.strftime('%d %B %Y')}   ⏰ {due_datetime.strftime('%I:%M %p')}")
                    if occurrence["category"]:
                        st.caption(f"{occurrence['category']} • {occurrence['priority']} priority")
                with col2:
                    if days_left == 0:
                        st.warning("Due today")
                    elif days_left == 1:
                        st.warning("Due tomorrow")
                    else:
                        st.info(f"{days_left} days left")

    st.divider()
    st.subheader("🔔 Scheduled Reminders")
    scheduled_reminders = []
    for occurrence in all_occurrences:
        if occurrence["completed"]:
            continue
        for reminder_datetime, reminder_label in get_reminders_for_occurrence(occurrence):
            scheduled_reminders.append((reminder_datetime, reminder_label, occurrence))

    scheduled_reminders.sort(key=lambda x: x[0])
    future_reminders = [r for r in scheduled_reminders if r[0] >= now]

    if not future_reminders:
        st.info("No reminders scheduled.")
    else:
        for reminder_datetime, reminder_label, occurrence in future_reminders[:30]:
            with st.container(border=True):
                st.markdown(f"🔔 **{occurrence['title']}**")
                st.caption(f"{reminder_label} • {reminder_datetime.strftime('%d %B %Y, %I:%M %p')}")

    st.divider()
    st.subheader("⚠️ Overdue")
    overdue_tasks = []
    for occurrence in all_occurrences:
        due_datetime = parse_due_datetime(occurrence["date"].isoformat(), occurrence["due_time"])
        if due_datetime and due_datetime < now and not occurrence["completed"]:
            overdue_tasks.append((due_datetime, occurrence))

    overdue_tasks.sort(key=lambda x: x[0], reverse=True)

    if not overdue_tasks:
        st.success("No overdue tasks. 🎉")
    else:
        for due_datetime, occurrence in overdue_tasks[:20]:
            with st.container(border=True):
                st.error(f"**{occurrence['title']}**")
                st.caption(f"Due: {due_datetime.strftime('%d %B %Y, %I:%M %p')}")