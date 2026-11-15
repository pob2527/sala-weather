import gzip
import json
import os
from calendar import isleap
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from io import BytesIO
from math import acos, cos, degrees, exp, radians, sin, tau
from pathlib import Path
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

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
DWD_URL = (
    "https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/"
    "single_stations/48455/kml/MOSMIX_L_LATEST_48455.kmz"
)
PUBLIC_URL = "https://pob2527.github.io/sala-weather/weather.json"
OPEN_METEO_CREDIT = '<a href="https://open-meteo.com/">Open-Meteo</a>'
MET_CREDIT = (
    '<a href="https://www.met.no/en">MET Norway</a> · '
    '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a> · '
    'Weather categories adapted for Sala. '
    '<a href="https://gml.noaa.gov/grad/solcalc/solareqns.PDF">Sun times calculated locally</a>.'
)
DWD_CREDIT = (
    '<a href="https://opendata.dwd.de/weather/local_forecasts/mos/">Deutscher Wetterdienst (DWD)</a> · '
    'Bangkok station 48455. Weather, feels-like and sunrise/sunset adapted for Sala.'
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


def download_met_forecast():
    cached = json.loads(MET_CACHE.read_text(encoding="utf-8")) if MET_CACHE.exists() else {}
    requested_at = datetime.now(timezone.utc).timestamp()
    if requested_at < cached.get("retry_at", 0):
        if "data" in cached:
            print("Using the cached MET Norway response.", flush=True)
            return cached["data"]
        raise ValueError("MET Norway request interval has not elapsed")
    # Keep the request interval across scheduled runs even when the network call fails.
    retry_at = requested_at + 3 * 3600
    MET_CACHE.parent.mkdir(exist_ok=True)
    cached["retry_at"] = retry_at
    MET_CACHE.write_text(json.dumps(cached), encoding="utf-8")
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
            if error.code in (429, 503) and error.headers.get("Retry-After"):
                delay = error.headers["Retry-After"]
                eligible = requested_at + int(delay) if delay.isdecimal() else parsedate_to_datetime(delay).timestamp()
                cached["retry_at"] = max(retry_at, eligible)
                MET_CACHE.write_text(json.dumps(cached), encoding="utf-8")
            raise
        data = cached["data"]
        response_headers = error.headers
    expires = response_headers.get("Expires")
    if expires:
        retry_at = max(retry_at, parsedate_to_datetime(expires).timestamp())
    MET_CACHE.write_text(json.dumps({
        "data": data,
        "last_modified": response_headers.get("Last-Modified", cached.get("last_modified")),
        "retry_at": retry_at,
    }), encoding="utf-8")
    return data


def dwd_forecast(archive, now):
    kml = {"kml": "http://www.opengis.net/kml/2.2",
           "dwd": "https://opendata.dwd.de/weather/lib/pointforecast_dwd_extension_V1_0.xsd"}
    with ZipFile(BytesIO(archive)) as package:
        entry, = package.infolist()
        if not entry.filename.endswith("_48455.kml") or entry.file_size > 2_000_000:
            raise ValueError("Unexpected DWD Bangkok station archive")
        root = ElementTree.fromstring(package.read(entry))
    issue = datetime.fromisoformat(root.findtext(".//dwd:IssueTime", namespaces=kml))
    if not timedelta(0) <= now - issue < timedelta(hours=12):
        raise ValueError("DWD Bangkok forecast is stale")
    station = root.find(".//kml:Placemark", kml)
    if station.findtext("kml:name", namespaces=kml) != "48455":
        raise ValueError("Unexpected DWD station")
    times = [
        int(datetime.fromisoformat(node.text).timestamp())
        for node in root.findall(".//dwd:ForecastTimeSteps/dwd:TimeStep", kml)
    ]
    name_attribute = "{" + kml["dwd"] + "}elementName"
    fields = {
        node.attrib[name_attribute]: node.findtext("dwd:value", namespaces=kml).split()
        for node in station.findall(".//dwd:Forecast", kml)
        if node.attrib[name_attribute] in ("TTT", "Td", "FF", "N", "RR1c")
    }
    start = times.index(int(now.timestamp()) // 3600 * 3600)
    end = min(start + 48, len(times) - 1, len(fields["RR1c"]) - 1)
    hourly = {"time": times[start:end], "weather_code": [], "temperature_2m": [], "apparent_temperature": []}
    for i in range(start, end):
        temperature = float(fields["TTT"][i]) - 273.15
        dewpoint = float(fields["Td"][i]) - 273.15
        wind = float(fields["FF"][i])
        cloud = float(fields["N"][i])
        # DWD stamps accumulated rain at the end of the hour.
        rain = float(fields["RR1c"][i + 1])
        if not (-60 <= dewpoint <= temperature + 1 and 0 <= wind <= 100
                and 0 <= cloud <= 100 and 0 <= rain <= 200):
            raise ValueError("Invalid DWD weather reading")
        # Steadman shade apparent temperature, using vapour pressure from dewpoint.
        vapour_pressure = 6.105 * exp(17.27 * dewpoint / (237.7 + dewpoint))
        hourly["temperature_2m"].append(round(temperature, 1))
        hourly["apparent_temperature"].append(round(temperature + 0.33 * vapour_pressure - 0.70 * wind - 4, 1))
        hourly["weather_code"].append(61 if rain >= 0.1 else 2 if cloud >= 50 else 0)
    return {"hourly": hourly, "daily": solar_schedule(now)}


def download_dwd_forecast(now):
    with urlopen(DWD_URL, timeout=20) as response:
        archive = response.read(256_001)
    if len(archive) > 256_000:
        raise ValueError("DWD Bangkok station archive is too large")
    return forecast_payload(dwd_forecast(archive, now), now)


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
    print("Trying MET Norway for the Bangkok forecast.", flush=True)
    try:
        data = met_forecast(download_met_forecast(), now)
        return forecast_payload(data, now), "MET Norway", MET_CREDIT
    except (OSError, HTTPException, ValueError, KeyError, TypeError, EOFError) as error:
        print(f"MET Norway unavailable: {error}", flush=True)
    print("Trying DWD Bangkok station 48455.", flush=True)
    try:
        return download_dwd_forecast(now), "Deutscher Wetterdienst (DWD)", DWD_CREDIT
    except (BadZipFile, ElementTree.ParseError, OSError, HTTPException, ValueError, KeyError,
            TypeError, AttributeError, IndexError) as error:
        raise RuntimeError("All three Bangkok forecast sources failed") from error


def regular_run_published(now):
    # Retry slots follow the regular runs at 03:17, 09:17, 15:17, and 21:17 UTC.
    anchor = 3 * 3600 + 17 * 60
    slot = (int(now.timestamp()) - anchor) // (6 * 3600) * (6 * 3600) + anchor
    try:
        with urlopen(f"{PUBLIC_URL}?retry_slot={slot}", timeout=10) as response:
            published = json.load(response)
        times = published["hourly"]["time"]
        return (
            slot <= published["published_at"] <= now.timestamp()
            and times[0] <= now.timestamp() < times[-1] + 3600
        )
    except (OSError, HTTPException, ValueError, KeyError, TypeError, IndexError):
        return False


def main():
    now = datetime.now(BANGKOK)
    if os.environ.get("SALA_RETRY_RUN") == "true" and regular_run_published(now):
        print("The last scheduled slot already has a valid published forecast.", flush=True)
        return
    payload, source, credit = download_forecast(now)
    payload["source"] = source
    payload["published_at"] = int(now.timestamp())
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
