import os
import json
import math
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import requests

BOK_API_KEY = os.environ.get("BOK_API_KEY")
if not BOK_API_KEY:
    raise SystemExit("BOK_API_KEY environment variable is missing.")

OUT = Path("rates.json")


def to_float(value):
    return float(str(value).replace(",", "."))


def fetch_bok_usd_krw(days_back=80):
    end = date.today()
    start = end - timedelta(days=days_back)
    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{BOK_API_KEY}/json/kr/1/5000/731Y001/D/"
        f"{start:%Y%m%d}/{end:%Y%m%d}"
    )
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    payload = r.json()

    if "StatisticSearch" not in payload:
        raise RuntimeError(f"Unexpected BOK response: {payload}")

    rows = payload["StatisticSearch"].get("row", [])
    result = {}
    for row in rows:
        name = row.get("ITEM_NAME1", "")
        if "미국달러" in name:
            t = row["TIME"]
            d = f"{t[:4]}-{t[4:6]}-{t[6:]}"
            result[d] = to_float(row["DATA_VALUE"])

    if not result:
        raise RuntimeError("No USD/KRW rows found from BOK.")

    return dict(sorted(result.items()))


def fetch_cbr_usd_rub(iso_date):
    y, m, d = iso_date.split("-")
    date_req = f"{d}/{m}/{y}"
    url = "https://www.cbr.ru/scripts/XML_daily_eng.asp"
    r = requests.get(url, params={"date_req": date_req}, timeout=25)
    r.raise_for_status()
    root = ET.fromstring(r.content)

    for valute in root.findall("Valute"):
        code = valute.findtext("CharCode")
        if code == "USD":
            nominal = to_float(valute.findtext("Nominal"))
            value = to_float(valute.findtext("Value"))
            return value / nominal
    raise RuntimeError(f"USD not found in CBR response for {iso_date}")


def score_system(calc_series, current):
    recent = calc_series[-10:]
    avg = sum(recent) / len(recent)
    variance = sum((x - avg) ** 2 for x in recent) / len(recent)
    vol = math.sqrt(variance)
    dev = (current - avg) / avg
    trend = 0 if len(recent) < 2 else (recent[-1] - recent[0]) / recent[0]

    # CALC_RUB_KRW는 1 RUB당 KRW. 낮을수록 원화로 루블 환전 유리.
    score = 60 + (-dev * 900) + (-trend * 250) - (vol * 1.2)
    return max(0, min(100, round(score)))


def signal(score):
    if score >= 80:
        return "🟢 STRONG BUY"
    if score >= 65:
        return "🟢 BUY"
    if score >= 50:
        return "🟡 HOLD"
    return "🔴 WAIT"


def main():
    bok = fetch_bok_usd_krw()
    recent_dates = list(bok.keys())[-10:]

    rows = []
    calc_series = []
    for dt in recent_dates:
        bok_usd_krw = bok[dt]
        cbr_usd_rub = fetch_cbr_usd_rub(dt)
        calc_rub_krw = bok_usd_krw / cbr_usd_rub
        calc_series.append(calc_rub_krw)
        rows.append({
            "date": dt,
            "bok_usd_krw": round(bok_usd_krw, 4),
            "cbr_usd_rub": round(cbr_usd_rub, 6),
            "calc_rub_krw": round(calc_rub_krw, 6),
            "krw_1_5m_to_rub": round(1500000 / calc_rub_krw),
            "usd_1000_to_rub": round(1000 * cbr_usd_rub),
            # backward-compatible aliases
            "usd_rub": round(cbr_usd_rub, 6),
            "krw_rub": round(calc_rub_krw, 6),
        })

    for i, row in enumerate(rows):
        s = score_system(calc_series[: i + 1], row["calc_rub_krw"])
        row["score"] = s
        row["signal"] = signal(s)

    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT} with {len(rows)} rows. Latest: {rows[-1]['date']}")


if __name__ == "__main__":
    main()
