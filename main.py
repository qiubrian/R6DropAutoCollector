import argparse
import csv
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from itertools import zip_longest
from textwrap import wrap
from time import sleep
from zoneinfo import ZoneInfo

import requests

scriptFolder = os.path.dirname(os.path.abspath(__file__))
configPath = os.path.join(scriptFolder, "config.json")
collectorScript = os.path.abspath(__file__)
launcherFolder = os.path.join(scriptFolder, "launchers")
with open(configPath, "r") as file:
    config = json.load(file)

PANDASCORE_TOKEN = config["pandascore"]["token"]
TWITCH_SCRIPT = config["twitch"]["script_path"]
START_EARLY_MINUTES = config["schedule"]["start_early_minutes"]
TIMEZONE = config["schedule"]["timezone"]
EVENT_KEYWORDS = config["events"]["keywords"]
MATCHES_URL = "https://api.pandascore.co/r6siege/matches"
DISCOVERY_TASK_NAME = "R6_DROPS_DISCOVERY"
DISCOVERY_RETRY_DELAYS = (15, 30, 60)
LOOKAHEAD_DAYS = config["schedule"].get("lookahead_days", 14)
DISCOVERY_DAY = config["schedule"].get("discovery_day", "SUN").strip().upper()
DISCOVERY_TIME = config["schedule"].get("discovery_time", "12:00").strip()
userTimeZone = ZoneInfo(TIMEZONE)
py = sys.executable

def getPandaScoreMatches(url, filters=None, showProgress=False):
    allMatches = []
    page = 1
    headers = {"Accept": "application/json", "Authorization": f"Bearer {PANDASCORE_TOKEN}"}
    while True:
        if showProgress:
            print(f"Retrieving page {page}")
        params = {**(filters or {}), "per_page": 100, "page": page}
        response = requests.get(url, headers = headers, params = params, timeout = 30)
        response.raise_for_status()
        matches = response.json()
        if not isinstance(matches, list):
            raise ValueError("PandaScore returned an unexpected response")
        for match in matches:
            if not isinstance(match, dict):
                raise ValueError("PandaScore returned an invalid match")
        allMatches.extend(matches)
        if len(matches) < 100:
            break
        page += 1
    return allMatches

def getUpcomingMatches():
    return getPandaScoreMatches(f"{MATCHES_URL}/upcoming", showProgress=True)

def getMatchesForDay(day):
    midnight = datetime.combine(day, time.min, tzinfo=userTimeZone)
    nextMidnight = datetime.combine(day + timedelta(days=1), time.min, tzinfo=userTimeZone)
    utcRange = ",".join(
        boundary.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        for boundary in (midnight, nextMidnight)
    )
    matches = getPandaScoreMatches(MATCHES_URL, {"range[begin_at]": utcRange, "sort": "begin_at,id"})
    return [
        match for match in matches
        if match.get("begin_at") and convertTime(match["begin_at"]).date() == day
    ]

def getEventName(match):
    league = match.get("league") or {}
    tournament = match.get("tournament") or {}
    serie = match.get("serie") or {}
    parts = [match.get("name") or "", league.get("name") or "", serie.get("name") or "", serie.get("full_name") or "", tournament.get("name") or ""]
    return " | ".join(parts).lower()

def isDropEvent(match):
    eventName = getEventName(match)
    for word in EVENT_KEYWORDS:
        strippedWord = word.strip().lower()
        if not strippedWord:
            continue
        if strippedWord in eventName:
            return True
    return False

def convertTime(apiTime):
    if not isinstance(apiTime, str):
        raise ValueError("Expected a PandaScore timestamp")
    originalTime = datetime.fromisoformat(apiTime.replace("Z", "+00:00"))
    if originalTime.tzinfo is None:
        raise ValueError("PandaScore timestamp is missing its UTC offset")
    localTime = originalTime.astimezone(userTimeZone)
    return localTime

def formatLocalTime(localTime):
    return localTime.strftime("%I:%M %p %Z").lstrip("0")

