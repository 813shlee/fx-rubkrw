import os
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import requests

BOK_API_KEY = os.environ.get("BOK_API_KEY")
if not BOK_API_KEY:
    raise SystemExit("BOK_API_KEY environment variable is missing.")

OUT = Path("rates.json")

# 네이버 현재 RUB/KRW 상세 페이지
NAVER_URL = "https://finance.naver.com/marketindex/exchangeDetail.naver"


# ----------------------------
# 숫자 파서
# ----------------------------
def parse_kr_number(value):
    """한국/네이버식 숫자: 1,537.20 -> 1537.20"""
    return float(str(value).replace(",", "").strip())


def parse_ru_number(value):
    """러시아/CBR식 숫자: 74,6200 -> 74.6200"""
    return float(str(value).replace(",", ".").strip())


# ----------------------------
# 기존 rates.json 로드
# 네이버 값은 매일 누적 보존하기 위해 사용
# 단, 잘못된 값은 복사하지 않음
# ----------------------------
def load_existing_rows():
    if not OUT.exists():
        return {}

    try:
        rows = json.loads(OUT.read_text(encoding="utf-8"))
        return {r.get("date"): r for r in rows if r.get("date")}
    except Exception as e:
        print(f"Existing rates.json could not be read. Starting fresh. Reason: {e}")
        return {}


# ----------------------------
# BOK USD/KRW
# ----------------------------
def fetch_bok_usd_krw(days_back=80):
    end = date.today()
    start = end - timedelta(days=days_back)

    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{BOK_API_KEY}/json/kr/1/5000/"
        f"731Y001/D/{start:%Y%m%d}/{end:%Y%m%d}"
    )

    r = requests.get(url, timeout=25)
    r.raise_for_status()
    payload = r.json()

    if "StatisticSearch" not in payload:
        raise RuntimeError(f"Unexpected BOK response: {payload}")

    rows = payload["StatisticSearch"].get("row", [])
    result = {}

    for row in rows:
        if "미국달러" in row.get("ITEM_NAME1", ""):
            t = row["TIME"]
            d = f"{t[:4]}-{t[4:6]}-{t[6:]}"
            result[d] = parse_kr_number(row["DATA_VALUE"])

    if not result:
        raise RuntimeError("No BOK USD/KRW data found.")

    return dict(sorted(result.items()))


def last_bok_value_on_or_before(bok_map, iso_date):
    available_dates = [d for d in bok_map.keys() if d <= iso_date]
    if not available_dates:
        raise RuntimeError(f"No BOK USD/KRW value available on or before {iso_date}")

    source_date = max(available_dates)
    return bok_map[source_date], source_date


# ----------------------------
# CBR USD/RUB
# ----------------------------
def fetch_cbr_usd_rub(iso_date):
    y, m, d = iso_date.split("-")
    date_req = f"{d}/{m}/{y}"

    url = "https://www.cbr.ru/scripts/XML_daily_eng.asp"
    r = requests.get(url, params={"date_req": date_req}, timeout=25)
    r.raise_for_status()

    root = ET.fromstring(r.content)

    for valute in root.findall("Valute"):
        if valute.findtext("CharCode") == "USD":
            nominal = parse_ru_number(valute.findtext("Nominal"))
            value = parse_ru_number(valute.findtext("Value"))
            usd_rub = value / nominal

            # 이상치 방지: 746200 같은 값이면 여기서 실패시킴
            if usd_rub < 20 or usd_rub > 200:
                raise RuntimeError(f"CBR USD/RUB looks wrong: {usd_rub}")

            return usd_rub

    raise RuntimeError(f"USD not found in CBR for {iso_date}")


