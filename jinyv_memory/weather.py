# -*- coding: utf-8 -*-
"""天气：Open-Meteo（免 key、无日限流）+ 城市定位。

定位优先级：
  1. 聊天里改过的城市（weather_city.json，随时可换）
  2. 环境变量 RP_WEATHER_CITY
  3. 服务器公网 IP 兜底（只能猜到机房所在城市）

主人在大连不代表以后还在大连，所以城市必须能随时改：
模型听主人说「我在成都」时调 weather 带 city，并可以 set_default=true 记下来，
之后自主醒来看到的天气就换成成都，不用改配置也不用重启。

环境变量：
  RP_WEATHER       1/0 开关，默认 1
  RP_WEATHER_CITY  默认城市，如 大连
  RP_WEATHER_IP    1/0 城市未知时是否用服务器 IP 猜，默认 0
                   （机房城市多半不是主人所在地，猜错比不说更糟）
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("rp_memory")

GEO = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
UA = "Mozilla/5.0 (compatible; jinyu-weather/1.0)"

CODE_CN = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "毛毛雨", 53: "小雨", 55: "细雨", 56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴冰雹",
}

_path: Optional[Path] = None
_state: dict = {}
_WEATHER_TTL = 600            # 天气缓存 10 分钟
_GEO_TTL = 30 * 24 * 3600     # 城市坐标基本不变
_IP_TTL = 24 * 3600
_MIN_POP = 200000             # 低于这个人口的「同名村庄」不可信，宁可不答

# Open-Meteo 的中文地名库很不准（青岛会指到辽宁的同名村、长沙指到重庆），
# 所以常见城市先用拉丁名查，查回来的名字和省份才是对的。
_CITY_ALIAS = {
    "北京": "Beijing", "上海": "Shanghai", "天津": "Tianjin", "重庆": "Chongqing",
    "广州": "Guangzhou", "深圳": "Shenzhen", "杭州": "Hangzhou", "南京": "Nanjing",
    "武汉": "Wuhan", "成都": "Chengdu", "西安": "Xi'an", "苏州": "Suzhou",
    "郑州": "Zhengzhou", "长沙": "Changsha", "沈阳": "Shenyang", "青岛": "Qingdao",
    "大连": "Dalian", "厦门": "Xiamen", "福州": "Fuzhou", "济南": "Jinan",
    "合肥": "Hefei", "昆明": "Kunming", "哈尔滨": "Harbin", "长春": "Changchun",
    "石家庄": "Shijiazhuang", "太原": "Taiyuan", "南昌": "Nanchang", "南宁": "Nanning",
    "贵阳": "Guiyang", "兰州": "Lanzhou", "西宁": "Xining", "银川": "Yinchuan",
    "乌鲁木齐": "Urumqi", "呼和浩特": "Hohhot", "拉萨": "Lhasa", "海口": "Haikou",
    "三亚": "Sanya", "宁波": "Ningbo", "无锡": "Wuxi", "东莞": "Dongguan",
    "佛山": "Foshan", "珠海": "Zhuhai", "中山": "Zhongshan", "惠州": "Huizhou",
    "温州": "Wenzhou", "常州": "Changzhou", "徐州": "Xuzhou", "洛阳": "Luoyang",
    "烟台": "Yantai", "威海": "Weihai", "唐山": "Tangshan", "保定": "Baoding",
    "廊坊": "Langfang", "秦皇岛": "Qinhuangdao", "邯郸": "Handan", "包头": "Baotou",
    "大同": "Datong", "鞍山": "Anshan", "抚顺": "Fushun", "吉林": "Jilin",
    "齐齐哈尔": "Qiqihar", "大庆": "Daqing", "扬州": "Yangzhou", "南通": "Nantong",
    "盐城": "Yancheng", "镇江": "Zhenjiang", "湖州": "Huzhou", "嘉兴": "Jiaxing",
    "绍兴": "Shaoxing", "金华": "Jinhua", "台州": "Taizhou", "泰州": "Taizhou",
    "泉州": "Quanzhou", "漳州": "Zhangzhou", "赣州": "Ganzhou", "九江": "Jiujiang",
    "淄博": "Zibo", "潍坊": "Weifang", "临沂": "Linyi", "济宁": "Jining",
    "泰安": "Tai'an", "新乡": "Xinxiang", "南阳": "Nanyang", "宜昌": "Yichang",
    "襄阳": "Xiangyang", "株洲": "Zhuzhou", "湘潭": "Xiangtan", "衡阳": "Hengyang",
    "桂林": "Guilin", "柳州": "Liuzhou", "遵义": "Zunyi", "绵阳": "Mianyang",
    "德阳": "Deyang", "大理": "Dali", "丽江": "Lijiang", "香港": "Hong Kong",
    "澳门": "Macau", "台北": "Taipei", "高雄": "Kaohsiung", "台中": "Taichung",
    "丹东": "Dandong", "锦州": "Jinzhou", "营口": "Yingkou", "盘锦": "Panjin",
    "本溪": "Benxi", "辽阳": "Liaoyang", "铁岭": "Tieling", "舟山": "Zhoushan",
    "昆山": "Kunshan", "江阴": "Jiangyin", "义乌": "Yiwu", "慈溪": "Cixi",
}


def init(path) -> None:
    """打开城市缓存文件；没有就建一个空的（不影响使用）。"""
    global _path, _state
    _path = Path(path).expanduser()
    _path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if _path.exists():
        try:
            data = json.loads(_path.read_text("utf-8") or "{}")
        except (OSError, ValueError):
            logger.warning("天气缓存文件损坏，按空处理 path=%s", _path)
            data = {}
    _state = {
        "city": str(data.get("city") or ""),
        "coords": dict(data.get("coords") or {}),   # name → {lat, lon, admin, ts}
        "ip": dict(data.get("ip") or {}),
        "wx": dict(data.get("wx") or {}),           # 坐标 → {ts, text, brief}
    }


def _save() -> None:
    if _path is None:
        return
    try:
        tmp = _path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(_path)
    except OSError as e:                       # 只读目录/权限不对也不能让查天气失败
        logger.info("天气缓存写不进去：%s", type(e).__name__)


def enabled() -> bool:
    return (os.environ.get("RP_WEATHER") or "1").strip().lower() not in {
        "0", "false", "no", "off"}


def city() -> str:
    """当前默认城市：聊天里改过的优先，其次环境变量。"""
    return str(_state.get("city") or os.environ.get("RP_WEATHER_CITY") or "").strip()


def set_city(name: str) -> tuple[bool, str]:
    """把默认城市换成 name（要能地理编码成功才存）。"""
    name = str(name or "").strip()
    if not name:
        return False, "城市名是空的。"
    hit = _geocode(name)
    if not hit:
        return False, f"没找到「{name}」这个地名，换个写法试试（比如只写城市名）。"
    _state["city"] = hit["name"]
    _save()
    return True, f"天气地点改成 {hit['name']}" + (
        f"（{hit['admin']}）" if hit.get("admin") else "")


# ---------- 定位 ----------
def _clean(name: str) -> list:
    """地名候选：先按常见城市换成拉丁名，再原样、去「省/市/区/县」后缀。"""
    s = str(name or "").strip()
    if not s:
        return []
    out = []
    base = s
    for suf in ("特别行政区", "自治区", "省", "市", "区", "县"):
        if s.endswith(suf) and len(s) > len(suf) + 1:
            base = s[: -len(suf)]
            break
    for cand in (_CITY_ALIAS.get(s), _CITY_ALIAS.get(base), s, base):
        if cand and cand not in out:
            out.append(cand)
    return out


def _geocode(name: str) -> Optional[dict]:
    import httpx
    key = str(name or "").strip().lower()
    if not key:
        return None
    hit = (_state.get("coords") or {}).get(key)
    if hit and time.time() - float(hit.get("ts") or 0) < _GEO_TTL:
        return hit
    for cand in _clean(name):
        try:
            r = httpx.get(GEO, params={"name": cand, "count": 10,
                                       "language": "zh", "format": "json"},
                          headers={"User-Agent": UA}, timeout=8)
            res = (r.json() or {}).get("results") or []
        except (httpx.HTTPError, ValueError) as e:
            logger.info("天气地理编码失败 %s：%s", cand, type(e).__name__)
            continue
        if not res:
            continue
        # 同名村庄一大堆：只认人口够大的那个，否则宁可不答也不能报错城市
        top = max(res, key=lambda x: float(x.get("population") or 0))
        pop = float(top.get("population") or 0)
        if pop < _MIN_POP:
            logger.info("天气地理编码「%s」只找到同名小地名（人口 %d），跳过",
                        cand, int(pop))
            continue
        hit = {
            "name": str(top.get("name") or cand),
            "lat": float(top.get("latitude") or 0),
            "lon": float(top.get("longitude") or 0),
            "admin": str(top.get("admin1") or ""),
            "country": str(top.get("country") or ""),
            "ts": time.time(),
        }
        _state.setdefault("coords", {})[key] = hit
        _save()
        return hit
    return None


def _ip_locate() -> Optional[dict]:
    """服务器公网 IP 兜底定位：只当最后手段，机房城市未必是主人所在地。"""
    import httpx
    hit = _state.get("ip") or {}
    if hit and time.time() - float(hit.get("ts") or 0) < _IP_TTL:
        return hit
    tries = (
        ("http://ip-api.com/json/?lang=zh-CN&fields=status,city,regionName,lat,lon",
         lambda d: {"name": d.get("city"), "lat": d.get("lat"), "lon": d.get("lon"),
                    "admin": d.get("regionName") or ""} if d.get("status") == "success" else None),
        ("https://ipinfo.io/json",
         lambda d: {"name": d.get("city"), "lat": float(str(d.get("loc") or "0,0").split(",")[0]),
                    "lon": float(str(d.get("loc") or "0,0").split(",")[1]),
                    "admin": d.get("region") or ""} if d.get("loc") else None),
    )
    for url, parse in tries:
        try:
            d = httpx.get(url, headers={"User-Agent": UA}, timeout=6).json() or {}
            got = parse(d)
        except (httpx.HTTPError, ValueError, TypeError, IndexError) as e:
            logger.info("天气 IP 定位失败 %s：%s", url.split("/")[2], type(e).__name__)
            continue
        if got and got.get("lat") and got.get("lon"):
            got["ts"] = time.time()
            got["source"] = "ip"
            _state["ip"] = got
            _save()
            logger.info("天气定位走 IP 兜底：%s（机房城市，可能不是主人所在地）", got.get("name"))
            return got
    return None


def _resolve(city_name: str = "") -> Optional[dict]:
    """城市名 → 坐标；没给城市就走默认 → （可选）服务器 IP。"""
    name = str(city_name or "").strip() or city()
    if name:
        got = _geocode(name)
        if got:
            return got
    if (os.environ.get("RP_WEATHER_IP") or "0").strip().lower() in {
            "1", "true", "yes", "on"}:
        return _ip_locate()
    return None


# ---------- 取天气 ----------
def _fetch(coords: dict) -> Optional[dict]:
    import httpx
    key = f"{coords['lat']:.2f},{coords['lon']:.2f}"
    cache = (_state.get("wx") or {}).get(key) or {}
    if cache and time.time() - float(cache.get("ts") or 0) < _WEATHER_TTL:
        return cache
    try:
        r = httpx.get(FORECAST, params={
            "latitude": coords["lat"], "longitude": coords["lon"],
            "current": "temperature_2m,apparent_temperature,weather_code,"
                       "relative_humidity_2m,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,sunrise,sunset",
            "timezone": "Asia/Shanghai", "forecast_days": 2,
        }, headers={"User-Agent": UA}, timeout=10)
        data = r.json() or {}
    except (httpx.HTTPError, ValueError) as e:
        logger.info("天气接口失败：%s", type(e).__name__)
        return cache or None
    cur = data.get("current") or {}
    if not cur:
        return cache or None
    cache = {"ts": time.time(), "raw": data}
    _state.setdefault("wx", {})[key] = cache
    _save()
    return cache


def _fmt(data: dict, coords: dict) -> str:
    cur = data.get("current") or {}
    daily = data.get("daily") or {}

    def first(k):
        v = daily.get(k) or []
        return v[0] if v else None

    def second(k):
        v = daily.get(k) or []
        return v[1] if len(v) > 1 else None

    where = str(coords.get("name") or "")
    if coords.get("admin") and coords["admin"] != where:
        where = f"{where}（{coords['admin']}）"
    now_t = cur.get("temperature_2m")
    feel = cur.get("apparent_temperature")
    code = int(cur.get("weather_code") or 0)
    desc = CODE_CN.get(code, "说不清的天气")
    wind = cur.get("wind_speed_10m")
    hum = cur.get("relative_humidity_2m")
    lines = [f"{where} 现在 {_n(now_t)}°C（体感 {_n(feel)}°C），{desc}"
             + (f"，风 {_n(wind)}km/h" if wind is not None else "")
             + (f"，湿度 {_n(hum)}%" if hum is not None else "")]
    hi, lo = first("temperature_2m_max"), first("temperature_2m_min")
    rain = first("precipitation_probability_max")
    line = f"今天 {_n(lo)}~{_n(hi)}°C"
    if rain is not None:
        line += f"，降水概率 {_n(rain)}%"
    sr, ss = first("sunrise"), first("sunset")
    tail = []
    if sr:
        tail.append("日出 " + str(sr)[-5:])
    if ss:
        tail.append("日落 " + str(ss)[-5:])
    if tail:
        line += "，" + "｜".join(tail)
    lines.append(line)
    if second("temperature_2m_max") is not None:
        line2 = (f"明天 {_n(second('temperature_2m_min'))}"
                 f"~{_n(second('temperature_2m_max'))}°C")
        if second("precipitation_probability_max") is not None:
            line2 += f"，降水概率 {_n(second('precipitation_probability_max'))}%"
        lines.append(line2)
    if coords.get("source") == "ip":
        lines.append("（位置是服务器 IP 猜的，你换地方了就跟我说一声城市）")
    return "\n".join(lines)


def _n(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return str(int(round(f)))


def report(city_name: str = "") -> str:
    """给聊天用的天气：现在 + 今天 + 明天；取不到就说清楚原因。"""
    if not enabled():
        return "天气功能关着（RP_WEATHER=0）。"
    coords = _resolve(city_name)
    if not coords:
        name = str(city_name or "").strip()
        if name:
            return f"没查到「{name}」的天气（地名没认出来或网络不通），说城市名就行。"
        return "我还不知道你在哪，告诉我一个城市名就行。"
    cache = _fetch(coords)
    if not cache or not cache.get("raw"):
        return "天气接口暂时连不上，等会儿再试。"
    return _fmt(cache["raw"], coords)


def brief() -> str:
    """给自主作息醒来时的一句天气，取不到返回空串。"""
    if not enabled():
        return ""
    try:
        coords = _resolve("")
        if not coords:
            return ""
        cache = _fetch(coords)
        if not cache or not cache.get("raw"):
            return ""
        data = cache["raw"]
        cur = data.get("current") or {}
        daily = data.get("daily") or {}
        hi = (daily.get("temperature_2m_max") or [None])[0]
        lo = (daily.get("temperature_2m_min") or [None])[0]
        rain = (daily.get("precipitation_probability_max") or [None])[0]
        where = str(coords.get("name") or "")
        desc = CODE_CN.get(int(cur.get("weather_code") or 0), "")
        out = f"{where} {_n(cur.get('temperature_2m'))}°C {desc}，今天 {_n(lo)}~{_n(hi)}°C"
        if rain is not None and float(rain or 0) >= 30:
            out += f"，降水概率 {_n(rain)}%"
        return out
    except Exception as e:                       # noqa: BLE001 天气坏了不能拖垮作息
        logger.info("自主作息取天气失败：%s", type(e).__name__)
        return ""
