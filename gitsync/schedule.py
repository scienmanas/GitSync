"""The schedule command: keep running, and sync on a cron expression.

The obvious implementation is to hand the schedule to the system's cron, and on
a host that is exactly what deploy.sh does. In a container it does not work: the
image runs unprivileged, and busybox crond silently refuses to start a job when
it is not root - so the choice is a root container or our own clock. This is the
clock.

Keeping the schedule in-process pays for itself anyway: a scheduled run logs
through the same redacting handlers as a manual one, Ctrl-C and SIGTERM stop it
the same way, and there is no second copy of the credentials in a crontab file.
"""

import logging
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# minute, hour, day-of-month, month, day-of-week
FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")

# Names standard cron accepts in place of numbers, so the `0 9 * * MON-FRI` people
# copy off the internet works. Keys are lowercase - parse_cron lowercases the
# whole expression first. Sunday is 0 here; 7 still works as a number.
MONTH_NAMES = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
WEEKDAY_NAMES = {name: number for number, name in enumerate(
    ("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
FIELD_ALIASES = (None, None, None, MONTH_NAMES, WEEKDAY_NAMES)

# The most days each month can have - February's 29 counts, since a leap year
# does come round. Used to reject a date no calendar ever has.
DAYS_IN_MONTH = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
                 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

# The usual shorthands, so `--cron @daily` works
MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


class CronError(ValueError):
    """A cron expression we cannot make sense of."""


# A parsed cron expression: the five sets of allowed numbers, plus whether
# each day field was left as "*". Built by parse_cron below; the scheduler
# asks matches(now) once a minute.


class CronSchedule:
    def __init__(self, minutes, hours, days, months, weekdays,
                 every_day_of_month, every_weekday, text) -> None:
        self.minutes = minutes
        self.hours = hours
        self.days = days
        self.months = months
        self.weekdays = weekdays
        self.every_day_of_month = every_day_of_month
        self.every_weekday = every_weekday
        self.text = text

    # Is this the minute to run?
    def matches(self, when) -> bool:
        if (when.minute not in self.minutes
                or when.hour not in self.hours
                or when.month not in self.months):
            return False

        # cron's one genuinely odd rule: when both day fields are restricted,
        # either one matching is enough - that is how "the 1st, or any Monday"
        # is written. When only one is restricted, it has to match.
        # datetime has Monday=0, cron has Sunday=0.
        weekday = (when.weekday() + 1) % 7
        day_matches = when.day in self.days
        weekday_matches = weekday in self.weekdays

        if self.every_day_of_month and self.every_weekday:
            return True
        if self.every_day_of_month:
            return weekday_matches
        if self.every_weekday:
            return day_matches
        return day_matches or weekday_matches

    def __str__(self) -> str:
        return self.text


# Turn ONE field of a cron expression into the set of numbers it stands for.
# parse_cron calls it five times, once per field - it never sees the whole
# expression.
#
#   spec     the field's text: "*", "5", "1,15", "9-17", "*/15", "9-17/2",
#            "5/10", "mon-fri"
#   low/high the values this field allows, e.g. 0 and 59 for minutes
#   name     the field's name, only used in error messages ("hour")
#   aliases  names accepted in place of numbers ({"mon": 1, ...}), or None
#            for fields that take numbers only
#
# Returns a set of ints, e.g. "9-17/2" in the hour field -> {9, 11, 13, 15, 17}.
# Raises CronError for anything malformed or out of range.


def parse_field(spec, low, high, name, aliases=None) -> set[int]:
    values = set()
    for part in spec.split(","):
        if not part:
            raise CronError(f"empty {name} field in {spec!r}")

        step = 1
        # Whether a "/" was written at all - not whether step > 1. "0/1" in
        # the hour field means "from 0, every hour", and keying off the step
        # value would read it as just {0}: daily instead of hourly.
        has_step = "/" in part
        if has_step:
            part, _, step_text = part.partition("/")
            # isdecimal, not isdigit: isdigit also passes "²", which int()
            # then rejects with a plain ValueError the CLI does not catch
            if not step_text.isdecimal() or int(step_text) == 0:
                raise CronError(f"bad step {step_text!r} in the {name} field")
            step = int(step_text)

        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            start = _number(start_text, name, aliases)
            end = _number(end_text, name, aliases)
            if start > end:
                raise CronError(
                    f"{name} range {part!r} runs backwards - write the smaller "
                    "value first, or list the values instead (e.g. 5,6,0)")
        else:
            start = _number(part, name, aliases)
            # "5/10" means "from 5 to the end of the range, every 10"
            end = high if has_step else start

        if start < low or end > high:
            raise CronError(
                f"{name} value {part!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))

    return values


def _number(text, name, aliases=None) -> int:
    if aliases and text in aliases:
        return aliases[text]
    if not text.isdecimal():
        raise CronError(f"{text!r} is not a number in the {name} field")
    return int(text)


# Turn a whole cron expression into a CronSchedule the scheduler can check the
# clock against.
#
#   expression  what the user wrote: "0 2 * * *", "0 9 * * MON-FRI", "@daily"
#
# Steps: expand a macro (@daily -> "0 0 * * *"), split into the 5 fields
# (minute hour day-of-month month day-of-week), parse each with parse_field,
# treat Sunday 7 as 0, and refuse a date no calendar has (31 February).
#
# Returns a CronSchedule holding the five sets of allowed numbers - e.g.
# "0 2 * * *" -> minutes {0}, hours {2}, every day, month and weekday - whose
# matches(now) answers "is this the minute to run?".
# Raises CronError for anything it cannot make sense of; nothing is run.


def parse_cron(expression) -> CronSchedule:
    text = expression.strip().lower()
    text = MACROS.get(text, text)

    parts = text.split()
    if len(parts) != 5:
        raise CronError(
            f"expected 5 fields (minute hour day month weekday), got {len(parts)}: "
            f"{expression!r}")

    # fields = [{0}, {9}, {1..31}, {1..12}, {7}]
    #  min   hour  day      month    weekday
    fields = [parse_field(part, low, high, name, aliases)
              for part, (low, high), name, aliases
              in zip(parts, FIELD_BOUNDS, FIELD_NAMES, FIELD_ALIASES)]

    # Sunday is both 0 and 7 in cron; normalise so matching only checks one
    weekdays = {0 if day == 7 else day for day in fields[4]}

    every_day_of_month = parts[2] == "*"
    every_weekday = parts[4] == "*"

    # "0 0 31 2 *" parses fine and then never fires - the worst kind of backup
    # schedule, one that quietly never runs. Only when the day-of-month alone
    # decides (a restricted weekday would still fire on its own - see
    # CronSchedule.matches) must at least one day exist in one chosen month.
    if not every_day_of_month and every_weekday and not any(
            day <= DAYS_IN_MONTH[month] for month in fields[3] for day in fields[2]):
        raise CronError(
            f"{expression.strip()!r} never runs - none of those months has that "
            "day of the month")

    return CronSchedule(minutes=fields[0], hours=fields[1], days=fields[2],
                        months=fields[3], weekdays=weekdays,
                        every_day_of_month=every_day_of_month,
                        every_weekday=every_weekday,
                        text=expression.strip())


# Refuse anything that would run more often than once an hour. A sync or an
# archive walks the whole account, and a slot every few minutes would mostly
# hit GitHub/GitLab rate limits (or overlap with a run still going) rather than
# back anything up. Hourly-or-slower is exactly "one value in the minute field":
# with a single minute, two runs are always at least an hour apart, while two or
# more (*/15, 0,30, 5-10) fire several times in any hour that matches.


def check_min_interval(schedule) -> None:
    if len(schedule.minutes) > 1:
        raise CronError(
            f"{schedule.text!r} would run {len(schedule.minutes)} times an hour - "
            "the most often a schedule may run is hourly. Pick a single minute, "
            "e.g. '0 * * * *' (hourly) or '0 2 * * *' (daily at 02:00)")


# Seconds until the start of the next minute, plus a hair so we land inside it
# rather than on the boundary, where rounding could hand us the same minute twice


def seconds_until_next_minute(now) -> float:
    following = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    return max(0.1, (following - now).total_seconds() + 0.5)


# Run `runner` whenever the expression says so, until interrupted.
#
# now/sleep/max_ticks exist so the tests can drive the loop with a fake clock;
# nothing else passes them.


def run_schedule(expression, description, runner, run_on_start=False,
                 now=datetime.now, sleep=time.sleep, max_ticks=None) -> int:
    schedule = parse_cron(expression)
    check_min_interval(schedule)   # before anything runs, run-on-start included
    logger.info("⏰ Scheduled: %s (%s)", description, schedule)

    last_status = 0
    ticks = 0
    last_run_minute = None
    try:
        if run_on_start:
            logger.info("Running %s now (run-on-start)", description)
            started = now()
            last_status = _run_slot(runner, description)
            if last_status == 130:
                return _stopped()
            _log_skipped_slots(schedule, description, started, now())

        while max_ticks is None or ticks < max_ticks:
            sleep(seconds_until_next_minute(now()))
            ticks += 1

            current = now()
            # A run that overruns its slot must not re-trigger on the way out
            minute = current.replace(second=0, microsecond=0)
            if minute == last_run_minute or not schedule.matches(current):
                continue

            last_run_minute = minute
            logger.info("⏰ %s - starting %s",
                        current.strftime("%Y-%m-%d %H:%M"), description)
            last_status = _run_slot(runner, description)
            finished = now()
            # sync and archive catch Ctrl-C/SIGTERM themselves - to drain the
            # workers, delete a half-written zip - and report it as 130. That
            # interrupt was meant for us too: without this check the loop
            # would go back to sleep and `docker stop` would end in SIGKILL.
            if last_status == 130:
                return _stopped()
            logger.info("Finished %s (exit %s); waiting for the next slot",
                        description, last_status)
            _log_skipped_slots(schedule, description, current, finished)
    except KeyboardInterrupt:
        # Interrupted while sleeping between slots
        return _stopped()

    return last_status


# Runs never overlap: the loop waits for one to finish before it looks at the
# clock again, so a slot that comes round while a run is still going (a slow
# network, a huge first clone) is never started. That is on purpose - two runs
# on the same mirrors at once is exactly what must not happen - but it should
# not happen silently. After each run, list the slots it ran over and say so.
#
# Every minute in (started, finished] is checked against the schedule: the
# started minute itself was the slot that ran, and the loop's next wake-up is
# the minute after finished. Capped at a week of minutes, so even a run stuck
# for days costs a bounded, cheap loop (and an hourly schedule names at most
# 168 skipped slots).


def _log_skipped_slots(schedule, description, started, finished) -> list:
    start = started.replace(second=0, microsecond=0)
    end = min(finished.replace(second=0, microsecond=0), start + timedelta(days=7))
    skipped = []
    minute = start + timedelta(minutes=1)
    while minute <= end:
        if schedule.matches(minute):
            skipped.append(minute)
        minute += timedelta(minutes=1)
    if skipped:
        logger.warning(
            "Skipped %d scheduled run(s) of %s at %s - the previous run "
            "(started %s) was still going. Runs never overlap; the next one "
            "is at the following slot.",
            len(skipped), description,
            ", ".join(m.strftime("%Y-%m-%d %H:%M") for m in skipped),
            started.strftime("%Y-%m-%d %H:%M"))
    return skipped


def _stopped() -> int:
    logger.warning("Schedule stopped.")
    return 130


# One slot's run. A failed slot must not end the schedule: an unattended
# container would exit, restart, run-on-start straight into the same outage,
# and loop. So an unexpected exception - or a SystemExit, which is how e.g. an
# unreachable GitLab group lookup stops a one-off run - becomes a failed exit
# code for this slot, and the next slot tries again. Only an interrupt stops it.


def _run_slot(runner, description) -> int:
    try:
        return runner()
    except KeyboardInterrupt:
        raise
    except SystemExit as e:
        # SystemExit() / SystemExit(0) mean success; a message string means failure
        code = 0 if e.code is None else e.code if isinstance(e.code, int) else 1
        logger.error("%s stopped with %s (exit %s); trying again next slot",
                     description, e, code)
        return code
    except Exception as e:
        logger.exception("%s crashed: %s; trying again next slot", description, e)
        return 1
