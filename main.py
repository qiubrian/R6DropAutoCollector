import sys
import os
import json
import subprocess
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import platform
import csv

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
API_URL = "https://api.pandascore.co/r6siege/matches/upcoming"
DISCOVERY_TASK_NAME = "R6_DROPS_DISCOVERY"
LOOKAHEAD_DAYS = config["schedule"].get("lookahead_days", 14)
DISCOVERY_DAY = config["schedule"].get("discovery_day", "SUN").strip().upper()
DISCOVERY_TIME = config["schedule"].get("discovery_time", "12:00").strip()
userTimeZone = ZoneInfo(TIMEZONE)
py = sys.executable

def getUpcomingMatches():
    allMatches = []
    page = 1
    headers = {"Accept": "application/json", "Authorization": f"Bearer {PANDASCORE_TOKEN}"}
    while True:
        print(f"Retreiving page {page}")
        params = {"per_page": 100, "page": page}
        response = requests.get(API_URL, headers = headers, params = params, timeout = 30)
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
    originalTime = datetime.fromisoformat(apiTime.replace("Z", "+00:00"))
    localTime = originalTime.astimezone(userTimeZone)
    return localTime

def findFirstMatch(matches):
    firstMatchStartTime = None
    for match in matches:
        apiTime = match.get("begin_at")
        if not apiTime:
            continue
        localTime = convertTime(apiTime)
        if firstMatchStartTime is None:
            firstMatchStartTime = localTime
        elif localTime < firstMatchStartTime:
            firstMatchStartTime = localTime
    return firstMatchStartTime

def findLastMatch(matches):
    lastMatchStartTime = None
    for match in matches:
        apiTime = match.get("begin_at")
        if not apiTime:
            continue
        localTime = convertTime(apiTime)
        if lastMatchStartTime is None:
            lastMatchStartTime = localTime
        elif localTime > lastMatchStartTime:
            lastMatchStartTime = localTime
    return lastMatchStartTime

def refreshMatchTasks():
    matchTaskNames = []
    allTasksCreated = True
    print("Rerunning retreiving matches")
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
    dropMatches = []
    for match in matches:
        if isDropEvent(match):
            dropMatches.append(match)
    if not dropMatches:
        print("No upcoming drops matches")
        return {}, allTasksCreated, matchTaskNames
    try:
        allMatchesByDay = groupMatchesByDay(dropMatches)
    except (TypeError, ValueError) as error:
        print("A match has an invalid start time")
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
        if startDate < tomorrow:
            startDate = tomorrow
        if startDate > weekEnd:
            print(f"No future midnight refreshes remain for the week of {weekStart}")
            continue
        taskName = f"R6_DROPS_REFRESH_{weekStart.isoformat()}"
        refreshTaskNames.append(taskName)
        startDateText = startDate.strftime("%m/%d/%Y")
        endDateText = weekEnd.strftime("%m/%d/%Y")
        command = ["schtasks", "/Create", "/TN", taskName, "/TR", refreshPath, "/SC", "DAILY", "/MO", "1", "/SD", startDateText, "/ED", endDateText, "/ST", timeText, "/IT", "/F"]
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
        wakeCommand = ["powershell", "-NoProfile", "-Command", f"$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; Set-ScheduledTask -TaskName '{taskName}' -Settings $settings"]
        try:
            wakeResult = subprocess.run(wakeCommand, capture_output = True, text = True)
        except OSError as e:
            print("Could not configure the daily refresh task")
            print(e)
            allTasksCreated = False
            continue
        if wakeResult.returncode != 0:
            print(f"Could not enable wake from sleep for {taskName}")
            print(wakeResult.stdout)
            print(wakeResult.stderr)
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
    if launchTime <= minimumLaunchTime:
        launchTime = minimumLaunchTime
    systemLaunchTime = launchTime.astimezone()
    taskName = (f"R6_DROPS_{day.isoformat()}")
    dateText = systemLaunchTime.strftime("%m/%d/%Y")
    timeText = systemLaunchTime.strftime("%H:%M")
    command = ["schtasks", "/Create", "/TN", taskName, "/TR", launchPath, "/SC", "ONCE", "/SD", dateText, "/ST", timeText, "/IT", "/F"]
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
    wakeCommand = ["powershell", "-NoProfile", "-Command", f"$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; Set-ScheduledTask -TaskName '{taskName}' -Settings $settings"]
    try:
        wakeResult = subprocess.run(wakeCommand, capture_output = True, text = True)
    except OSError as e:
        print("Couldnt start from sleep")
        print(e)
        return False
    if wakeResult.returncode != 0:
        print("Could not enable wake from sleep")
        print(wakeResult.stdout)
        print(wakeResult.stderr)
        return False
    print(f"Scheduled Twitch launch: {launchTime.strftime('%A, %B %d at %I:%M %p %Z')}")
    return True

