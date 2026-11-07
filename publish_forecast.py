import json
from pathlib import Path
from urllib.request import urlopen

FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude=13.7563&longitude=100.5018"
    "&hourly=weather_code,apparent_temperature&daily=sunrise,sunset&forecast_days=3"
    "&forecast_hours=48&timezone=Asia%2FBangkok&timeformat=unixtime"
)


def forecast_payload(data):
    hourly = data["hourly"]
    times = hourly["time"]
    codes = hourly["weather_code"]
    if len(times) != 48 or len(codes) != 48:
        raise ValueError("Expected 48 hourly forecast entries")
    if any(t != times[0] + i * 3600 for i, t in enumerate(times)):
        raise ValueError("Forecast hours are not consecutive")
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
    published_hourly = {"time": times, "weather_code": codes}
    temperatures = hourly.get("apparent_temperature")
    if isinstance(temperatures, list) and len(temperatures) == len(times):
        published_hourly["apparent_temperature"] = [
            value if type(value) in (int, float) and -50 <= value <= 65 else None
            for value in temperatures
        ]
    return {"hourly": published_hourly, "daily": daily}


def main():
    with urlopen(FORECAST_URL, timeout=20) as response:
        payload = forecast_payload(json.load(response))
    output = Path("public")
    output.mkdir(exist_ok=True)
    (output / "weather.json").write_text(
        json.dumps(payload, separators=(",", ":")), encoding="utf-8"
    )
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Sala weather</title>'
        '<a href="weather.json">Bangkok forecast JSON</a> · '
        '<a href="https://open-meteo.com/">Weather data by Open-Meteo</a>',
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
