"""Instant answers for the most common requests, with no LLM round trip.

"Set a timer for ten minutes", "how much time is left", "cancel the pasta
timer", "what time is it", "turn it up" are handled right here, and the reply
usually comes from the TTS cache, so audio starts a few milliseconds after the
transcript. Anything it isn't sure about (no duration, extra instructions,
several timers to pick from) returns None and goes to the fast model instead.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass
from typing import Callable

from .util import format_clock, spoken_duration

_ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i > 1}
_UNITS = {"second": 1, "seconds": 1, "sec": 1, "secs": 1, "minute": 60, "minutes": 60, "min": 60, "mins": 60,
          "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600}
# words a "set a timer" sentence may contain besides the duration and a short label
_FILLER = set("set start make create put on begin a an the timer countdown for called named please me can could "
              "you would and my new another up of to hey".split())
_NOT_A_SET = re.compile(r"\b(cancel|stop|delete|remove|clear|how|left|what|whats|is|pause|resume|change|add|"
                        r"extend|more|remaining|check|until|when|remind|alarm|every)\b")
_LEFT = re.compile(r"\b(how (much|long)|time left|whats left|remaining)\b")
_LEFT_BARE = re.compile(r"(how much (longer|time is left|time left|time do i have( left)?)|how long (is )?left|"
                        r"time left)")
_CANCEL = re.compile(r"(please )?(cancel|stop|delete|remove|clear|turn off|end)( the| my| that| this)?"
                     r"(?P<all> all( of)?( the| my)?)?( (?P<label>[a-z0-9 ]+?))? timers?( please)?")
_UP = re.compile(r"(turn (it|the volume|the sound) up|volume up|turn up the volume|(a (bit|little) )?louder|"
                 r"(increase|raise) the volume)( please)?")
_DOWN = re.compile(r"(turn (it|the volume|the sound) down|volume down|turn down the volume|"
                   r"(a (bit|little) )?(quieter|softer)|(decrease|lower) the volume)( please)?")
_SET_VOL = re.compile(r"(set |change |turn )?(the )?volume (to |at )?(?P<n>\w+)( percent)?")
_TIME = re.compile(r"(what time is it( now| right now)?|whats the time( now)?|what is the time|"
                   r"do you know what time it is|time)( please)?")
_DATE = re.compile(r"(whats|what is) (the date|todays date|today|the date today)|what day is (it|today)( today)?")
_WIFI = re.compile(r"((start|open|do|run) )?(the )?(wi ?fi|wireless) setup|set ?up( the| my| a)?( new)? (wi ?fi|wireless)"
                   r"( network)?|(connect|switch) to (a )?(new |different )?(wi ?fi|wireless)( network)?|"
                   r"change( the| my)? (wi ?fi|wireless)( network)?")
WIFI_SETUP = "Okay. Scan the code on my screen to set up Wi-Fi."
GOOD_NIGHT, GOOD_MORNING = "Good night.", "Good morning."
QUIET_ON = "Quiet mode on. I'll show a green light when something's ready."
QUIET_OFF = "Quiet mode off."
SOUND_OFF, SOUND_ON = "Sound off.", "Sound's back on."
_QUIET_ON = re.compile(r"(please )?(((turn|switch|put) on|enable|start|go (in)?to|use|enter|activate) (the )?"
                       r"(quiet|silent) mode|(quiet|silent) mode( on)?)( please)?")
_QUIET_OFF = re.compile(r"(please )?(((turn|switch) off|disable|stop|exit|leave|end|deactivate) (the )?"
                        r"(quiet|silent) mode|(quiet|silent) mode off)( please)?")
_MUTE_MIC = re.compile(r"(please )?(mute( yourself| your mic(rophone)?| the mic(rophone)?)?|stop listening( to me)?|"
                       r"turn (off (your |the )?mic(rophone)?|(your |the )?mic(rophone)? off))"
                       r"( please| for (a (bit|while|minute)|now))?")
_MUTE_SOUND = re.compile(r"(please )?(mute (the |your )?(sound|audio|volume|speaker|music)|"
                         r"turn (the )?(sound|volume) (all the way )?off|turn off the (sound|volume))( please)?")
_UNMUTE = re.compile(r"(please )?(unmute( yourself| the (sound|audio|volume|speaker|music))?|"
                     r"turn (the )?(sound|volume) (back )?on)( please)?")
_NIGHT = re.compile(r"(good ?night|night night|nighty night)( \w+)?|(im )?(going|heading|off) to (bed|sleep)( now)?")
_MORNING = re.compile(r"good morning( \w+)?|im (awake|up)( now)?")
SHUTTING_DOWN = "Shutting down. Unplug me once the screen goes dark."
RESTARTING = "Restarting. Give me a minute."
_SELF = r"( yourself| itself| tesseract| the (speaker|device))?"
_POWER_OFF = re.compile(r"(please )?(shut" + _SELF + r" (down|off)|shut down" + _SELF + r"|power" + _SELF +
                        r" (down|off)|turn (yourself|itself) off)( now)?( please)?")
_LOCAL_ON = re.compile(r"(please )?((switch|go|change) (over )?to|use|turn on)( the| your| my)? local( mode| model| ai)?"
                       r"( please)?|local mode( on)?")
_LOCAL_OFF = re.compile(r"(please )?((switch|go|change) (back |over )?to|use|turn on)( the)? cloud( mode| models?| ai)?"
                        r"( please)?|turn off local mode|local mode off")
# ── music (Spotify) ──
_SP = r"(?: (?:on|from|with|using) spotify)?"
_PLS = r"(?: please)?"
_M_PAUSE = re.compile(r"(please )?(pause|stop|halt)( the| this| playing)?( music| song| track| spotify| playback)?" + _PLS)
_M_RESUME = re.compile(r"(please )?(resume|unpause|continue|keep playing|play|start)( the| some)?"
                       r"( music| song| spotify| playback| playing)?( again)?" + _SP + _PLS)
_M_NEXT = re.compile(r"(please )?(next|skip)( this| the| to the next| ahead)?( song| track| one| it)?" + _PLS +
                     r"|play the next (song|track)")
_M_PREV = re.compile(r"(please )?(previous|last)( song| track)?" + _PLS + r"|play the (previous|last) (song|track)( again)?"
                     r"|go back (a|one|to the (previous|last)) (song|track)|(previous|last) (song|track) please")
_M_WHAT = re.compile(r"(what|whats|what is)( song| track)?( is)? (this|that|playing)( song| track)?( called)?"
                     r"|what (song|track) is (this|that|playing)|who (sings|is singing|sang|is this) (this|that|it)?( song)?"
                     r"|whos (singing|this)|name (this|that) (song|track)|what am i listening to")
_M_LIKE = re.compile(r"(i )?(like|love|save|heart)( this| the| that)( song| track| one)( to my (library|liked songs|likes|favorites))?"
                     r"|add (this|the|that) (song|track) to my (liked songs|library|likes|favorites)|like this|save this song")
_M_SHUFFLE = re.compile(r"(turn )?shuffle( mode)?( on| off)?|turn (on|off) shuffle( mode)?|(shuffle|mix) it up")
_M_LIKED = re.compile(r"(play|put on) (something from |some of )?my (liked songs|likes|favorites|favourites|music|library"
                      r"|saved songs|songs)" + _SP + _PLS)
_M_PLAYLIST = re.compile(r"(play|put on) (the |my )?playlist (called |named )?(?P<q>.+?)" + _SP + _PLS +
                         r"|(play|put on) (the |my )?(?P<q2>.+?) playlist" + _SP + _PLS)
_M_ALBUM = re.compile(r"(play|put on) (the )?album (called |named )?(?P<q>.+?)( by (?P<a>.+?))?" + _SP + _PLS +
                      r"|(play|put on) (the )?(?P<q2>.+?) album( by (?P<a2>.+?))?" + _SP + _PLS)
_M_ARTIST = re.compile(r"(play|put on) (some )?(songs|music|tracks|stuff|something) by (?P<a>.+?)" + _SP + _PLS)
_M_PLAY = re.compile(r"(can you |could you |please )?(play|put on|start playing|queue up)( me)?( the song| the track| some)? "
                     r"(?P<q>.+?)( by (?P<a>.+?))?" + _SP + _PLS)
_NOT_MUSIC = re.compile(r"\b(game|games|trivia|questions|rock paper|a joke|hide and seek|catch)\b")
_RESTART = re.compile(r"(please )?(restart|reboot)" + _SELF + r"( now)?( please)?")
_SCREEN = r"(screen|display)"
_SCREEN_UP = re.compile(rf"(make|turn) (the )?{_SCREEN} (brighter|up)( a bit| a little)?|brighten (the )?{_SCREEN}"
                        rf"( a bit| a little)?|(the )?{_SCREEN} (is )?too (dark|dim)")
_SCREEN_DOWN = re.compile(rf"(make|turn) (the )?{_SCREEN} (dimmer|darker|down)( a bit| a little)?|(dim|darken) "
                          rf"(the )?{_SCREEN}( a bit| a little)?|(the )?{_SCREEN} (is )?too bright")
_SCREEN_SET = re.compile(rf"(set |change |turn )?(the )?{_SCREEN}( brightness)? (to |at )?(?P<n>\w+)( percent)?"
                         rf"|turn (?P<onoff>on|off) (the )?{_SCREEN}")


def _number(words: list[str], i: int) -> tuple[float | None, int]:
    """Read a number at words[i]: '10', '1.5', 'twenty five', 'a'/'an' (=1). -> (value, next index)."""
    w = words[i]
    if re.fullmatch(r"\d+(\.\d+)?", w):
        return float(w), i + 1
    if w in _TENS:
        if i + 1 < len(words) and words[i + 1] in _ONES and 0 < _ONES[words[i + 1]] < 10:
            return _TENS[w] + _ONES[words[i + 1]], i + 2
        return _TENS[w], i + 1
    if w in _ONES:
        return _ONES[w], i + 1
    if w in ("a", "an"):
        return 1, i + 1
    return None, i


def parse_duration(words: list[str]) -> tuple[float, set[int]]:
    """Total seconds in e.g. 'an hour and a half', '2 and a half minutes', '1 hour 30 minutes',
    'half an hour', plus the indices of the words that made it up."""
    total, used, i, n = 0.0, set(), 0, len(words)
    while i < n:
        if words[i] == "half" and i + 2 < n and words[i + 1] in ("a", "an") and words[i + 2] in _UNITS:
            total += 0.5 * _UNITS[words[i + 2]]
            used.update(range(i, i + 3))
            i += 3
            continue
        num, j = _number(words, i)
        if num is not None:
            if words[j:j + 3] == ["and", "a", "half"]:
                num, j = num + 0.5, j + 3
            if j < n and words[j] in _UNITS:
                unit = _UNITS[words[j]]
                total += num * unit
                used.update(range(i, j + 1))
                i = j + 1
                if words[i:i + 3] == ["and", "a", "half"]:
                    total += 0.5 * unit
                    used.update(range(i, i + 3))
                    i += 3
                continue
        i += 1
    return total, used


def _words(norm: str) -> list[str]:
    return re.sub(r"(\d)([a-z])", r"\1 \2", norm).split()


def ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


@dataclass
class Reply:
    text: str            # what to say ("" = just a tick)
    cache: bool = True   # worth keeping in the TTS cache (fixed phrasing)
    then: Callable[[], None] | None = None   # run after it has been said


class LocalCommands:
    def __init__(self, cfg, alarms, hooks):
        """hooks: set_volume(int), volume() -> int, start_wifi_setup(), set_screen(int), screen() -> int,
        good_night(), good_morning(), power_off(restart=False), set_local(bool) -> str"""
        self.cfg, self.alarms, self.hooks = cfg, alarms, hooks

    def _local(self, norm: str) -> Reply | None:
        if not hasattr(self.hooks, "set_local"):
            return None
        if _LOCAL_ON.fullmatch(norm):
            return Reply(self.hooks.set_local(True))
        if _LOCAL_OFF.fullmatch(norm):
            return Reply(self.hooks.set_local(False))
        return None

    def _music(self, norm: str) -> Reply | None:
        """Spotify: play/pause/next/previous/what's playing/like/shuffle, and "play X [by Y]"."""
        sp = getattr(self.hooks, "spotify", None)
        if sp is None:
            if "spotify" in norm and _M_PLAY.fullmatch(norm):
                return Reply("Spotify isn't set up yet. Run tesseract spotify setup on me.")
            return None
        from .spotify import SpotifyError
        try:
            if _M_PAUSE.fullmatch(norm):
                return Reply(sp.pause())
            if _M_NEXT.fullmatch(norm):
                return Reply(sp.next())
            if _M_PREV.fullmatch(norm):
                return Reply(sp.previous())
            if _M_WHAT.fullmatch(norm):
                return Reply(sp.whats_playing(), cache=False)
            if _M_LIKE.fullmatch(norm):
                return Reply(sp.like(), cache=False)
            if m := _M_SHUFFLE.fullmatch(norm):
                return Reply(sp.shuffle(not norm.endswith("off") and " off " not in f" {norm} "))
            if _M_LIKED.fullmatch(norm):
                return Reply(sp.play("liked songs", "liked"))
            if _M_RESUME.fullmatch(norm):
                return Reply(sp.resume())
            if (m := _M_PLAYLIST.fullmatch(norm)) and not _NOT_MUSIC.search(norm):
                return Reply(sp.play(m.group("q") or m.group("q2"), "playlist"), cache=False)
            if (m := _M_ALBUM.fullmatch(norm)) and not _NOT_MUSIC.search(norm):
                return Reply(sp.play(m.group("q") or m.group("q2"), "album", m.group("a") or m.group("a2") or ""),
                             cache=False)
            if m := _M_ARTIST.fullmatch(norm):
                return Reply(sp.play(m.group("a"), "artist"), cache=False)
            if (m := _M_PLAY.fullmatch(norm)) and not _NOT_MUSIC.search(norm):
                return Reply(sp.play(m.group("q"), "", m.group("a") or ""), cache=False)
        except SpotifyError as e:
            return Reply(str(e), cache=False)
        return None

    def _power(self, norm: str) -> Reply | None:
        if not hasattr(self.hooks, "power_off"):
            return None
        if _POWER_OFF.fullmatch(norm):
            return Reply(SHUTTING_DOWN, then=self.hooks.power_off)
        if _RESTART.fullmatch(norm):
            return Reply(RESTARTING, then=lambda: self.hooks.power_off(restart=True))
        return None

    def handle(self, norm: str) -> Reply | None:
        """norm: normalize()d text (lowercase, no punctuation, wake word stripped)."""
        for fn in (self._set_timer, self._time_left, self._cancel_timer, self._mute, self._volume, self._clock,
                   self._wifi, self._night, self._quiet, self._screen, self._power, self._local, self._music):
            r = fn(norm)
            if r is not None:
                return r
        return None

    # ── timers ──────────────────────────────────────────────────────────────
    def _set_timer(self, norm: str) -> Reply | None:
        words = _words(norm)
        if not ({"timer", "countdown"} & set(words)) or _NOT_A_SET.search(norm):
            return None
        seconds, used = parse_duration(words)
        if not 1 <= seconds <= 24 * 3600:
            return None
        rest = [w for i, w in enumerate(words) if i not in used and w not in _FILLER]
        if len(rest) > 3:
            return None      # "...and remind me to flip the steak": let the model handle it
        label = " ".join(rest)
        self.alarms.add_timer(seconds, label)
        dur = spoken_duration(seconds)
        return Reply(f"{label.capitalize()} timer set for {dur}." if label else f"Timer set for {dur}.")

    def _time_left(self, norm: str) -> Reply | None:
        words = _words(norm)
        if not _LEFT.search(norm) or not ("timer" in words or _LEFT_BARE.fullmatch(norm)):
            return None
        timers = self.alarms.timers()
        named = [t for t in timers if t.label and t.label in norm]
        timers = named or timers
        if not timers:
            return Reply("You don't have a timer running.")
        parts = [f"{spoken_duration(max(0, t.due - time.time()))} left" + (f" on the {t.label} timer" if t.label else "")
                 for t in timers[:3]]
        return Reply(("; ".join(parts) + ".").capitalize(), cache=False)

    def _cancel_timer(self, norm: str) -> Reply | None:
        m = _CANCEL.fullmatch(norm)
        if not m:
            return None
        timers = self.alarms.timers()
        label = (m.group("label") or "").strip()
        if not timers:
            return Reply("Okay.")          # e.g. "stop the timer" right after it rang
        if label:
            hit = [t for t in timers if t.label and (t.label == label or label in t.label)]
            if not hit:
                return None
        elif m.group("all") or len(timers) == 1 or norm.endswith("timers"):
            hit = timers
        else:
            return None                    # several timers, none named: let the model ask
        for t in hit:
            self.alarms.cancel(t.id)
        if len(hit) > 1:
            return Reply(f"Cancelled {len(hit)} timers.")
        return Reply(f"{hit[0].label.capitalize()} timer cancelled." if hit[0].label else "Timer cancelled.")

    # ── mute (the mic, or the sound) and quiet mode ─────────────────────────
    def _mute(self, norm: str) -> Reply | None:
        if not hasattr(self.hooks, "mute_mic"):
            return None
        if _MUTE_SOUND.fullmatch(norm):
            return Reply(SOUND_OFF, then=lambda: self.hooks.mute_sound(True))
        if _MUTE_MIC.fullmatch(norm):
            return Reply("", then=self.hooks.mute_mic)   # no words: the mute sound says it (the button unmutes)
        if _UNMUTE.fullmatch(norm):
            return Reply(SOUND_ON if self.hooks.mute_sound(False) else "I'm not muted.")
        return None

    def _quiet(self, norm: str) -> Reply | None:
        if not hasattr(self.hooks, "set_quiet"):
            return None
        if _QUIET_ON.fullmatch(norm):
            return Reply(QUIET_ON, then=lambda: self.hooks.set_quiet(True))
        if _QUIET_OFF.fullmatch(norm):
            return Reply(QUIET_OFF, then=lambda: self.hooks.set_quiet(False))
        return None

    # ── volume ──────────────────────────────────────────────────────────────
    def _volume(self, norm: str) -> Reply | None:
        if _UP.fullmatch(norm):
            self.hooks.set_volume(self.hooks.volume() + 2)
            return Reply("")
        if _DOWN.fullmatch(norm):
            self.hooks.set_volume(self.hooks.volume() - 2)
            return Reply("")
        m = _SET_VOL.fullmatch(norm)
        if m:
            w = m.group("n")
            n = {"max": 10, "maximum": 10, "full": 10, "min": 1, "minimum": 1}.get(w)
            if n is None:
                v, _ = _number([w], 0)
                if v is None or w in ("a", "an"):
                    return None
                n = round(v / 10) if v > 10 else round(v)   # "volume 50 percent"
            self.hooks.set_volume(n)
            return Reply("")
        return None

    # ── clock ───────────────────────────────────────────────────────────────
    def _clock(self, norm: str) -> Reply | None:
        now = dt.datetime.now()
        if _TIME.fullmatch(norm):
            hhmm, ampm = format_clock(now, self.cfg.display.clock_24h)
            return Reply(f"It's {hhmm} {ampm}.".replace(" .", "."))
        if _DATE.fullmatch(norm):
            return Reply(f"It's {now:%A, %B} {ordinal(now.day)}.")
        return None

    # ── Wi-Fi ───────────────────────────────────────────────────────────────
    def _wifi(self, norm: str) -> Reply | None:
        if not _WIFI.fullmatch(norm) or not hasattr(self.hooks, "start_wifi_setup"):
            return None
        # said first (from the cache): once the setup hotspot is up there's no internet for speech
        return Reply(WIFI_SETUP, then=self.hooks.start_wifi_setup)

    # ── night mode and the screen ───────────────────────────────────────────
    def _night(self, norm: str) -> Reply | None:
        if not hasattr(self.hooks, "good_night"):
            return None
        if _NIGHT.fullmatch(norm):
            return Reply(GOOD_NIGHT, then=self.hooks.good_night)
        if _MORNING.fullmatch(norm):
            return Reply(GOOD_MORNING, then=self.hooks.good_morning)
        return None

    def _screen(self, norm: str) -> Reply | None:
        if not hasattr(self.hooks, "set_screen"):
            return None
        if _SCREEN_UP.fullmatch(norm):
            self.hooks.set_screen(max(1, self.hooks.screen()) + 3)
            return Reply("")
        if _SCREEN_DOWN.fullmatch(norm):
            self.hooks.set_screen(max(1, self.hooks.screen() - 3))
            return Reply("")
        m = _SCREEN_SET.fullmatch(norm)
        if m:
            if m.group("onoff"):
                level = 0 if m.group("onoff") == "off" else (self.hooks.screen() or 10)
            else:
                w = m.group("n")
                level = {"max": 10, "maximum": 10, "full": 10, "off": 0, "on": 10, "min": 1, "minimum": 1}.get(w)
                if level is None:
                    v, _ = _number([w], 0)
                    if v is None or w in ("a", "an"):
                        return None
                    level = round(v / 10) if v > 10 else round(v)   # "screen brightness 50 percent"
            self.hooks.set_screen(level)
            return Reply("")
        return None