# ----------------------------
# NAVER RUB/KRW
# ----------------------------
def fetch_naver_rub_krw():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://finance.naver.com/marketindex/",
    }

    r = requests.get(
        NAVER_URL,
        params={"marketindexCd": "FX_RUBKRW"},
        headers=headers,
        timeout=25,
    )
    r.raise_for_status()
    r.encoding = "euc-kr"
    html = r.text

    # 1차: 상세 페이지의 현재가 영역
    m = re.search(
        r'class=["\']no_today["\'][\s\S]*?<span class=["\']blind["\']>\s*([0-9,.]+)\s*</span>',
        html,
    )
    if m:
        value = parse_kr_number(m.group(1))
        if 5 <= value <= 50:
            return value

    # 2차: 페이지 전체 숫자 중 RUB/KRW 범위에 맞는 값 찾기
    # 날짜/퍼센트/기타 숫자를 피하기 위해 5~50 범위만 허용
    matches = re.findall(r'([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)', html)
    candidates = []
    for text in matches:
        try:
            value = parse_kr_number(text)
            if 5 <= value <= 50:
                candidates.append(value)
        except Exception:
            pass

    if candidates:
        return candidates[0]

    raise RuntimeError("NAVER RUB/KRW parsing failed")


# ----------------------------
# scoring
# ----------------------------
def score_system(calc_series, current):
    recent = calc_series[-10:]
    avg = sum(recent) / len(recent)
    variance = sum((x - avg) ** 2 for x in recent) / len(recent)
    vol = math.sqrt(variance)
    dev = (current - avg) / avg
    trend = 0 if len(recent) < 2 else (recent[-1] - recent[0]) / recent[0]

    score = 60 + (-dev * 900) + (-trend * 250) - (vol * 1.2)
    return max(0, min(100, round(score)))


def signal(score):
    if score >= 80:
        return "▲ BEST"
    if score >= 65:
        return "● GOOD"
    if score >= 50:
        return "■ NORMAL"
    return "▼ BAD"


# ----------------------------
# main
# ----------------------------
def main():
    today_date = date.today()
    today_iso = today_date.isoformat()

    existing = load_existing_rows()
    bok = fetch_bok_usd_krw(days_back=80)

    # 네이버가 실패해도 BOK/CBR 데이터는 업데이트되도록 처리
    try:
        naver_today = fetch_naver_rub_krw()
    except Exception as e:
        print(f"Naver fetch failed, skipping Naver value: {e}")
        naver_today = None

    dates = [
        (today_date - timedelta(days=i)).isoformat()
        for i in range(9, -1, -1)
    ]

    rows = []
    calc_series = []

    for dt in dates:
        bok_usd_krw, bok_source_date = last_bok_value_on_or_before(bok, dt)
        cbr_usd_rub = fetch_cbr_usd_rub(dt)
        calc_rub_krw = bok_usd_krw / cbr_usd_rub
        calc_series.append(calc_rub_krw)

        row = {
            "date": dt,
            "bok_source_date": bok_source_date,
            "bok_usd_krw": round(bok_usd_krw, 4),
            "cbr_usd_rub": round(cbr_usd_rub, 6),
            "calc_rub_krw": round(calc_rub_krw, 6),
            "krw_1_5m_to_rub": round(1500000 / calc_rub_krw),
            "usd_rub": round(cbr_usd_rub, 6),
            "krw_rub": round(calc_rub_krw, 6),
        }

        # 기존 네이버 값 보존. 단, 2.0 같은 잘못된 값은 버림.
        old = existing.get(dt)
        if old and old.get("naver_rub_krw") is not None:
            try:
                old_naver = float(old["naver_rub_krw"])
                if 5 <= old_naver <= 50:
                    row["naver_rub_krw"] = round(old_naver, 4)
                    row["naver_calc_diff"] = round(old_naver - calc_rub_krw, 4)
            except Exception:
                pass

        # 오늘 값은 새로 갱신
        if dt == today_iso and naver_today is not None:
            row["naver_rub_krw"] = round(naver_today, 4)
            row["naver_calc_diff"] = round(naver_today - calc_rub_krw, 4)

        rows.append(row)

    for i, row in enumerate(rows):
        s = score_system(calc_series[: i + 1], row["calc_rub_krw"])
        row["score"] = s
        row["signal"] = signal(s)

    OUT.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Wrote {OUT} with {len(rows)} rows.")
    print(f"Latest date: {rows[-1]['date']}")
    print(f"Latest BOK USD/KRW: {rows[-1]['bok_usd_krw']}")
    print(f"Latest CBR USD/RUB: {rows[-1]['cbr_usd_rub']}")
    print(f"Latest CALC RUB/KRW: {rows[-1]['calc_rub_krw']}")
    print(f"Latest NAVER RUB/KRW: {rows[-1].get('naver_rub_krw')}")


if __name__ == "__main__":
    main()
