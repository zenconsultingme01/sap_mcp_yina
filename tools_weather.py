import requests

from tool import tool


# ── Open Meteo 날씨 도구 ──
# WMO Weather Code 설명은 https://open-meteo.com/en/docs 참고


def _geocode(city: str, country_code: str = "") -> dict:
    params = {"name": city, "count": 5, "language": "ko", "format": "json"}
    if country_code:
        params["country_code"] = country_code.upper()

    resp = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search", params=params, timeout=10
    )
    resp.raise_for_status()
    
    data = resp.json()
    results = data.get("results", [])
    if not results:
        return {"error": f"도시를 찾을 수 없습니다: {city}"}    

    return {
        "results": [
            {
                "name": r.get("name"),
                "country": r.get("country"),
                "admin1": r.get("admin1"),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "timezone": r.get("timezone"),
            }
            for r in results
        ]
    }


def _fetch_weather(latitude: float, longitude: float, days: int) -> dict:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
        "forecast_days": days,
        "timezone": "auto",
    }
    resp = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _build_weather_response(data: dict) -> dict:
    current = data.get("current", {})
    daily = data.get("daily", {})

    forecast = [
        {
            "date": date,
            "weather_code": daily["weather_code"][i],
            "temperature_max": daily["temperature_2m_max"][i],
            "temperature_min": daily["temperature_2m_min"][i],
            "precipitation_sum": daily["precipitation_sum"][i],
            "precipitation_probability_max": daily["precipitation_probability_max"][i],
        }
        for i, date in enumerate(daily.get("time", []))
    ]

    return {
        "current": {
            "time": current.get("time"),
            "temperature": current.get("temperature_2m"),
            "humidity": current.get("relative_humidity_2m"),
            "weather_code": current.get("weather_code"),
            "wind_speed": current.get("wind_speed_10m"),
        },
        "daily_forecast": forecast,
        "units": {"temperature": "°C", "wind_speed": "km/h", "precipitation": "mm"},
    }


@tool
def geocode_city(city: str, country_code: str = "") -> dict:
    """도시명으로 위도/경도를 조회합니다. country_code는 ISO 3166-1 alpha-2 (예: KR, US)."""
    return _geocode(city, country_code)


@tool
def get_weather(latitude: float, longitude: float, days: int = 3) -> dict:
    """위도/경도로 현재 날씨와 예보를 조회합니다.
    days는 1~16일 범위.
    """
    if not (1 <= days <= 16):
        return {"error": "days는 1~16 사이여야 합니다."}

    data = _fetch_weather(latitude, longitude, days)
    return _build_weather_response(data)

@tool
def check_heatwave(dates: str, max_temperatures: str) -> dict:
    """날짜별 최고기온에서 30도 이상인 날을 찾습니다."""
    date_list = [d.strip() for d in dates.split(",")]
    temp_list = [float(t.strip()) for t in max_temperatures.split(",")]
    alerts = [
        {"date": date_list[i], "temp": temp_list[i]}
        for i in range(len(date_list))
        if temp_list[i] >= 30
    ]
    return {"alert": len(alerts) > 0, "dates": alerts}

@tool
def check_rain(dates: str, probabilities: str) -> dict:
    """날짜별 강수확률에서 60% 이상인 날을 찾습니다."""
    date_list = [d.strip() for d in dates.split(",")]
    prob_list = [float(p.strip()) for p in probabilities.split(",")]
    rainy = [
        {"date": date_list[i], "probability": prob_list[i]}
        for i in range(len(date_list))
        if prob_list[i] >= 60
    ]
    return {"alert": len(rainy) > 0, "dates": rainy}