def matchSummary(match):
    league = (match.get("league") or {}).get("name") or ""
    series = match.get("serie") or {}
    seriesName = series.get("full_name") or series.get("name") or ""
    event = seriesName
    if league and league.casefold() not in seriesName.casefold():
        event = f"{league} {seriesName}".strip()
    stage = (match.get("tournament") or {}).get("name")

    opponents = [entry.get("opponent") or {} for entry in match.get("opponents") or []]
    teamNames = [opponent.get("name") or "TBD" for opponent in opponents]
    if len(teamNames) == 1:
        teamNames.append("TBD")
    teams = " vs ".join(teamNames) if teamNames else match.get("name") or "TBD"

    start = formatLocalTime(convertTime(match["begin_at"]))
    status = match.get("status") or "unknown"
    statusLabel = "Live" if status == "running" else status.replace("_", " ").capitalize()
    scoreLabel = "-"
    if status in {"running", "finished"} and len(opponents) == 2:
        scoresByTeam = {
            result["team_id"]: result.get("score")
            for result in match.get("results") or []
            if result.get("team_id") is not None
        }
        scores = [scoresByTeam.get(opponent.get("id")) for opponent in opponents]
        if all(score is not None for score in scores):
            scoreLabel = "-".join(map(str, scores))

    matchType = match.get("match_type") or ""
    formatLabel = matchType.replace("_", " ").capitalize()
    gameCount = match.get("number_of_games")
    if gameCount and matchType in {"best_of", "first_to"}:
        formatLabel = f"{formatLabel} {gameCount}"
    return start, event or "-", stage or "-", teams, scoreLabel, formatLabel or "-", statusLabel

def formatCheckTable(headers, values, title, wrapColumns):
    rows = [headers]
    for row in values:
        cells = ("".join(ch if ch.isprintable() else " " for ch in cell) for cell in row)
        rows.append(tuple(" ".join(cell.split()) for cell in cells))

    widths = [max(map(len, column)) for column in zip(*rows)]
    separator = " | "
    availableWidth = shutil.get_terminal_size(fallback=(112, 24)).columns
    while sum(widths) + len(separator) * (len(headers) - 1) > availableWidth:
        wrappable = [index for index in wrapColumns if widths[index] > len(headers[index])]
        if not wrappable:
            break
        column = max(wrappable, key=lambda index: widths[index])
        widths[column] -= 1

    lines = [title, ""]
    for index, row in enumerate(rows):
        cells = [wrap(cell, width, break_on_hyphens=False) or [""] for cell, width in zip(row, widths)]
        for line in zip_longest(*cells, fillvalue=""):
            lines.append(separator.join(f"{cell:<{width}}" for cell, width in zip(line, widths)).rstrip())
        if index == 0:
            lines.append("-+-".join("-" * width for width in widths))
    return "\n".join(lines)

def formatMatchTable(matches, day):
    if not matches:
        return f"No PandaScore matches for {day} ({userTimeZone})."

    headers = ("Start time", "Event", "Stage", "Teams", "Score", "Format", "Status")
    count = len(matches)
    noun = "match" if count == 1 else "matches"
    title = f"PandaScore | {day} ({userTimeZone}) | {count} {noun}"
    return formatCheckTable(headers, (matchSummary(match) for match in matches), title, (1, 2, 3))

def formatTaskTable(tasks, day):
    if not tasks:
        return f"No collector tasks scheduled for {day} ({userTimeZone})."

    rows = []
    for task in tasks:
        name = task["task_name"]
        if name == DISCOVERY_TASK_NAME:
            taskType = "Weekly discovery"
        elif name.startswith("R6_DROPS_REFRESH_"):
            taskType = "Daily refresh"
        else:
            taskType = "Twitch launch"
        launchTime = datetime.fromisoformat(task["scheduled_at"]).astimezone(userTimeZone)
        status = "Enabled" if task["enabled"] else "Disabled"
        rows.append((formatLocalTime(launchTime), taskType, name, status))

    count = len(tasks)
    noun = "task" if count == 1 else "tasks"
    title = f"Windows Task Scheduler | {day} ({userTimeZone}) | {count} {noun}"
    return formatCheckTable(("Launch time", "Activity", "Task name", "Status"), rows, title, (1, 2))

