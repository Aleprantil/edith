"""
Meteo da Open-Meteo (gratuito, senza chiave né registrazione).
Una sola richiesta ogni 30 minuti al massimo: il risultato resta in cache.
"""
import logging
import threading
import time

import requests

from config import WEATHER_ENABLED, WEATHER_LAT, WEATHER_LON

logger = logging.getLogger(__name__)

URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 10
CACHE_TTL = 1800

_session = requests.Session()
_lock = threading.Lock()
_cache = {"ts": 0.0, "data": None}


class WeatherError(Exception):
    """Meteo non disponibile."""


# Codici WMO usati da Open-Meteo -> (descrizione, emoji)
_CODES = {
    0: ("sereno", "☀️"), 1: ("prevalentemente sereno", "🌤️"), 2: ("parzialmente nuvoloso", "⛅"), 3: ("coperto", "☁️"),
    45: ("nebbia", "🌫️"), 48: ("nebbia con brina", "🌫️"),
    51: ("pioviggine leggera", "🌦️"), 53: ("pioviggine", "🌦️"), 55: ("pioviggine intensa", "🌦️"),
    56: ("pioviggine gelata", "🌧️"), 57: ("pioviggine gelata", "🌧️"),
    61: ("pioggia debole", "🌦️"), 63: ("pioggia", "🌧️"), 65: ("pioggia forte", "🌧️"),
    66: ("pioggia gelata", "🌧️"), 67: ("pioggia gelata", "🌧️"),
    71: ("neve debole", "🌨️"), 73: ("neve", "❄️"), 75: ("neve forte", "❄️"), 77: ("nevischio", "🌨️"),
    80: ("rovesci deboli", "🌦️"), 81: ("rovesci", "🌧️"), 82: ("rovesci violenti", "⛈️"),
    85: ("rovesci di neve", "🌨️"), 86: ("forti rovesci di neve", "❄️"),
    95: ("temporale", "⛈️"), 96: ("temporale con grandine", "⛈️"), 99: ("temporale con grandine", "⛈️"),
}


def _hhmm(iso):
    """«2026-10-07T07:12» -> «07:12» (None se manca o non è leggibile)."""
    try:
        return str(iso)[11:16] if iso and len(str(iso)) >= 16 and str(iso)[13] == ":" else None
    except Exception:
        return None


def describe(code) -> tuple:
    try:
        return _CODES.get(int(code), ("tempo variabile", "🌡️"))
    except (TypeError, ValueError):
        return ("tempo variabile", "🌡️")


def get_forecast(force: bool = False) -> list:
    """
    Previsioni di oggi e dei due giorni dopo: [{"date","tmin","tmax","pop","precip","label","emoji","sunrise","sunset"}, ...]
    "sunrise"/"sunset" = alba e tramonto in "HH:MM" (None se il servizio non li dà).
    "pop" = probabilità massima di precipitazioni in % (può essere None). Solleva WeatherError.
    """
    if not WEATHER_ENABLED:
        raise WeatherError("meteo disattivato (WEATHER_ENABLED=0)")

    with _lock:
        if not force and _cache["data"] and time.time() - _cache["ts"] < CACHE_TTL:
            return _cache["data"]

        try:
            r = _session.get(URL, timeout=HTTP_TIMEOUT, params={
                "latitude": WEATHER_LAT,
                "longitude": WEATHER_LON,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,sunrise,sunset",
                "timezone": "Europe/Rome",
                "forecast_days": 3,
            })
        except requests.RequestException as e:
            raise WeatherError(f"servizio meteo non raggiungibile ({e.__class__.__name__})") from e
        if r.status_code != 200:
            raise WeatherError(f"servizio meteo: HTTP {r.status_code}")

        try:
            d = r.json()["daily"]
            days = []
            for i, day in enumerate(d["time"]):
                label, emoji = describe(d["weather_code"][i])
                pop = d.get("precipitation_probability_max", [None] * len(d["time"]))[i]
                precip = d.get("precipitation_sum", [0] * len(d["time"]))[i]
                days.append({
                    "sunrise": _hhmm(d.get("sunrise", [None] * len(d["time"]))[i]),
                    "sunset": _hhmm(d.get("sunset", [None] * len(d["time"]))[i]),
                    "date": day,
                    "tmin": round(d["temperature_2m_min"][i]),
                    "tmax": round(d["temperature_2m_max"][i]),
                    "pop": None if pop is None else int(pop),
                    "precip": float(precip or 0),
                    "label": label,
                    "emoji": emoji,
                })
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise WeatherError("risposta del servizio meteo non valida") from e

        _cache["ts"], _cache["data"] = time.time(), days
        return days
