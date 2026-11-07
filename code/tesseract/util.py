from __future__ import annotations

import datetime as dt


def parse_hhmm(s: str) -> dt.time:
    h, m = s.strip().split(":")
    return dt.time(int(h), int(m))


def next_at(hhmm: str, after: dt.datetime) -> dt.datetime:
    """The next time the clock reads hhmm, strictly after `after`."""
    t = parse_hhmm(hhmm)
    at = after.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    return at if at > after else at + dt.timedelta(days=1)


def darkness(cfg, now: dt.datetime | None = None, night_until: float = 0.0, day_until: float = 0.0) -> float:
    """How far into night mode the screen and LEDs are: 1 = night (dim, warm), 0 = day, and in
    between during the morning ramp from [night] sunrise_start to sunrise_end.

    Night starts when you say "good night" (night_until = the next sunrise_end), or by itself at
    [night] start if you forget. "Good morning" ends it early (day_until)."""
    now = now or dt.datetime.now()
    ts, N = now.timestamp(), cfg.night
    if day_until > ts:
        return 0.0
    rise, done, t = parse_hhmm(N.sunrise_start), parse_hhmm(N.sunrise_end), now.time()
    night = night_until > ts
    if not night and N.start:
        start = parse_hhmm(N.start)
        night = (start <= t or t < done) if start > done else (start <= t < done)
    if not night:
        return 0.0
    if rise <= t < done:
        span = (dt.datetime.combine(now.date(), done) - dt.datetime.combine(now.date(), rise)).total_seconds()
        gone = (now - dt.datetime.combine(now.date(), rise)).total_seconds()
        return max(0.0, 1 - gone / span)
    return 1.0


def blend(day: float, night: float, k: float) -> float:
    """Brightness k of the way from day to night, on a log scale so a ramp looks even to the eye."""
    if k <= 0 or night <= 0:
        return day if k <= 0 else day * (1 - k)
    return day ** (1 - k) * night ** k


def format_clock(t: dt.datetime | dt.time, h24: bool) -> tuple[str, str]:
    """-> ("7:05", "AM") or ("19:05", "")"""
    if h24:
        return f"{t.hour:02d}:{t.minute:02d}", ""
    h = t.hour % 12 or 12
    return f"{h}:{t.minute:02d}", "AM" if t.hour < 12 else "PM"


def spoken_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s and not h:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " and ".join(parts) or "0 seconds"