def findFirstMatch(matches):
    firstMatchStartTime = None
    for match in matches:
        apiTime = match.get("begin_at")
        if not apiTime:
            continue
        localTime = convertTime(apiTime)
        if firstMatchStartTime is None or localTime < firstMatchStartTime:
            firstMatchStartTime = localTime
    return firstMatchStartTime

def findLastMatch(matches):
    lastMatchStartTime = None
    for match in matches:
        apiTime = match.get("begin_at")
        if not apiTime:
            continue
        localTime = convertTime(apiTime)
        if lastMatchStartTime is None or localTime > lastMatchStartTime:
            lastMatchStartTime = localTime
    return lastMatchStartTime

def refreshMatchTasks():
    matchTaskNames = []
    allTasksCreated = True
    print("Refreshing matches")
    try:
        matches = getUpcomingMatches()
    except requests.RequestException as e:
        print("Failed to refresh matches")
        print(e)
        return None
    except ValueError as e:
        print("PandaScore returned invalid data")
        print(e)
        return None
    try:
        dropMatches = [match for match in matches if isDropEvent(match)]
        if not dropMatches:
            print("No upcoming drops matches")
            return {}, allTasksCreated, matchTaskNames
        allMatchesByDay = groupMatchesByDay(dropMatches)
    except (AttributeError, TypeError, ValueError) as error:
        print("PandaScore returned invalid match data")
        print(error)
        return None
    today = datetime.now(userTimeZone).date()
    lastDay = today + timedelta(days = LOOKAHEAD_DAYS)
    matchesByDay = {}
    for day in allMatchesByDay:
        if today <= day < lastDay:
            matchesByDay[day] = allMatchesByDay[day]
    if not matchesByDay:
        print(f"No drops matches inside the {LOOKAHEAD_DAYS}-day lookahead")
        return {}, allTasksCreated, matchTaskNames
    print("Drops-related match days found:")
    for day in sorted(matchesByDay):
        dailyMatches = matchesByDay[day]
        firstMatchTime = findFirstMatch(dailyMatches)
        lastMatchTime = findLastMatch(dailyMatches)
        if firstMatchTime is None or lastMatchTime is None:
            print(f"Could not determine match times for {day.isoformat()}")
            allTasksCreated = False
            continue
        try:
            launchPath = startTwitchLauncher(day, lastMatchTime)
        except (FileNotFoundError, OSError) as e:
            print(f"Couldnt activate Twitch Autojoiner for {day.isoformat()}")
            print(e)
            allTasksCreated = False
            continue
        launchTime = firstMatchTime - timedelta(minutes = START_EARLY_MINUTES)
        taskName = f"R6_DROPS_{day.isoformat()}"
        matchTaskNames.append(taskName)
        print(f"Date: {firstMatchTime.strftime('%A, %B %d, %Y')}")
        print(f"First match: {firstMatchTime.strftime('%I:%M %p %Z')}")
        print(f"Last match: {lastMatchTime.strftime('%I:%M %p %Z')}")
        print(f"Twitch launch: {launchTime.strftime('%I:%M %p %Z')}")
        taskCreated = scheduleJoiner(day, firstMatchTime, launchPath)
        if not taskCreated:
            allTasksCreated = False
    return matchesByDay, allTasksCreated, matchTaskNames

