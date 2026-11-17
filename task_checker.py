import json
import subprocess
from datetime import datetime, time, timedelta, timezone
from xml.etree import ElementTree

TASK_XML = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$tasks = @(
    foreach ($task in $service.GetFolder('\').GetTasks(1)) {
        if ($task.Name -match '^R6_DROPS_(?:DISCOVERY|(?:REFRESH_)?\d{4}-\d{2}-\d{2})$') {
            [pscustomobject]@{name=$task.Name; enabled=$task.Enabled; xml=$task.Xml}
        }
    }
)
ConvertTo-Json -InputObject $tasks -Compress
"""


def _registered_tasks():
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", QUERY],
            capture_output=True, text=True, encoding="utf-8-sig", errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Could not query Windows Task Scheduler: {error}") from error
    if result.returncode:
        raise RuntimeError(f"Windows Task Scheduler query failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError("Windows Task Scheduler returned invalid JSON") from error


def _windows_local_time(value):
    if value.tzinfo is not None:
        return value
    # A trigger in a daylight-saving gap moves to the first valid local time.
    if datetime.fromtimestamp(value.timestamp()) != value:
        value = value.replace(second=0, microsecond=0)
        while datetime.fromtimestamp(value.timestamp()) != value:
            value += timedelta(minutes=1)
    return value.astimezone()


def _trigger_time(trigger, day, zone):
    start = datetime.fromisoformat(trigger.findtext("task:StartBoundary", namespaces=TASK_XML))
    end_text = trigger.findtext("task:EndBoundary", namespaces=TASK_XML)
    end = datetime.fromisoformat(end_text) if end_text else None
    # A boundary without an offset follows the Windows local wall clock.
    start_of_day = datetime.combine(day, time.min, tzinfo=zone)
    end_of_day = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    first = start_of_day.astimezone(start.tzinfo).date()
    last = end_of_day.astimezone(start.tzinfo).date()
    daily = trigger.find("task:ScheduleByDay", TASK_XML)
    weekly = trigger.find("task:ScheduleByWeek", TASK_XML)
    if trigger.tag.endswith("}CalendarTrigger") and daily is None and weekly is None:
        raise ValueError("Only daily and weekly calendar triggers are supported")
    if daily is not None:
        daily_interval = int(daily.findtext("task:DaysInterval", default="1", namespaces=TASK_XML))
    if weekly is not None:
        weekly_interval = int(weekly.findtext("task:WeeksInterval", default="1", namespaces=TASK_XML))
    candidate_day = first
    while candidate_day <= last:
        elapsed_days = (candidate_day - start.date()).days
        candidate = datetime.combine(candidate_day, start.timetz())
        candidate_day += timedelta(days=1)
        if elapsed_days < 0:
            continue
        if daily is not None:
            if elapsed_days % daily_interval:
                continue
        elif weekly is not None:
            weeks = (elapsed_days + (start.weekday() + 1) % 7) // 7
            if weeks % weekly_interval:
                continue
            if weekly.find(f"task:DaysOfWeek/task:{WEEKDAYS[candidate.weekday()]}", TASK_XML) is None:
                continue
        elif elapsed_days:
            continue
        candidate = _windows_local_time(candidate)
        if end is not None and candidate > _windows_local_time(end):
            continue
        local = candidate.astimezone(zone)
        if local.date() == day:
            yield local


def list_scheduled_tasks(day, zone):
    rows = []
    for task in _registered_tasks():
        try:
            root = ElementTree.fromstring(task["xml"])
            triggers = root.findall("task:Triggers/*", TASK_XML)
            times = set()
            for trigger in triggers:
                if trigger.findtext("task:Enabled", default="true", namespaces=TASK_XML) in {"false", "0"}:
                    continue
                trigger_type = trigger.tag.rsplit("}", 1)[-1]
                if trigger_type in {"TimeTrigger", "CalendarTrigger"}:
                    times.update(run_time.astimezone(timezone.utc) for run_time in _trigger_time(trigger, day, zone))
            for run_time in times:
                rows.append({
                    "task_name": task["name"],
                    "scheduled_at": run_time.astimezone(zone).isoformat(),
                    "enabled": task["enabled"],
                })
        except (ElementTree.ParseError, KeyError, TypeError, ValueError) as error:
            raise RuntimeError(f"Invalid trigger for {task['name']}: {error}") from error
    return sorted(rows, key=lambda row: (datetime.fromisoformat(row["scheduled_at"]), row["task_name"]))
