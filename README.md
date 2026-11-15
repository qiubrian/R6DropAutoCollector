# AutoR6DropCollector
Combining the Twitch auto joiner script with a built in and autonomous checker that screens for R6 matches that have drops and calls the twitch joiner to join streams to automatically collect drops

This project works by taking information from PandaScore API for future siege games, running it through a filter that only includes games that would likely have twitch drops, and schedules the twitch autojoiner to join streams an interchangable amount of time before games, even when your laptop is asleep. To configure, create a config.json file by following the example format and inserting your PandaScore API key and timezone as well as the events you would like to set the autowatcher for.

## Usage

Run these commands from the project folder on Windows.

### Set up or update the schedule

```bash
python main.py activate
python main.py discover
python main.py refresh
```

- `activate` registers weekly discovery and builds the initial schedule.
- `discover` updates the schedule and retries on failure after 15, 30, and 60 seconds. Windows runs it weekly at the configured day and time.
- `refresh` makes one attempt to update the schedule. Windows runs it at midnight during weeks with matching events.

### Check PandaScore matches

The API checker displays a table by default. Use `--json` for the full match records. Both checkers use the configured time zone and default to today when you omit the date.

```bash
python main.py check api
python main.py check api 2026-11-13
python main.py check api 2026-11-13 --json
```

### Check scheduled Windows tasks

The task checker displays launch times in your configured local time zone. Use `--json` for machine-readable output.

```bash
python main.py check tasks
python main.py check tasks 2026-11-13
python main.py check tasks 2026-11-13 --json
```