def configureTaskWake(taskName):
    taskNameLiteral = taskName.replace("'", "''")
    command = [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ErrorAction Stop; "
        f"Set-ScheduledTask -TaskPath '\\' -TaskName '{taskNameLiteral}' -Settings $settings -ErrorAction Stop | Out-Null; "
        f"$task = Get-ScheduledTask -TaskPath '\\' -TaskName '{taskNameLiteral}' -ErrorAction Stop; "
        "if (-not $task.Settings.WakeToRun -or -not $task.Settings.StartWhenAvailable -or "
        "$task.Settings.DisallowStartIfOnBatteries -or $task.Settings.StopIfGoingOnBatteries -or "
        "$task.Settings.MultipleInstances -ne 'IgnoreNew') { "
        "throw 'Task Scheduler did not retain the requested settings' }",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError as error:
        print(f"Could not configure wake settings for {taskName}")
        print(error)
        return False
    if result.returncode != 0:
        print(f"Could not configure wake settings for {taskName}")
        print(result.stdout)
        print(result.stderr)
        return False
    return True

def scheduleDailyRefresh(matchesByDay):
    refreshTaskNames = []
    allTasksCreated = True
    if not matchesByDay:
        print("No event weeks need daily refreshes")
        return allTasksCreated, refreshTaskNames
    try:
        os.makedirs(launcherFolder, exist_ok = True)
        refreshPath = os.path.join(launcherFolder, "run_r6_schedule_refresh.bat")
        refreshText = ("@echo off\n" f'cd /d "{scriptFolder}"\n' f'"{py}" "{collectorScript}" refresh\n' "exit /b %errorlevel%\n")
        with open(refreshPath, "w", encoding = "utf-8") as file:
            file.write(refreshText)
    except OSError as e:
        print("Could not create the schedule refresh launcher")
        print(e)
        return False, refreshTaskNames
    weekStarts = []
    for day in matchesByDay:
        weekStart = day - timedelta(days = day.weekday())
        if weekStart not in weekStarts:
            weekStarts.append(weekStart)
    today = datetime.now(userTimeZone).date()
    tomorrow = today + timedelta(days = 1)
    timeText = "00:00"
    for weekStart in sorted(weekStarts):
        weekEnd = weekStart + timedelta(days = 6)
        startDate = weekStart
        startDate = max(startDate, tomorrow)
        if startDate > weekEnd:
            print(f"No future midnight refreshes remain for the week of {weekStart}")
            continue
        taskName = f"R6_DROPS_REFRESH_{weekStart.isoformat()}"
        refreshTaskNames.append(taskName)
        startDateText = startDate.strftime("%m/%d/%Y")
        endDateText = weekEnd.strftime("%m/%d/%Y")
        command = ["schtasks", "/Create", "/TN", taskName, "/TR", f'"{refreshPath}"', "/SC", "DAILY", "/MO", "1", "/SD", startDateText, "/ED", endDateText, "/ST", timeText, "/IT", "/F"]
        try:
            result = subprocess.run(command, capture_output = True, text = True)
        except OSError as e:
            print("Could not run Windows Task Scheduler")
            print(e)
            allTasksCreated = False
            continue
        if result.returncode != 0:
            print(f"Could not create daily refresh task: {taskName}")
            print(result.stdout)
            print(result.stderr)
            allTasksCreated = False
            continue
        if not configureTaskWake(taskName):
            allTasksCreated = False
            continue
        print(f"Scheduled midnight refreshes from {startDate.isoformat()} through {weekEnd.isoformat()}")
    return allTasksCreated, refreshTaskNames

def groupMatchesByDay(matches):
    matchesByDay = {}
    for match in matches:
        apiTime = match.get("begin_at")
        if not apiTime:
            continue
        localTime = convertTime(apiTime)
        day = localTime.date()
        if day not in matchesByDay:
            matchesByDay[day] = []
        matchesByDay[day].append(match)
    return matchesByDay

def startTwitchLauncher(day, lastMatchTime):
    if not os.path.exists(TWITCH_SCRIPT):
        raise FileNotFoundError("Couldnt find twitch autojoiner")
    twitchFolder = os.path.dirname(TWITCH_SCRIPT)
    launchPath = os.path.join(twitchFolder, f"run_twitch_autojoiner_{day.isoformat()}.bat")
    lastMatchText = lastMatchTime.isoformat()
    launchText = ("@echo off\n" f'cd /d "{twitchFolder}"\n' f'"{py}" "{TWITCH_SCRIPT}" --streamer rainbow6 --interval 60 --last-match-time "{lastMatchText}"\n' "exit /b %errorlevel%\n")
    with open(launchPath, "w", encoding="utf-8") as file:
        file.write(launchText)
    return launchPath

def scheduleJoiner(day, firstMatchTime, launchPath):
    launchTime = (firstMatchTime - timedelta(minutes = START_EARLY_MINUTES))
    now = datetime.now(userTimeZone)
    minimumLaunchTime = now + timedelta(minutes = 2)
    minimumLaunchTime = minimumLaunchTime.replace(
        second = 0,
        microsecond = 0
    )
    launchTime = max(minimumLaunchTime, launchTime)
    systemLaunchTime = launchTime.astimezone()
    taskName = (f"R6_DROPS_{day.isoformat()}")
    dateText = systemLaunchTime.strftime("%m/%d/%Y")
    timeText = systemLaunchTime.strftime("%H:%M")
    command = ["schtasks", "/Create", "/TN", taskName, "/TR", f'"{launchPath}"', "/SC", "ONCE", "/SD", dateText, "/ST", timeText, "/IT", "/F"]
    try:
        result = subprocess.run(command, capture_output = True, text = True)
    except OSError as e:
        print("Could not run Windows Task Scheduler")
        print(e)
        return False
    if result.returncode != 0:
        print(f"Could not create task: {taskName}")
        print(result.stdout)
        print(result.stderr)
        return False
    if not configureTaskWake(taskName):
        return False
    print(f"Scheduled Twitch launch: {launchTime.strftime('%A, %B %d at %I:%M %p %Z')}")
    return True

def scheduleWeeklyCheck():
    discoveryPath = os.path.join(launcherFolder, "run_r6_discovery.bat")
    discoveryText = ("@echo off\n" f'cd /d "{scriptFolder}"\n' f'"{py}" "{collectorScript}" discover\n' "exit /b %errorlevel%\n")
    try:
        os.makedirs(launcherFolder, exist_ok = True)
        with open(discoveryPath, "w", encoding = "utf-8") as file:
            file.write(discoveryText)
    except OSError as error:
        print("Could not create the weekly discovery launcher")
        print(error)
        return False
    command = ["schtasks", "/Create", "/TN", DISCOVERY_TASK_NAME, "/TR", f'"{discoveryPath}"', "/SC", "WEEKLY", "/MO", "1", "/D", DISCOVERY_DAY, "/ST", DISCOVERY_TIME, "/IT", "/F"]
    try:
        result = subprocess.run(command, capture_output = True, text = True)
    except OSError as error:
        print("Could not run Windows Task Scheduler")
        print(error)
        return False
    if result.returncode != 0:
        print("Could not create the weekly discovery task")
        print(result.stdout)
        print(result.stderr)
        return False
    if not configureTaskWake(DISCOVERY_TASK_NAME):
        return False
    print(f"Weekly discovery scheduled for {DISCOVERY_DAY} at {DISCOVERY_TIME}")
    return True

def removeOldTasks(matchTaskNames = None, refreshTaskNames = None):
    query = ["schtasks", "/Query", "/FO", "CSV", "/NH"]
    try:
        result = subprocess.run(query, capture_output=True, text = True)
    except OSError as e:
        print("Failed to retreive scheduled tasks")
        print(e)
        return False
    if result.returncode != 0:
        print("Could not retrieve scheduled tasks")
        print(result.stdout)
        print(result.stderr)
        return False
    oldTaskPaths = []
    taskRows = csv.reader(result.stdout.splitlines())
    for row in taskRows:
        if not row:
            continue
        taskPath = row[0].strip()
        if not taskPath.startswith("\\"):
            continue
        taskName = taskPath[1:]
        if "\\" in taskName:
            continue
        if taskName == DISCOVERY_TASK_NAME:
            continue
        isRefreshTask = False
        if taskName.startswith("R6_DROPS_REFRESH_"):
            if refreshTaskNames is None:
                continue
            isRefreshTask = True
            dateText = taskName.replace("R6_DROPS_REFRESH_", "", 1)
        elif taskName.startswith("R6_DROPS_"):
            if matchTaskNames is None:
                continue
            dateText = taskName.replace("R6_DROPS_", "", 1)
        else:
            continue
        try:
            taskDate = datetime.strptime(dateText, "%Y-%m-%d").date()
        except ValueError:
            continue
        if taskDate.isoformat() != dateText:
            continue
        if isRefreshTask:
            if taskName in refreshTaskNames:
                continue
        else:
            if taskName in matchTaskNames:
                continue
        oldTaskPaths.append(taskPath)
    allTasksDeleted = True
    for taskPath in oldTaskPaths:
        deleteCommand = ["schtasks", "/Delete", "/TN", taskPath, "/F"]
        try:
            deleteResult = subprocess.run(deleteCommand, capture_output = True, text = True)
        except OSError as e:
            print(f"Could not delete old task: {taskPath}")
            print(e)
            allTasksDeleted = False
            continue
        if deleteResult.returncode != 0:
            print(f"Could not delete old task: {taskPath}")
            print(deleteResult.stdout)
            print(deleteResult.stderr)
            allTasksDeleted = False
            continue
        print(f"Removed old task: {taskPath}")
    return allTasksDeleted

def reconcileSchedule():
    matchResult = refreshMatchTasks()
    if matchResult is None:
        return False
    matchesByDay, matchTasksCreated, matchTaskNames = matchResult
    dailyTasksCreated, refreshTaskNames = scheduleDailyRefresh(matchesByDay)
    if not matchTasksCreated or not dailyTasksCreated:
        print("Skipping stale-task cleanup because replacement scheduling failed")
        return False
    return removeOldTasks(matchTaskNames, refreshTaskNames)

def executeScheduleMode(mode):
    weeklyTaskCreated = mode != "activate" or scheduleWeeklyCheck()
    scheduleUpdated = reconcileSchedule()
    if mode == "discover":
        for delay in DISCOVERY_RETRY_DELAYS:
            if scheduleUpdated:
                break
            print(f"Discovery failed. Retrying in {delay} seconds.", flush=True)
            sleep(delay)
            scheduleUpdated = reconcileSchedule()
        if not scheduleUpdated:
            print("Discovery failed after all retry attempts")
    return 0 if weeklyTaskCreated and scheduleUpdated else 1

def parseDay(value):
    try:
        day = date.fromisoformat(value)
        if day.isoformat() == value:
            return day
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("expected a calendar date in YYYY-MM-DD format")

def checkDay(argv):
    parser = argparse.ArgumentParser(prog="python main.py check")
    parser.add_argument("source", choices=("api", "tasks"))
    parser.add_argument("date", nargs="?", type=parseDay, help="YYYY-MM-DD, defaults to today.")
    parser.add_argument("--json", action="store_true", help="Print records as JSON.")
    args = parser.parse_intermixed_args(argv)
    day = args.date or datetime.now(userTimeZone).date()
    try:
        if args.source == "api":
            if not PANDASCORE_TOKEN or PANDASCORE_TOKEN == "YOUR_PANDASCORE_TOKEN":
                print("Set a PandaScore token in config.json.", file=sys.stderr)
                return 1
            matches = getMatchesForDay(day)
            output = json.dumps(matches, indent=2) if args.json else formatMatchTable(matches, day)
        else:
            if platform.system() != "Windows":
                print("This command requires Windows Task Scheduler.", file=sys.stderr)
                return 1
            from task_checker import list_scheduled_tasks

            tasks = list_scheduled_tasks(day, userTimeZone)
            output = json.dumps(tasks, indent=2) if args.json else formatTaskTable(tasks, day)
    except (requests.RequestException, RuntimeError, OverflowError, ValueError) as error:
        print(f"Could not check {args.source} for {day}: {error}", file=sys.stderr)
        return 1
    print(output)
    return 0

def main():
    if len(sys.argv) >= 2 and sys.argv[1].strip().lower() == "check":
        return checkDay(sys.argv[2:])
    if platform.system() != "Windows":
        print("This scheduler currently requires Windows")
        return 1
    if not PANDASCORE_TOKEN or PANDASCORE_TOKEN == "YOUR_PANDASCORE_TOKEN":
        print("No real pandascore token in config")
        return 1
    mode = "activate"
    if len(sys.argv) > 2:
        print("Usage: python main.py [activate|discover|refresh|check]")
        return 1
    if len(sys.argv) == 2:
        mode = sys.argv[1].strip().lower()
    if mode not in ["activate", "discover", "refresh"]:
        print("Mode must be activate, discover, refresh, or check")
        return 1
    return executeScheduleMode(mode)

if __name__ == "__main__":
    raise SystemExit(main())
