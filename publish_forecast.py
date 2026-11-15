import gzip
import json
import os
from calendar import isleap
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from math import acos, cos, degrees, radians, sin, tau
from pathlib import Path
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LATITUDE, LONGITUDE = 13.7563, 100.5018
BANGKOK = timezone(timedelta(hours=7))
FORECAST_URL = (
    f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}"
    "&hourly=weather_code,temperature_2m,apparent_temperature&daily=sunrise,sunset&forecast_days=3"
    "&forecast_hours=48&timezone=Asia%2FBangkok&timeformat=unixtime"
)
MET_URL = (
    "https://api.met.no/weatherapi/locationforecast/2.0/complete"
    f"?lat={LATITUDE}&lon={LONGITUDE}"
)
MET_CACHE = Path(".forecast-cache/met-norway.json")
OPEN_METEO_CREDIT = '<a href="https://open-meteo.com/">Open-Meteo</a>'
MET_CREDIT = (
    '<a href="https://www.met.no/en">MET Norway</a> · '
    '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a> · '
    'Weather categories adapted for Sala. '
    '<a href="https://gml.noaa.gov/grad/solcalc/solareqns.PDF">Sun times calculated locally</a>.'
)


def forecast_payload(data, now):
    hourly = data["hourly"]
    times = hourly["time"]
    codes = hourly["weather_code"]
    if not 24 <= len(times) <= 48 or len(codes) != len(times):
        raise ValueError("Expected 24 to 48 hourly forecast entries")
    if any(type(t) is not int or t != times[0] + i * 3600 for i, t in enumerate(times)):
        raise ValueError("Forecast hours are not consecutive")
    current_hour = int(now.timestamp()) // 3600 * 3600
    if times[0] > current_hour or times[-1] < current_hour + 23 * 3600:
        raise ValueError("Forecast does not cover the current hour and the next 23 hours")
    if any(type(code) is not int or code not in (0, 1, 2, 3, 45, 48)
           and not 51 <= code <= 99 for code in codes):
        raise ValueError("Invalid weather code")
    daily = data["daily"]
    dates = daily["time"]
    rises = daily["sunrise"]
    sets = daily["sunset"]
    if len(dates) != 3 or len(rises) != 3 or len(sets) != 3:
        raise ValueError("Expected three days of sunrise and sunset data")
    for i, (day, rise, sunset) in enumerate(zip(dates, rises, sets)):
        if day != dates[0] + i * 86400 or not day < rise < sunset < day + 86400:
            raise ValueError("Invalid Bangkok solar schedule")
    if int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()) != dates[0]:
        raise ValueError("Solar schedule does not start today in Bangkok")
    published_hourly = {"time": times, "weather_code": codes}
    for field in ("temperature_2m", "apparent_temperature"):
        temperatures = hourly[field]
        if len(temperatures) != len(times) or any(
            type(value) not in (int, float) or not -50 <= value <= 65 for value in temperatures
        ):
            raise ValueError(f"Missing or invalid {field}")
        published_hourly[field] = temperatures
    return {"hourly": published_hourly, "daily": daily}


def solar_schedule(now):
    # NOAA fractional-year equations, using Bangkok local noon for each date.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    latitude = radians(LATITUDE)
    days, rises, sets = [], [], []
    for offset in range(3):
        day = midnight + timedelta(days=offset)
        year_angle = tau * (day.timetuple().tm_yday - 1) / (365 + isleap(day.year))
        equation = 229.18 * (
            0.000075 + 0.001868 * cos(year_angle) - 0.032077 * sin(year_angle)
            - 0.014615 * cos(2 * year_angle) - 0.040849 * sin(2 * year_angle)
        )
        declination = (
            0.006918 - 0.399912 * cos(year_angle) + 0.070257 * sin(year_angle)
            - 0.006758 * cos(2 * year_angle) + 0.000907 * sin(2 * year_angle)
            - 0.002697 * cos(3 * year_angle) + 0.00148 * sin(3 * year_angle)
        )
        hour_angle = acos(
            (cos(radians(90.833)) - sin(latitude) * sin(declination))
            / (cos(latitude) * cos(declination))
        )
        noon = 720 - 4 * LONGITUDE - equation + 7 * 60
        half_day = 4 * degrees(hour_angle)
        stamp = int(day.timestamp())
        days.append(stamp)
        rises.append(stamp + round(60 * (noon - half_day)))
        sets.append(stamp + round(60 * (noon + half_day)))
    return {"time": days, "sunrise": rises, "sunset": sets}


