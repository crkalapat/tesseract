"""Weather from Open-Meteo (free, no API key, ~150 ms), so "what's the weather"
doesn't need a web search, which is several times slower.

Returns compact text for the model to phrase, e.g.
  Austin, Texas now: 72°F (feels 70°F), partly cloudy, wind 8 mph.
  Today: partly cloudy, high 80°F, low 61°F, 10% chance of rain. Tomorrow: ...
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

import httpx

log = logging.getLogger(__name__)

FORECAST = "https://api.open-meteo.com/v1/forecast"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"

# WMO weather interpretation codes
CODES = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
         51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
         61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
         71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
         81: "rain showers", 82: "heavy rain showers", 85: "snow showers", 86: "heavy snow showers",
         95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}


class Weather:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = httpx.Client(timeout=httpx.Timeout(6.0, connect=3.0))
        self._places: dict[str, dict] = {}
        self._lock = threading.Lock()

    def place(self, name: str) -> tuple[float, float, str]:
        """'Austin, Texas' -> (lat, lon, 'Austin, Texas'). Cached."""
        hit = self.lookup(name)
        return hit["lat"], hit["lon"], hit["label"]

    def lookup(self, name: str) -> dict:
        """'Austin, Texas' -> {lat, lon, label: 'Austin, Texas', timezone: 'America/Chicago'}. Cached."""
        key = name.strip().lower()
        with self._lock:
            if key in self._places:
                return self._places[key]
        city, _, region = (p.strip() for p in name.partition(","))
        r = self.client.get(GEOCODE, params={"name": city, "count": 10, "language": "en"})
        r.raise_for_status()
        results = r.json().get("results") or []
        if not results:
            raise LookupError(f"couldn't find a place called {name}")
        pick = results[0]
        if region:   # "Springfield, Illinois": prefer the result in that state/country
            for res in results:
                where = f"{res.get('admin1', '')} {res.get('country', '')} {res.get('country_code', '')}".lower()
                if region.lower() in where:
                    pick = res
                    break
        label = ", ".join(x for x in (pick.get("name"), pick.get("admin1") or pick.get("country")) if x)
        hit = {"lat": pick["latitude"], "lon": pick["longitude"], "label": label, "timezone": pick.get("timezone")}
        with self._lock:
            self._places[key] = hit
        return hit

    def report(self, location: str = "", days: int = 3) -> str:
        where = location.strip() or self.cfg.assistant.location.strip()
        if not where:
            return ("No location is set. Ask the user where they are; when they answer, call set_location "
                    "with it (that saves it), then answer their question. Or they can name a city. "
                    "Don't mention config files; "
                    "or to ask about a specific city.")
        lat, lon, label = self.place(where)
        imperial = self.cfg.assistant.units.lower().startswith("imp")
        t_unit, w_unit = ("°F", "mph") if imperial else ("°C", "km/h")
        r = self.client.get(FORECAST, params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": max(1, min(days, 7)),
            "temperature_unit": "fahrenheit" if imperial else "celsius",
            "wind_speed_unit": "mph" if imperial else "kmh",
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        })
        r.raise_for_status()
        return format_report(r.json(), label, t_unit, w_unit)


def format_report(j: dict, label: str, t_unit: str, w_unit: str) -> str:
    c = j.get("current", {})
    lines = [f"{label} now: {round(c.get('temperature_2m', 0))}{t_unit} "
             f"(feels {round(c.get('apparent_temperature', 0))}{t_unit}), "
             f"{CODES.get(c.get('weather_code'), 'unknown')}, wind {round(c.get('wind_speed_10m', 0))} {w_unit}."]
    d = j.get("daily", {})
    today = dt.date.today()
    for i, day in enumerate(d.get("time", [])):
        date = dt.date.fromisoformat(day)
        name = "Today" if date == today else "Tomorrow" if date == today + dt.timedelta(days=1) else f"{date:%A}"
        rain = (d.get("precipitation_probability_max") or [None] * (i + 1))[i]
        lines.append(f"{name}: {CODES.get(d['weather_code'][i], 'unknown')}, "
                     f"high {round(d['temperature_2m_max'][i])}{t_unit}, low {round(d['temperature_2m_min'][i])}{t_unit}"
                     + (f", {rain}% chance of rain." if rain is not None else "."))
    return " ".join(lines)