def scheduleWeeklyCheck():
    os.makedirs(launcherFolder, exist_ok = True)
    discoveryPath = os.path.join(launcherFolder, "run_r6_discovery.bat")
    discoveryText = ("@echo off\n" f'cd /d "{scriptFolder}"\n' f'"{py}" "{collectorScript}" discover\n' "exit /b %errorlevel%\n")
    try:
        with open(discoveryPath, "w", encoding = "utf-8") as file:
            file.write(discoveryText)
    except OSError as error:
        print("Could not create the weekly discovery launcher")
        print(error)
        return False
    command = ["schtasks", "/Create", "/TN", DISCOVERY_TASK_NAME, "/TR", discoveryPath, "/SC", "WEEKLY", "/MO", "1", "/D", DISCOVERY_DAY, "/ST", DISCOVERY_TIME, "/IT", "/F"]
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
    wakeCommand = ["powershell", "-NoProfile", "-Command", f"$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; Set-ScheduledTask -TaskName '{DISCOVERY_TASK_NAME}' -Settings $settings"]
    try:
        wakeResult = subprocess.run(wakeCommand, capture_output = True, text = True)
    except OSError as error:
        print("Could not configure the weekly discovery task")
        print(error)
        return False
    if wakeResult.returncode != 0:
        print("Could not enable wake from sleep for weekly discovery")
        print(wakeResult.stdout)
        print(wakeResult.stderr)
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

def main():
    if platform.system() != "Windows":
        print("This scheduler currently requires Windows")
        return 1
    if not PANDASCORE_TOKEN or PANDASCORE_TOKEN == "YOUR_PANDASCORE_TOKEN":
        print("No real pandascore token in config")
        return 1
    mode = "activate"
    if len(sys.argv) > 2:
        print("Usage: python main.py [activate|discover|refresh]")
        return 1
    if len(sys.argv) == 2:
        mode = sys.argv[1].strip().lower()
    if mode not in ["activate", "discover", "refresh"]:
        print("Mode must be activate, discover, or refresh")
        return 1
    if mode == "activate":
        weeklyTaskCreated = scheduleWeeklyCheck()
        matchResult = refreshMatchTasks()
        if matchResult is None:
            return 1
        matchesByDay, matchTasksCreated, matchTaskNames = matchResult
        dailyTasksCreated, refreshTaskNames = scheduleDailyRefresh(matchesByDay)
        if not matchTasksCreated or not dailyTasksCreated:
            print("Keeping existing tasks because replacement scheduling failed")
            return 1
        oldTasksRemoved = removeOldTasks(matchTaskNames, refreshTaskNames)
        if weeklyTaskCreated and oldTasksRemoved:
            return 0
        return 1
    if mode == "discover":
        matchResult = refreshMatchTasks()
        if matchResult is None:
            return 1
        matchesByDay, matchTasksCreated, matchTaskNames = matchResult
        dailyTasksCreated, refreshTaskNames = scheduleDailyRefresh(matchesByDay)
        if not matchTasksCreated or not dailyTasksCreated:
            print("Keeping existing tasks because replacement scheduling failed")
            return 1

        oldTasksRemoved = removeOldTasks(matchTaskNames, refreshTaskNames)
        if oldTasksRemoved:
            return 0
        return 1
    if mode == "refresh":
        matchResult = refreshMatchTasks()
        if matchResult is None:
            return 1
        matchesByDay, matchTasksCreated, matchTaskNames = matchResult
        dailyTasksCreated, refreshTaskNames = scheduleDailyRefresh(matchesByDay)
        if not matchTasksCreated or not dailyTasksCreated:
            print("Keeping existing tasks because replacement scheduling failed")
            return 1
        oldTasksRemoved = removeOldTasks(matchTaskNames, refreshTaskNames)
        if oldTasksRemoved:
            return 0
        return 1
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
