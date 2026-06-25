"""Weather query tools powered by Open-Meteo (free, no API key required)."""
from __future__ import annotations

import logging
from typing import Any

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _get_coordinates(city: str) -> tuple[float, float] | None:
    """Resolve city name to (latitude, longitude) using Open-Meteo Geocoding API."""
    try:
        resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "zh", "format": "json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None
        return results[0]["latitude"], results[0]["longitude"]
    except Exception as exc:
        logger.warning("Geocoding failed for '%s': %s", city, exc)
        return None


def _format_weather(data: dict[str, Any], city: str) -> str:
    """Format Open-Meteo current weather response."""
    current = data.get("current", {})
    temp = current.get("temperature_2m", "N/A")
    unit = data.get("current_units", {}).get("temperature_2m", "°C")
    humidity = current.get("relative_humidity_2m", "N/A")
    wind = current.get("wind_speed_10m", "N/A")
    weather_code = current.get("weather_code", -1)

    # WMO Weather interpretation codes (simplified).
    weather_map = {
        0: "晴朗",
        1: "大部晴朗",
        2: "多云",
        3: "阴天",
        45: "雾",
        48: "雾凇",
        51: "毛毛雨",
        53: "中度毛毛雨",
        55: "强毛毛雨",
        61: "小雨",
        63: "中雨",
        65: "大雨",
        71: "小雪",
        73: "中雪",
        75: "大雪",
        95: "雷雨",
        96: "雷雨伴冰雹",
        99: "强雷雨伴冰雹",
    }
    desc = weather_map.get(weather_code, "未知天气")

    return (
        f"{city} 当前天气：{desc}\n"
        f"温度：{temp}{unit}\n"
        f"相对湿度：{humidity}%\n"
        f"风速：{wind} km/h"
    )


def create_weather_search_tool() -> Any:
    """Create the ``weather_search`` tool."""

    @tool
    def weather_search(city: str) -> str:
        """查询指定城市的当前天气。

        Args:
            city: 城市名称，例如 "北京"、"Shanghai"。
        """
        coords = _get_coordinates(city)
        if coords is None:
            return f"[error] 无法找到城市 '{city}' 的经纬度信息"

        lat, lon = coords
        try:
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                    "timezone": "auto",
                },
                timeout=30,
            )
            resp.raise_for_status()
            return _format_weather(resp.json(), city)
        except Exception as exc:
            logger.warning("Weather query failed for '%s': %s", city, exc)
            return f"[error] 查询 {city} 天气失败: {exc}"

    return weather_search


def build_weather_tools() -> list[Any]:
    """Return all weather tools."""
    return [create_weather_search_tool()]


# Module-level convenience instance.
weather_search = create_weather_search_tool()