def met_weather_code(symbol):
    condition = symbol.split("_")[0]
    if any(word in condition for word in ("rain", "sleet", "snow", "thunder")):
        return 61
    return {"clearsky": 0, "fair": 1, "partlycloudy": 2, "cloudy": 3, "fog": 45}[condition]


def met_forecast(data, now):
    start = int(now.timestamp()) // 3600 * 3600
    hourly = {"time": [], "weather_code": [], "temperature_2m": [], "apparent_temperature": []}
    for row in data["properties"]["timeseries"]:
        stamp = int(datetime.fromisoformat(row["time"]).timestamp())
        if stamp < start:
            continue
        if stamp != start + len(hourly["time"]) * 3600 or len(hourly["time"]) == 48:
            break
        values = row["data"]
        if "next_1_hours" not in values:
            break
        conditions = values["next_1_hours"]["summary"]["symbol_code"]
        readings = values["instant"]["details"]
        hourly["time"].append(stamp)
        hourly["weather_code"].append(met_weather_code(conditions))
        hourly["temperature_2m"].append(readings["air_temperature"])
        hourly["apparent_temperature"].append(readings["apparent_air_temperature"])
    return {"hourly": hourly, "daily": solar_schedule(now)}


def download_met_forecast(now):
    cached = json.loads(MET_CACHE.read_text(encoding="utf-8")) if MET_CACHE.exists() else {}
    if now.timestamp() < cached.get("retry_at", 0):
        return cached["data"]
    headers = {
        "User-Agent": "SalaWeather/1.0 (+https://github.com/pob2527/sala-weather)",
        "Accept-Encoding": "gzip",
    }
    if cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]
    request = Request(MET_URL, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            data = json.loads(raw)
            response_headers = response.headers
    except HTTPError as error:
        if error.code != 304 or "data" not in cached:
            raise
        data = cached["data"]
        response_headers = error.headers
    expires = response_headers.get("Expires")
    retry_at = now.timestamp() + 3 * 3600
    if expires:
        retry_at = max(retry_at, parsedate_to_datetime(expires).timestamp())
    MET_CACHE.parent.mkdir(exist_ok=True)
    MET_CACHE.write_text(json.dumps({
        "data": data,
        "last_modified": response_headers.get("Last-Modified", cached.get("last_modified")),
        "retry_at": retry_at,
    }), encoding="utf-8")
    return data


def download_forecast(now):
    for attempt in (1, 2):
        try:
            with urlopen(FORECAST_URL, timeout=20) as response:
                return forecast_payload(json.load(response), now), "Open-Meteo", OPEN_METEO_CREDIT
        except (OSError, HTTPException, ValueError, KeyError, TypeError) as error:
            print(f"Open-Meteo attempt {attempt} failed: {error}", flush=True)
            reason = error.reason if isinstance(error, URLError) else error
            retryable = (
                isinstance(reason, (TimeoutError, ConnectionError, HTTPException))
                or isinstance(error, HTTPError) and error.code in (500, 502, 503, 504)
            )
            if attempt == 1 and retryable:
                print("Retrying Open-Meteo in 60 seconds.", flush=True)
                sleep(60)
                continue
            if os.environ.get("SALA_ENABLE_MET_NORWAY") != "true":
                raise
            break
    print("Using MET Norway for the Bangkok forecast.", flush=True)
    data = met_forecast(download_met_forecast(now), now)
    return forecast_payload(data, now), "MET Norway", MET_CREDIT


def main():
    payload, source, credit = download_forecast(datetime.now(BANGKOK))
    payload["source"] = source
    output = Path("public")
    output.mkdir(exist_ok=True)
    (output / "weather.json").write_text(
        json.dumps(payload, separators=(",", ":")), encoding="utf-8"
    )
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Sala weather</title>'
        '<a href="weather.json">Bangkok forecast JSON</a> · '
        f'Weather data by {credit}',
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
