"""
actions/weather_report.py — the weather, as real numbers, for the model to say.

WHY IT FETCHES INSTEAD OF OPENING A SEARCH
    This tool used to open a web search in the browser and answer "Showing
    the weather for Istanbul". The browser opened behind the assistant, the
    model had no number at all, and it made the weather up - "18 °C, partly
    cloudy, no rain" while the streets were flooding (beta, 2026-10-14). A
    tool that answers a factual question must hand back the facts.

WHERE THE NUMBERS COME FROM
    MET Norway's forecast (open data, CC BY 4.0 - allowed in a paid product
    with its credit), the place from OpenStreetMap; both through
    core/cloud.forecast_days, which the live weather panel uses too, so what
    is said and what is shown are the same numbers.

NO CITY GIVEN
    "How is the weather?" means where the user is: the city they told the
    assistant (memory, identity.city). Without one, the tool asks.
    A weather panel opened without a city (plugins/_holo_desk_core) shows the
    city asked about last (last_city), then that one.
"""
from memory.config_manager import get_assistant_name


def _remembered_city() -> str:
    try:
        from memory.memory_manager import load_memory
        city = (load_memory().get("identity") or {}).get("city") or {}
        value = city.get("value") if isinstance(city, dict) else city
        return str(value or "").strip()        # nothing saved: "" (it was "None", a city it looked up)
    except Exception:
        return ""


def remember_asked(city: str) -> None:
    """The city the user last got the weather for (config weather_last_city)."""
    city = " ".join(str(city or "").split())[:120]
    if not city:
        return
    try:
        from memory.config_manager import load_api_keys, update_config
        if load_api_keys().get("weather_last_city") != city:
            update_config(weather_last_city=city)
    except Exception:
        pass


def last_city() -> str:
    """The city asked about last, else the user's own (_remembered_city)."""
    try:
        from memory.config_manager import load_api_keys
        city = str(load_api_keys().get("weather_last_city") or "").strip()
    except Exception:
        city = ""
    return city or _remembered_city()


def _mm(value) -> str:
    return "no rain" if not value else f"{value} mm rain"


def facts_for(city: str, when: str = "") -> str:
    """The weather as one block of facts for the model. Raises on failure."""
    from core import cloud
    fc = cloud.forecast_days(city)
    now, days = fc["now"], fc["daily"]
    lines = [
        f"Weather for {fc['place']}, from MET Norway.",
        f"NOW: {now['symbol'] or 'unknown'}, {now['temp_c']}°C (feels {now['feels_c']}°C), "
        f"humidity {now['humidity']}%, wind {now['wind_kmh']} km/h, "
        f"{_mm(now['rain_mm_next_hour'])} in the next hour.",
    ]
    if days:
        d = days[0]
        lines.append(f"TODAY: {d['symbol']}, {d['min_c']}..{d['max_c']}°C, {_mm(d['rain_mm'])}"
                     + (f" ({d['rain_pct']}% chance)" if d['rain_pct'] is not None else "")
                     + (f", sunrise {d['sunrise']}, sunset {d['sunset']}" if d['sunrise'] else "") + ".")
        lines.append("NEXT DAYS: " + "; ".join(
            f"{x['date']}: {x['symbol']}, {x['min_c']}..{x['max_c']}°C, {_mm(x['rain_mm'])}"
            for x in days[1:]) + ".")
    lines.append(f"The user asked about: {when or 'now'}. Say it briefly in the user's language "
                 f"and in the units they use, from these numbers only - never add weather that "
                 f"is not here. Symbols are MET's names (heavyrain = heavy rain). "
                 f"Credit if asked: {fc['source']}.")
    # What the report does NOT hold, said by the report itself: having sunrise
    # in it, a question about a time that only depends on the sun was answered
    # "around 05:50, about 1.5 hours before sunrise" or "I don't have that",
    # 5 of 5, when a web search had the exact time (bench 2026-10-25).
    lines.append("This report holds the weather and the sun's rise and set, nothing else. If the "
                 "user asked about anything else, it is not here: get it with web_search, and "
                 "never estimate it from these numbers.")
    return "\n".join(lines)
    return "\n".join(lines)


def weather_action(parameters: dict, player=None, session_memory=None) -> str:
    from core import outcome
    city = str((parameters or {}).get("city") or "").strip() or _remembered_city()
    when = str((parameters or {}).get("time") or "").strip()
    if not city:
        return outcome.ask("weather_report", "which city the user wants the weather for")
    try:
        text = facts_for(city, when)
    except Exception as e:
        reason = str(e) or type(e).__name__
        _log(f"The weather for {city} could not be fetched: {reason}", player)
        return outcome.failed("weather_report",
                              f"the weather for {city} could not be fetched ({reason}); "
                              f"say so - do not guess the weather")
    _log(f"Weather for {city} fetched.", player)
    remember_asked(city)
    if session_memory:
        try:
            session_memory.set_last_search(query=f"weather {city} {when}".strip(), response=text)
        except Exception:
            pass
    return text


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"{get_assistant_name()}: {message}")
        except Exception:
            pass


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "weather_report",
    "description": (
        "The real weather - now, today and the next days - for a city, from a weather "
        "service. Call it for ANY question about weather, temperature, rain, wind or the "
        "forecast, and answer only from what it returns; never state the weather without "
        "it. Without a city it uses the city the user lives in, if known. It holds the "
        "weather and the sun's rise and set only: any other time or schedule of a place "
        "or a day is web_search."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "city": {"type": "STRING", "description": "City name, in any language (optional)"},
            "time": {"type": "STRING",
                     "description": "What the user asked about: 'now', 'today', 'tomorrow', 'weekend'"},
        },
        "required": [],
    },
    "handler": weather_action,
}
