import sys
import os
import json
import subprocess
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import platform

scriptFolder = os.path.dirname(os.path.abspath(__file__))
configPath = os.path.join(scriptFolder, "config.json")
with open(configPath, "r") as file:
    config = json.load(file)

PANDASCORE_TOKEN = config["pandascore"]["token"]
TWITCH_SCRIPT = config["twitch"]["script_path"]
START_EARLY_MINUTES = config["schedule"]["start_early_minutes"]
TIMEZONE = config["schedule"]["timezone"]
EVENT_KEYWORDS = config["events"]["keywords"]
API_URL = "https://api.pandascore.co/r6siege/matches/upcoming"
userTimeZone = ZoneInfo(TIMEZONE)
py = sys.executable

def getUpcomingMatches():
    allMatches = []
    page = 1
    while True:
        print(f"Retreiving page {page}")
        response = requests.get(API_URL, params = {"token": PANDASCORE_TOKEN, "per_page": 100, "page": page}, timeout = 30)
        response.raise_for_status()
        matches = response.json()
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
        if word.lower() in eventName:
            return True
    return False

def convertTime(apiTime):
    originalTime = datetime.fromisoformat(apiTime.replace("Z", "+00:00"))
    localTime = originalTime.astimezone(userTimeZone)
    return localTime

def findFirstMatch(matches):
    firstMatchByDay = {}
    for match in matches:
        if not isDropEvent(match):
            continue
        apiTime = match.get("begin_at")
        if not apiTime:
            continue
        localTime = convertTime(apiTime)
        day = localTime.date()
        if day not in firstMatchByDay:
            firstMatchByDay[day] = localTime
        elif localTime < firstMatchByDay[day]:
            firstMatchByDay[day] = localTime
    return firstMatchByDay

def startTwitchLauncher():
    if not os.path.exists(TWITCH_SCRIPT):
        raise FileNotFoundError("Couldnt find twitch autojoiner")
    twitchFolder = os.path.dirname(TWITCH_SCRIPT)
    launchPath = os.path.join(twitchFolder, "run_twitch_autojoiner.bat")
    launchText = ("@echo off\n" f'cd /d "{twitchFolder}"\n' f'"{py}" "{TWITCH_SCRIPT}"\n')
    with open(launchPath, "w", encoding="utf-8") as file:
        file.write(launchText)
    return launchPath

def scheduleJoiner(day, firstMatchTime, launchPath):
    launchTime = (firstMatchTime - timedelta(minutes = START_EARLY_MINUTES))
    now = datetime.now(userTimeZone)
    if launchTime <= now:
        launchTime = now + timedelta(minutes = 1)
    taskName = (f"R6_DROPS_{day.isoformat()}")
    dateText = launchTime.strftime("%m/%d/%Y")
    timeText = launchTime.strftime("%H:%M")
    command = ["schtasks", "/Create", "/TN", taskName, "/TR", launchPath, "/SC", "ONCE", "/SD", dateText, "/ST", timeText, "/IT", "/F"]
    result = subprocess.run(command, capture_output = True, text = True)
    wakeCommand = ["powershell", "-NoProfile", "-Command", f"$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable; Set-ScheduledTask -TaskName '{taskName}' -Settings $settings"]
    wakeResult = subprocess.run(wakeCommand, capture_output = True, text = True)
    if wakeResult.returncode != 0:
        print("Could not enable wake from sleep")
        print(wakeResult.stdout)
        print(wakeResult.stderr)
    if result.returncode == 0:
        print(f"Scheduled Twitch launch: " f"{launchTime.strftime('%A, %B %d at %I:%M %p %Z')}")
    else:
        print(f"\nCould not create task: " f"{taskName}")
        print(result.stdout)
        print(result.stderr)

def main():
    if PANDASCORE_TOKEN == "YOUR_PANDASCORE_TOKEN":
        print("No real pandascore token in config")
        return
    print("Retreiving matches")
    try:
        matches = getUpcomingMatches()
    except requests.RequestException:
        print("Failed to retreive matches")
        return
    print(f"PandaScore returned " f"{len(matches)} upcoming R6 matches")
    firstMatches = findFirstMatch(matches)
    if not firstMatches:
        print("No upcoming drops matches")
        return
    try:
        launchPath = startTwitchLauncher()
    except FileNotFoundError:
        return
    print("Drops-related match days found:")
    for day in sorted(firstMatches):
        firstMatch = firstMatches[day]
        launchTime = (firstMatch - timedelta(minutes = START_EARLY_MINUTES))
        print(f"Date: " f"{firstMatch.strftime('%A, %B %d, %Y')}")
        print(f"First match: " f"{firstMatch.strftime('%I:%M %p %Z')}")
        print(f"Twitch launch: " f"{launchTime.strftime('%I:%M %p %Z')}")
        scheduleJoiner(day, firstMatch, launchPath)

if __name__ == "__main__":
    main()
