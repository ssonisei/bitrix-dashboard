#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ежедневное обновление данных для дашборда Bitrix24.

Первый запуск: полностью выгружает задачи с PERIOD_START по сегодня.
Все следующие запуски: забирают только задачи, изменённые за последние
LOOKBACK_DAYS дней, и сливают их в сохранённый архив (data/tasks_archive.json).
Статистика на каждом запуске пересчитывается заново по ВСЕМУ архиву — это
дёшево (без обращений к Bitrix) и учитывает изменения статусов старых задач.

Итог сохраняется в docs/data.json — эту точку дашборд (docs/index.html)
подгружает через fetch().

Работает и локально, и в GitHub Actions. Вебхуки берутся из переменных
окружения (для GitHub Actions — из Secrets), с запасным вариантом
для локального теста.

Запуск:
    pip install requests --break-system-packages
    python daily_update.py
"""

import os
import re
import html
import json
import time
import datetime as dt
from collections import defaultdict

import requests

# ============================== CONFIG ==============================

TASKS_WEBHOOK = os.environ.get("TASKS_WEBHOOK")
USERS_WEBHOOK = os.environ.get("USERS_WEBHOOK")
DEPARTMENTS_WEBHOOK = os.environ.get("DEPARTMENTS_WEBHOOK")

if not (TASKS_WEBHOOK and USERS_WEBHOOK and DEPARTMENTS_WEBHOOK):
    raise SystemExit(
        "Не заданы вебхуки. Установи переменные окружения (или GitHub Secrets) "
        "TASKS_WEBHOOK, USERS_WEBHOOK, DEPARTMENTS_WEBHOOK."
    )

_excluded_env = os.environ.get("EXCLUDED_USER_IDS", "")
BASE_EXCLUDED_IDS = {x.strip() for x in _excluded_env.split(",") if x.strip()} or {
    "547722", "652618", "24724", "11428", "178192", "113710", "104332",
}

# ID, которые исключаются ТОЛЬКО как исполнитель (RESPONSIBLE_ID), но остаются
# видимыми как постановщик — например Тультаев Алмаз.
_excluded_resp_only_env = os.environ.get("EXCLUDED_RESPONSIBLE_ONLY_IDS", "")
EXCLUDED_RESPONSIBLE_ONLY_IDS = {
    x.strip() for x in _excluded_resp_only_env.split(",") if x.strip()
} or {"54"}

EXCLUDED_RESPONSIBLE_IDS = BASE_EXCLUDED_IDS | EXCLUDED_RESPONSIBLE_ONLY_IDS
EXCLUDED_CREATOR_IDS = BASE_EXCLUDED_IDS

# Задачи, у которых в названии/описании встречается любая из этих фраз
# (без учёта регистра), полностью исключаются из дашборда — это
# автоматические CRM-напоминания, а не реальная рабочая задача.
_excluded_titles_env = os.environ.get("EXCLUDED_TITLE_SUBSTRINGS", "")
EXCLUDED_TITLE_SUBSTRINGS = [
    x.strip().lower() for x in _excluded_titles_env.split(",") if x.strip()
] or ["связаться с клиентом"]

# Отделы, которые полностью исключаются из дашборда (например, отдел разработки
# и парки/локации — их задачи не относятся к операционной аналитике по сотрудникам).
_excluded_depts_env = os.environ.get("EXCLUDED_DEPARTMENTS", "")
EXCLUDED_DEPARTMENTS = {
    x.strip() for x in _excluded_depts_env.split(",") if x.strip()
} or {
    "Разработчики",
    'Алматы "Ice World"',
    'Алматы "Magic Forest"',
    'Алматы "Rock World"',
    'Алматы "Water World"',
    'Караганда "Rock World"',
    'Ташкент "Rock World"',
    'Тараз "Ice World"',
    'Шымкент "Ice World"',
}

PERIOD_START_STR = os.environ.get("PERIOD_START", "2026-07-01")
OUTPUT_JSON = os.environ.get("OUTPUT_JSON", "docs/data.json")
ARCHIVE_PATH = os.environ.get("ARCHIVE_PATH", "data/tasks_archive.json")
# Сколько дней "назад" перезабирать при инкрементальном обновлении — с запасом,
# на случай пропущенного дня или изменений задним числом.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "3"))

REQUEST_DELAY = 5.0
_last_request_time = [0.0]
SESSION = requests.Session()

# ============================== HELPERS (same as bitrix_report.py) ==============================


def parse_bx_date(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s).replace(tzinfo=None)
    except Exception:
        return None


def fio(user):
    if not user:
        return ""
    parts = [user.get("LAST_NAME") or "", user.get("NAME") or "", user.get("SECOND_NAME") or ""]
    return " ".join(p.strip() for p in parts if p and p.strip())


def is_active(user):
    if not user:
        return False
    return user.get("ACTIVE") in (True, "Y", "1", 1)


def is_employee_type(user):
    if not user:
        return False
    t = user.get("USER_TYPE")
    return True if t is None else t == "employee"


def rest_call(webhook, method, params=None, max_retries=15):
    url = webhook.rstrip("/") + "/" + method + ".json"
    for attempt in range(max_retries):
        elapsed = time.monotonic() - _last_request_time[0]
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        _last_request_time[0] = time.monotonic()
        try:
            resp = SESSION.post(url, json=params or {}, timeout=90)
        except requests.exceptions.RequestException as e:
            wait = min(5 * (attempt + 1), 90)
            print(f"  [сеть] {type(e).__name__}, жду {wait} сек ({method})...")
            time.sleep(wait)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = min(5 * (attempt + 1), 90)
            print(f"  [{resp.status_code}] жду {wait} сек ({method})...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            if data.get("error") == "QUERY_LIMIT_EXCEEDED":
                wait = min(5 * (attempt + 1), 90)
                print(f"  [лимит] жду {wait} сек ({method})...")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Bitrix error on {method}: {data}")
        return data
    raise RuntimeError(f"Bitrix REST call to {method} failed after {max_retries} retries.")


def fetch_all_list(webhook, method, base_params=None):
    base_params = dict(base_params or {})
    all_items = []
    start = 0
    while True:
        params = dict(base_params)
        params["start"] = start
        data = rest_call(webhook, method, params)
        result = data.get("result", [])
        if isinstance(result, dict):
            result = result.get("tasks", result.get("task", []))
        all_items.extend(result)
        nxt = data.get("next")
        if nxt is None:
            break
        start = nxt
    return all_items


CAMEL_TO_UPPER_MAP = {
    "id": "ID", "title": "TITLE", "description": "DESCRIPTION",
    "responsibleId": "RESPONSIBLE_ID", "createdBy": "CREATED_BY",
    "groupId": "GROUP_ID", "parentId": "PARENT_ID", "priority": "PRIORITY",
    "status": "STATUS", "deadline": "DEADLINE",
    "startDatePlan": "START_DATE_PLAN", "endDatePlan": "END_DATE_PLAN",
    "createdDate": "CREATED_DATE", "changedDate": "CHANGED_DATE",
    "changedBy": "CHANGED_BY", "closedDate": "CLOSED_DATE",
    "closedBy": "CLOSED_BY", "activityDate": "ACTIVITY_DATE",
}


def normalize_task(t):
    out = dict(t)
    for camel, upper in CAMEL_TO_UPPER_MAP.items():
        if camel in t and upper not in out:
            out[upper] = t[camel]
    return out


# ============================== FETCH ==============================

now = dt.datetime.now()
period_end = now
period_start = dt.datetime.strptime(PERIOD_START_STR, "%Y-%m-%d")

# Архив уже скачанных (нормализованных, ДО фильтрации по сотрудникам/названию)
# задач — ключ: ID задачи. Фильтры (уволенные, исключённые названия и т.д.)
# применяются заново на КАЖДОМ запуске поверх всего архива, чтобы правильно
# учитывать смены статуса/дедлайна и изменения в списке сотрудников.
archive = {}
if os.path.exists(ARCHIVE_PATH):
    try:
        with open(ARCHIVE_PATH, "r", encoding="utf-8") as f:
            archive = json.load(f)
        print(f"Архив найден: {len(archive)} задач уже сохранено ранее.")
    except Exception as e:
        print(f"Не удалось прочитать архив ({e}), начинаю с нуля.")
        archive = {}

is_first_run = not archive

print("Загружаю сотрудников...")
raw_users = fetch_all_list(USERS_WEBHOOK, "user.get")
print(f"  получено: {len(raw_users)}")

print("Загружаю отделы...")
try:
    raw_departments = fetch_all_list(DEPARTMENTS_WEBHOOK, "department.get")
except Exception as e:
    print(f"  department.get недоступен ({e}); отделы будут пустыми.")
    raw_departments = []
print(f"  получено: {len(raw_departments)}")

if is_first_run:
    print(f"Первый запуск: полная выгрузка задач с {period_start.date()} по {period_end.date()}...")
    task_filter = {
        ">=DEADLINE": period_start.strftime("%Y-%m-%dT00:00:00"),
        "<=DEADLINE": period_end.strftime("%Y-%m-%dT23:59:59"),
    }
else:
    fetch_since = now - dt.timedelta(days=LOOKBACK_DAYS)
    print(f"Инкрементальное обновление: задачи, изменённые с {fetch_since.date()}...")
    task_filter = {
        ">=CHANGED_DATE": fetch_since.strftime("%Y-%m-%dT00:00:00"),
    }

if EXCLUDED_RESPONSIBLE_IDS:
    task_filter["!RESPONSIBLE_ID"] = sorted(EXCLUDED_RESPONSIBLE_IDS)

fetched_tasks = fetch_all_list(
    TASKS_WEBHOOK, "tasks.task.list",
    base_params={"filter": task_filter, "order": {"ID": "asc"}},
)
fetched_tasks = [normalize_task(t) for t in fetched_tasks]
print(f"  получено за этот запуск (по крайнему сроку): {len(fetched_tasks)}")

if is_first_run:
    # Задачи без дедлайна не попадают в фильтр по DEADLINE вообще — отдельно
    # добираем их по дате СОЗДАНИЯ и оставляем только те, где дедлайна и правда нет.
    print("Дополнительно ищу задачи без дедлайна (по дате создания)...")
    no_deadline_filter = {
        ">=CREATED_DATE": period_start.strftime("%Y-%m-%dT00:00:00"),
        "<=CREATED_DATE": period_end.strftime("%Y-%m-%dT23:59:59"),
    }
    if EXCLUDED_RESPONSIBLE_IDS:
        no_deadline_filter["!RESPONSIBLE_ID"] = sorted(EXCLUDED_RESPONSIBLE_IDS)
    by_created = fetch_all_list(
        TASKS_WEBHOOK, "tasks.task.list",
        base_params={"filter": no_deadline_filter, "order": {"ID": "asc"}},
    )
    by_created = [normalize_task(t) for t in by_created]
    already_ids = {str(t.get("ID")) for t in fetched_tasks}
    added_no_deadline = 0
    for t in by_created:
        if str(t.get("ID")) in already_ids:
            continue
        if not t.get("DEADLINE"):
            fetched_tasks.append(t)
            added_no_deadline += 1
    print(f"  добавлено задач без дедлайна: {added_no_deadline}")

print(f"  всего получено за этот запуск: {len(fetched_tasks)}")

# Сливаем свежескачанное в архив (перезаписываем по ID — новые данные всегда точнее).
for t in fetched_tasks:
    tid = str(t.get("ID"))
    if tid:
        archive[tid] = t

with open(ARCHIVE_PATH, "w", encoding="utf-8") as f:
    os.makedirs(os.path.dirname(ARCHIVE_PATH) or ".", exist_ok=True)
    json.dump(archive, f, ensure_ascii=False)
print(f"Архив обновлён: всего {len(archive)} задач.")

raw_tasks = list(archive.values())
print(f"Всего задач для пересчёта статистики: {len(raw_tasks)}")

# ============================== INDEX ==============================

users_by_id = {str(u["ID"]): u for u in raw_users}
departments_by_id = {str(d["ID"]): d.get("NAME", "") for d in raw_departments}

STATUS_NAMES = {
    "1": "Ждёт выполнения", "2": "В работе", "3": "Ждёт контроля",
    "4": "На контроле", "5": "Завершена", "6": "Отложена", "7": "Отклонена",
    "-1": "Просрочена",
}


def resolve_department(user):
    depts = user.get("UF_DEPARTMENT") or []
    if not depts:
        return ""
    return departments_by_id.get(str(depts[0]), "")


def user_in_excluded_department(user):
    """True if the user belongs to ANY excluded department (not just their
    primary one) — so employees of excluded departments are fully skipped."""
    depts = user.get("UF_DEPARTMENT") or []
    for d in depts:
        name = departments_by_id.get(str(d), "")
        if name in EXCLUDED_DEPARTMENTS:
            return True
    return False


# ============================== FILTER + AGGREGATE ==============================

quality = {
    "total_input_tasks": len(raw_tasks),
    "added_tasks": 0,
    "excluded_unknown_responsible": 0,
    "excluded_inactive_responsible": 0,
    "excluded_by_excluded_list": 0,
    "excluded_by_title": 0,
    "excluded_by_department": 0,
}

emp_stats = defaultdict(lambda: {
    "dept": "", "fio": "", "position": "",
    "total": 0, "done": 0, "in_progress": 0, "overdue_now": 0,
    "closed_overdue": 0, "no_deadline": 0, "overdue_days": [],
})
dept_stats = defaultdict(lambda: {
    "employees": set(), "total": 0, "done": 0, "in_progress": 0,
    "overdue_now": 0, "closed_overdue": 0,
})
daily_counts = defaultdict(lambda: {"created": 0, "completed": 0})
top_overdue = []
tasks_out = []

STATUS_DONE = {"5"}
STATUS_IN_PROGRESS = {"2", "3"}

valid_tasks = 0
for t in raw_tasks:
    resp_id = str(t.get("RESPONSIBLE_ID") or "")
    resp_user = users_by_id.get(resp_id)

    if not resp_user:
        quality["excluded_unknown_responsible"] += 1
        continue
    if not (is_active(resp_user) and is_employee_type(resp_user)):
        quality["excluded_inactive_responsible"] += 1
        continue
    if resp_id in EXCLUDED_RESPONSIBLE_IDS:
        quality["excluded_by_excluded_list"] += 1
        continue

    creator_id = str(t.get("CREATED_BY") or "")
    if creator_id in EXCLUDED_CREATOR_IDS:
        quality["excluded_by_excluded_list"] += 1
        continue

    text_lower = ((t.get("TITLE") or "") + " " + (t.get("DESCRIPTION") or "")).lower()
    if any(sub in text_lower for sub in EXCLUDED_TITLE_SUBSTRINGS):
        quality["excluded_by_title"] += 1
        continue

    if user_in_excluded_department(resp_user):
        quality["excluded_by_department"] += 1
        continue

    dept_name = resolve_department(resp_user)

    valid_tasks += 1
    employee_fio = fio(resp_user)
    position = resp_user.get("WORK_POSITION", "") or ""

    deadline = parse_bx_date(t.get("DEADLINE"))
    created = parse_bx_date(t.get("CREATED_DATE"))
    closed = parse_bx_date(t.get("CLOSED_DATE"))
    status = str(t.get("STATUS") or "")
    is_done = status in STATUS_DONE
    has_deadline = deadline is not None

    overdue_now = (not is_done) and has_deadline and deadline < now
    closed_overdue = False
    overdue_days = 0
    if is_done and closed and has_deadline:
        closed_overdue = closed > deadline
        if closed_overdue:
            overdue_days = (closed - deadline).days
    elif overdue_now:
        overdue_days = (now - deadline).days

    if created:
        daily_counts[created.date().isoformat()]["created"] += 1
    if is_done and closed:
        daily_counts[closed.date().isoformat()]["completed"] += 1

    key = resp_id
    es = emp_stats[key]
    es["dept"], es["fio"], es["position"] = dept_name, employee_fio, position
    es["total"] += 1
    if is_done:
        es["done"] += 1
    if status in STATUS_IN_PROGRESS:
        es["in_progress"] += 1
    if overdue_now:
        es["overdue_now"] += 1
    if closed_overdue:
        es["closed_overdue"] += 1
        es["overdue_days"].append(overdue_days)
    if not has_deadline:
        es["no_deadline"] += 1

    if dept_name:
        ds = dept_stats[dept_name]
        ds["employees"].add(resp_id)
        ds["total"] += 1
        if is_done:
            ds["done"] += 1
        if status in STATUS_IN_PROGRESS:
            ds["in_progress"] += 1
        if overdue_now:
            ds["overdue_now"] += 1
        if closed_overdue:
            ds["closed_overdue"] += 1

    task_url = f"{TASKS_WEBHOOK.split('/rest/')[0]}/company/personal/user/{resp_id}/tasks/task/view/{t.get('ID')}/"

    if overdue_now and overdue_days > 0:
        top_overdue.append({
            "title": t.get("TITLE", ""),
            "employee": employee_fio,
            "dept": dept_name,
            "days": overdue_days,
            "url": task_url,
        })

    # Компактная запись по задаче — нужна дашборду для динамических фильтров
    # (по сотруднику, отделу, диапазону дат) без повторной выгрузки.
    tasks_out.append({
        "dept": dept_name,
        "employee": employee_fio,
        "created": created.date().isoformat() if created else None,
        "closed": closed.date().isoformat() if closed else None,
        "deadline": deadline.date().isoformat() if deadline else None,
        "done": is_done,
        "in_progress": status in STATUS_IN_PROGRESS,
        "overdue_now": overdue_now,
        "closed_overdue": closed_overdue,
        "overdue_days": overdue_days,
        "no_deadline": not has_deadline,
        "title": t.get("TITLE", ""),
        "url": task_url,
    })

quality["added_tasks"] = valid_tasks
print(f"Итог: добавлено {valid_tasks}, исключено (неизвестный ID) {quality['excluded_unknown_responsible']}, "
      f"неактивных {quality['excluded_inactive_responsible']}, по списку {quality['excluded_by_excluded_list']}, "
      f"по названию {quality['excluded_by_title']}, по отделу {quality['excluded_by_department']}")

# ============================== BUILD JSON ==============================

employees_out = []
for uid, es in emp_stats.items():
    total = es["total"]
    employees_out.append({
        "dept": es["dept"], "fio": es["fio"], "position": es["position"],
        "total": total, "done": es["done"], "in_progress": es["in_progress"],
        "overdue_now": es["overdue_now"], "closed_overdue": es["closed_overdue"],
        "no_deadline": es["no_deadline"],
        "pct_done": round(100 * es["done"] / total, 1) if total else 0,
        "avg_overdue": round(sum(es["overdue_days"]) / len(es["overdue_days"]), 1) if es["overdue_days"] else 0,
    })
employees_out.sort(key=lambda e: (e["dept"], e["fio"]))

departments_out = []
for dept, ds in dept_stats.items():
    total = ds["total"]
    departments_out.append({
        "dept": dept, "employees": len(ds["employees"]), "total": total,
        "done": ds["done"], "in_progress": ds["in_progress"],
        "overdue_now": ds["overdue_now"], "closed_overdue": ds["closed_overdue"],
        "pct_done": round(100 * ds["done"] / total, 1) if total else 0,
    })
departments_out.sort(key=lambda d: -d["total"])

daily_trend = [
    {"date": d, "created": v["created"], "completed": v["completed"]}
    for d, v in sorted(daily_counts.items())
]

top_overdue.sort(key=lambda x: -x["days"])
top_overdue = top_overdue[:15]

output = {
    "generated_at": now.isoformat(timespec="seconds"),
    "period_start": period_start.date().isoformat(),
    "period_end": period_end.date().isoformat(),
    "quality": quality,
    "employees": employees_out,
    "departments": departments_out,
    "daily_trend": daily_trend,
    "top_overdue": top_overdue,
    "tasks": tasks_out,
}

os.makedirs(os.path.dirname(OUTPUT_JSON) or ".", exist_ok=True)
with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"Готово: {OUTPUT_JSON}")
