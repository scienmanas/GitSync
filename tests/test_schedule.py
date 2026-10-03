"""The scheduler behind `main.py schedule` (and behind the container's cron
mode). Hand-written cron parsing earns a thorough suite: a schedule that is
quietly wrong means a backup that quietly never runs.
"""

from datetime import datetime, timedelta

import pytest

from gitsync.schedule import (CronError, check_min_interval, parse_cron,
                              run_schedule, seconds_until_next_minute)


def at(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# Parsing                                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("expression, when, expected", [
    ("0 2 * * *", "2026-08-15 02:00", True),
    ("0 2 * * *", "2026-08-15 02:01", False),
    ("0 2 * * *", "2026-08-15 03:00", False),
    ("* * * * *", "2026-08-15 13:47", True),
    ("*/15 * * * *", "2026-08-15 13:45", True),
    ("*/15 * * * *", "2026-08-15 13:46", False),
    ("30 9-17 * * *", "2026-08-15 09:30", True),
    ("30 9-17 * * *", "2026-08-15 18:30", False),
    ("0 0,12 * * *", "2026-08-15 12:00", True),
    ("0 9-17/4 * * *", "2026-08-15 13:00", True),
    ("0 9-17/4 * * *", "2026-08-15 14:00", False),
])
def test_matching(expression, when, expected):
    assert parse_cron(expression).matches(at(when)) is expected


@pytest.mark.parametrize("macro, when", [
    ("@hourly", "2026-08-15 13:00"),
    ("@daily", "2026-08-15 00:00"),
    ("@weekly", "2026-08-16 00:00"),    # a Sunday
    ("@monthly", "2026-08-01 00:00"),
    ("@yearly", "2026-01-01 00:00"),
])
def test_macros(macro, when):
    assert parse_cron(macro).matches(at(when)) is True


def test_sunday_is_both_zero_and_seven():
    sunday = at("2026-08-16 00:00")
    assert sunday.weekday() == 6          # datetime's Sunday
    assert parse_cron("0 0 * * 0").matches(sunday) is True
    assert parse_cron("0 0 * * 7").matches(sunday) is True


@pytest.mark.parametrize("when, expected", [
    ("2026-08-03 00:00", True),    # a Monday
    ("2026-08-01 00:00", True),    # the 1st, a Saturday
    ("2026-08-04 00:00", False),   # a Tuesday, not the 1st
])
def test_day_of_month_or_day_of_week(when, expected):
    # cron's oddest rule: with both day fields restricted, either match fires.
    # "the 1st of the month, or any Monday" - not "Mondays that are the 1st".
    assert parse_cron("0 0 1 * 1").matches(at(when)) is expected


def test_a_restricted_weekday_alone_must_match():
    assert parse_cron("0 0 * * 1").matches(at("2026-08-03 00:00")) is True
    assert parse_cron("0 0 * * 1").matches(at("2026-08-04 00:00")) is False


@pytest.mark.parametrize("expression", [
    "0 2 * *",              # too few fields
    "0 2 * * * *",          # too many
    "60 * * * *",           # minute out of range
    "* 24 * * *",           # hour out of range
    "* * 0 * *",            # there is no day 0
    "* * * 13 *",           # there is no month 13
    "*/0 * * * *",          # step of zero
    "abc * * * *",          # not a number
    "5-1 * * * *",          # backwards range
    "",
])
def test_nonsense_is_rejected(expression):
    with pytest.raises(CronError):
        parse_cron(expression)


def test_the_error_says_which_field():
    with pytest.raises(CronError, match="hour"):
        parse_cron("* 99 * * *")


# --------------------------------------------------------------------------- #
# The loop                                                                     #
# --------------------------------------------------------------------------- #

class FakeClock:
    """Drives the loop without waiting for real minutes to pass."""

    def __init__(self, start):
        self.now_value = at(start)

    def now(self):
        return self.now_value

    def sleep(self, seconds):
        self.now_value += timedelta(seconds=seconds)


def test_runs_on_every_matching_slot():
    clock = FakeClock("2026-08-15 12:00")
    fired = []
    run_schedule("0 * * * *", "sync",
                 runner=lambda: fired.append(clock.now().strftime("%H:%M")) or 0,
                 now=clock.now, sleep=clock.sleep, max_ticks=200)

    assert fired == ["13:00", "14:00", "15:00"]


def test_run_on_start_does_not_wait_for_the_first_slot():
    clock = FakeClock("2026-08-15 12:00")
    fired = []
    run_schedule("0 3 * * *", "sync",
                 runner=lambda: fired.append(clock.now().strftime("%H:%M")) or 0,
                 run_on_start=True, now=clock.now, sleep=clock.sleep, max_ticks=5)

    # Once immediately, and 03:00 is nowhere near, so that is all
    assert fired == ["12:00"]


def test_an_overrunning_run_does_not_double_fire():
    # A sync that takes longer than its slot must not be started again for the
    # same minute the moment it finishes.
    clock = FakeClock("2026-08-15 12:00")
    fired = []

    def slow_runner():
        fired.append(clock.now().strftime("%H:%M"))
        clock.sleep(150)     # three minutes of cloning
        return 0

    run_schedule("0 * * * *", "sync", runner=slow_runner,
                 now=clock.now, sleep=clock.sleep, max_ticks=200)

    assert fired == sorted(set(fired)), "a minute fired twice"
    assert fired == ["13:00", "14:00", "15:00"]


def test_ctrl_c_stops_the_schedule():
    clock = FakeClock("2026-08-15 12:00")

    def interrupt():
        raise KeyboardInterrupt

    assert run_schedule("0 * * * *", "sync", runner=interrupt,
                        now=clock.now, sleep=clock.sleep, max_ticks=100) == 130


def test_the_last_exit_code_is_reported():
    clock = FakeClock("2026-08-15 12:00")
    assert run_schedule("0 * * * *", "sync", runner=lambda: 1,
                        now=clock.now, sleep=clock.sleep, max_ticks=100) == 1


@pytest.mark.parametrize("when, expected", [
    ("2026-08-15 12:00:00", 60.5),
    ("2026-08-15 12:00:30", 30.5),
    ("2026-08-15 12:00:59", 1.5),
])
def test_sleeps_to_just_inside_the_next_minute(when, expected):
    # Landing exactly on the boundary risks reading the same minute twice
    now = datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
    assert seconds_until_next_minute(now) == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# At most hourly                                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("expression", [
    "* * * * *", "*/5 * * * *", "*/30 * * * *", "0,30 2 * * *", "0-5 * * * *",
])
def test_more_often_than_hourly_is_refused(expression):
    with pytest.raises(CronError, match="hourly"):
        run_schedule(expression, "sync", runner=lambda: pytest.fail("ran"),
                     run_on_start=True, max_ticks=0)


@pytest.mark.parametrize("expression", [
    "0 * * * *", "@hourly", "30 */2 * * *", "0 2 * * *", "@daily", "15 3 * * 1",
])
def test_hourly_or_slower_is_allowed(expression):
    check_min_interval(parse_cron(expression))


# --------------------------------------------------------------------------- #
# Parsing edge cases                                                           #
# --------------------------------------------------------------------------- #

def test_a_start_with_step_one_runs_to_the_end_of_the_range():
    # "0/1" in the hour field is every hour, not just midnight
    assert parse_cron("0 0/1 * * *").hours == set(range(24))
    assert parse_cron("0 6/6 * * *").hours == {6, 12, 18}


def test_a_backwards_range_says_so():
    with pytest.raises(CronError, match="runs backwards"):
        parse_cron("0 17-9 * * *")


@pytest.mark.parametrize("expression", ["² * * * *", "0 */² * * *"])
def test_unicode_digits_are_a_cron_error_not_a_crash(expression):
    # isdigit() passes "²" but int() does not - it must still be a CronError
    with pytest.raises(CronError):
        parse_cron(expression)


@pytest.mark.parametrize("expression, field, expected", [
    ("0 9 * * MON-FRI", "weekdays", {1, 2, 3, 4, 5}),
    ("0 9 * * sat,sun", "weekdays", {6, 0}),
    ("0 0 1 JAN,jul *", "months", {1, 7}),
    ("0 0 1 mar-may *", "months", {3, 4, 5}),
])
def test_month_and_weekday_names(expression, field, expected):
    assert getattr(parse_cron(expression), field) == expected


def test_names_only_work_where_they_belong():
    with pytest.raises(CronError):
        parse_cron("mon * * * *")


@pytest.mark.parametrize("expression", [
    "0 0 31 2 *", "0 0 30 2 *", "0 0 31 4,6,9,11 *",
])
def test_a_date_that_never_exists_is_refused(expression):
    with pytest.raises(CronError, match="never runs"):
        parse_cron(expression)


@pytest.mark.parametrize("expression", [
    "0 0 29 2 *",      # leap years only - rare, but real
    "0 0 31 * *",      # the months that have a 31st
    "0 0 31 2 1",      # a restricted weekday fires on its own
])
def test_a_date_that_can_happen_is_allowed(expression):
    parse_cron(expression)


# --------------------------------------------------------------------------- #
# Stopping, and surviving a bad slot                                           #
# --------------------------------------------------------------------------- #

def test_a_run_that_was_interrupted_stops_the_schedule():
    # sync/archive catch Ctrl-C/SIGTERM themselves and return 130 - the loop
    # must stop too instead of sleeping on until docker's SIGKILL
    clock = FakeClock("2026-08-15 12:00")
    fired = []
    status = run_schedule("0 * * * *", "sync",
                          runner=lambda: fired.append(1) or 130,
                          now=clock.now, sleep=clock.sleep, max_ticks=300)
    assert status == 130
    assert fired == [1]          # 13:00 ran and was interrupted; 14:00 never came


def test_an_interrupted_run_on_start_stops_before_any_slot():
    clock = FakeClock("2026-08-15 12:00")
    fired = []
    status = run_schedule("0 * * * *", "sync",
                          runner=lambda: fired.append(1) or 130, run_on_start=True,
                          now=clock.now, sleep=clock.sleep, max_ticks=300)
    assert status == 130
    assert fired == [1]


@pytest.mark.parametrize("failure", [RuntimeError("boom"),
                                     SystemExit("GitLab group not found")])
def test_a_crashing_slot_does_not_end_the_schedule(failure, captured_log):
    clock = FakeClock("2026-08-15 12:00")
    fired = []

    def runner():
        fired.append(clock.now().strftime("%H:%M"))
        if len(fired) == 1:
            raise failure
        return 0

    status = run_schedule("0 * * * *", "sync", runner=runner,
                          now=clock.now, sleep=clock.sleep, max_ticks=130)
    assert fired == ["13:00", "14:00"]     # 14:00 still ran after 13:00 blew up
    assert status == 0
    assert any("trying again next slot" in line for line in captured_log)


@pytest.mark.parametrize("code, expected", [(None, 0), (0, 0), (3, 3), ("msg", 1)])
def test_a_systemexit_becomes_that_slots_exit_code(code, expected):
    clock = FakeClock("2026-08-15 12:00")

    def runner():
        raise SystemExit(code)

    assert run_schedule("0 * * * *", "sync", runner=runner,
                        now=clock.now, sleep=clock.sleep, max_ticks=70) == expected


# --------------------------------------------------------------------------- #
# A run that overruns the next slot                                            #
# --------------------------------------------------------------------------- #

def test_slots_a_long_run_overlaps_are_skipped_and_logged(captured_log):
    # Hourly, and the 13:00 run takes 2h30 (slow network): 14:00 and 15:00
    # come round while it is still going. They must not start - and must be
    # named in the log - and the next real run is 16:00.
    clock = FakeClock("2026-08-15 12:00")
    fired = []

    def slow_runner():
        fired.append(clock.now().strftime("%H:%M"))
        if len(fired) == 1:
            clock.sleep(2.5 * 3600)
        return 0

    run_schedule("0 * * * *", "sync", runner=slow_runner,
                 now=clock.now, sleep=clock.sleep, max_ticks=240)

    assert fired[:2] == ["13:00", "16:00"]          # nothing started in between
    assert "14:00" not in fired and "15:00" not in fired
    skipped = [line for line in captured_log if "Skipped" in line]
    assert len(skipped) == 1
    assert "Skipped 2 scheduled run(s) of sync at 2026-08-15 14:00, 2026-08-15 15:00" in skipped[0]
    assert "started 2026-08-15 13:00" in skipped[0]


def test_a_run_that_fits_in_its_slot_logs_no_skip(captured_log):
    clock = FakeClock("2026-08-15 12:00")

    def runner():
        clock.sleep(20 * 60)           # 20 minutes, well before the next hour
        return 0

    run_schedule("0 * * * *", "sync", runner=runner,
                 now=clock.now, sleep=clock.sleep, max_ticks=130)
    assert not any("Skipped" in line for line in captured_log)


def test_a_long_run_on_start_reports_the_slots_it_covered(captured_log):
    clock = FakeClock("2026-08-15 12:30")
    fired = []

    def runner():
        fired.append(clock.now().strftime("%H:%M"))
        if len(fired) == 1:
            clock.sleep(2 * 3600)      # run-on-start lasts until 14:30
        return 0

    run_schedule("0 * * * *", "archive --mode code", runner=runner, run_on_start=True,
                 now=clock.now, sleep=clock.sleep, max_ticks=40)
    assert fired == ["12:30", "15:00"]
    assert any("Skipped 2 scheduled run(s) of archive --mode code at "
               "2026-08-15 13:00, 2026-08-15 14:00" in line for line in captured_log)
