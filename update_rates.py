import html
import json
import math
import os
import re
import time
import signal as os_signal
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import requests

BOK_API_KEY = os.environ.get("BOK_API_KEY")
if not BOK_API_KEY:
    raise SystemExit("BOK_API_KEY environment variable is missing.")

OUT = Path("rates.json")

NAVER_DETAIL_URL = "https://finance.naver.com/marketindex/exchangeDetail.naver?marketindexCd=FX_RUBKRW"
NAVER_DAILY_URL = "https://finance.naver.com/marketindex/exchangeDailyQuote.naver?marketindexCd=FX_RUBKRW&page=1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://finance.naver.com/marketindex/",
    "Connection": "close",
}


def _timeout_handler(signum, frame):
    raise TimeoutError("update_rates.py exceeded the safety time limit")


try:
    os_signal.signal(os_signal.SIGALRM, _timeout_handler)
    os_signal.alarm(240)
except Exception:
    pass


def to_float(value):
    if value is None:
        return None

    text = html.unescape(str(value)).strip()
    text = re.sub(r"[^0-9,\.\-]", "", text)

    if not text or text in ("-", ".", ","):
        return None

    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text and "." not in text:
        text = text.replace(",", ".")

    return float(text)


def get_url(url, retries=3, backoff=2, **kwargs):
    timeout = kwargs.pop("timeout", (10, 20))
    last_exc = None

    for attempt in range(1, retries + 1):
        try:
            return requests.get(url, timeout=timeout, **kwargs)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            print(f"Warning: request failed ({attempt}/{retries}): {url} -> {exc}")
            if attempt < retries:
                time.sleep(backoff * attempt)

    raise last_exc


def load_existing_rows():
    if not OUT.exists():
        return []

    try:
        rows = json.loads(OUT.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except Exception as exc:
        print(f"Warning: failed to read existing rates.json: {exc}")
        return []


def build_bok_map_from_existing(rows):
    result = {}

    for row in rows:
        d = row.get("bok_source_date") or row.get("date")
        v = row.get("bok_usd_krw")

        if d and v:
            result[d] = float(v)

    return dict(sorted(result.items()))


def existing_value_for_date(rows, iso_date, field):
    candidates = [
        r for r in rows
        if r.get("date") <= iso_date and r.get(field) not in (None, "")
    ]

    if not candidates:
        return None

    return candidates[-1].get(field)


def fetch_bok_usd_krw(days_back=80):
    end = date.today()
    start = end - timedelta(days=days_back)

    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{BOK_API_KEY}/json/kr/1/5000/731Y001/D/"
        f"{start:%Y%m%d}/{end:%Y%m%d}"
    )

    r = get_url(url, timeout=(12, 25), retries=4, backoff=3)
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

    r = get_url(
        url,
        params={"date_req": date_req},
        timeout=(8, 15),
        retries=3,
        backoff=2,
    )
    r.raise_for_status()

    root = ET.fromstring(r.content)

    for valute in root.findall("Valute"):
        if valute.findtext("CharCode") == "USD":
            nominal = to_float(valute.findtext("Nominal"))
            value = to_float(valute.findtext("Value"))
            usd_rub = value / nominal

            if usd_rub < 20 or usd_rub > 200:
                raise RuntimeError(f"CBR USD/RUB looks wrong: {usd_rub}")

            return usd_rub

    raise RuntimeError(f"USD not found in CBR response for {iso_date}")


def _decode_response(response):
    raw = response.content

    for enc in [response.encoding, "euc-kr", "cp949", "utf-8"]:
        if not enc:
            continue

        try:
            return raw.decode(enc)
        except Exception:
            pass

    return response.text


def _clean_text(raw):
    raw = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.I)
    raw = re.sub(r"<style[\s\S]*?</style>", " ", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    return re.sub(r"\s+", " ", raw).strip()


def _flex_label_pattern(label):
    compact = re.sub(r"\s+", "", label)
    return r"\s*".join(map(re.escape, compact))


def _extract_after_label(text, label):
    pattern = _flex_label_pattern(label) + r"\s*([0-9][0-9,\.]*|N/A)"
    m = re.search(pattern, text)

    if not m:
        return None

    return m.group(1) if m.group(1) == "N/A" else to_float(m.group(1))


def _extract_main_naver_rate(raw, text, send=None, receive=None):
    candidates = []

    patterns = [
        r'class=["\']no_today["\'][\s\S]{0,1600}?<span[^>]*class=["\']blind["\'][^>]*>\s*([0-9][0-9,\.]*)\s*</span>',
        r'class=["\']no_today["\'][\s\S]{0,1600}?<em[^>]*>\s*([0-9][0-9,\.]*)\s*</em>',
        r'러시아\s*RUB\s*([0-9][0-9,\.]*)\s*원',
        r'RUBKRW[^0-9]{0,80}([0-9][0-9,\.]*)\s*원',
        r'([0-9][0-9,\.]*)\s*원\s*전일대비',
    ]

    for pat in patterns:
        source = raw if "class=" in pat else text

        for m in re.finditer(pat, source, flags=re.I):
            value = to_float(m.group(1))

            if value is not None:
                candidates.append(value)

    # 7.0 같은 전일대비/기타 숫자를 제거
    candidates = [v for v in candidates if 10 <= v <= 40]

    # 송금 보내실 때/받으실 때 값이 있으면 그 주변 값만 대표 환율로 인정
    if send and receive:
        send = float(send)
        receive = float(receive)

        low = min(send, receive) - 1.0
        high = max(send, receive) + 1.0
        filtered = [v for v in candidates if low <= v <= high]

        if filtered:
            midpoint = (send + receive) / 2
            return min(filtered, key=lambda x: abs(x - midpoint))

        # 대표값을 못 찾으면 송금값 평균으로 대체
        return round((send + receive) / 2, 4)

    if candidates:
        return candidates[0]

    return None


def empty_naver_data():
    return {
        "naver_rub_krw": None,
        "naver_change": None,
        "naver_change_pct": None,
        "naver_time": None,
        "naver_cash_buy": None,
        "naver_cash_sell": None,
        "naver_send": None,
        "naver_receive": None,
        "naver_tc_buy": None,
        "naver_check_sell": None,
    }


def fetch_naver_rub_krw():
    result = empty_naver_data()

    r = get_url(NAVER_DETAIL_URL, headers=HEADERS, timeout=(5, 10))
    r.raise_for_status()

    raw = _decode_response(r)
    text = _clean_text(raw)

    # 먼저 보조 환율을 읽어 대표 환율 검증 기준으로 사용
    result["naver_cash_buy"] = _extract_after_label(text, "현찰 사실 때")
    result["naver_cash_sell"] = _extract_after_label(text, "현찰 파실 때")
    result["naver_send"] = _extract_after_label(text, "송금 보내실 때")
    result["naver_receive"] = _extract_after_label(text, "송금 받으실 때")
    result["naver_tc_buy"] = _extract_after_label(text, "T/C 사실 때")
    result["naver_check_sell"] = _extract_after_label(text, "외화수표 파실 때")

    result["naver_rub_krw"] = _extract_main_naver_rate(
        raw,
        text,
        result["naver_send"],
        result["naver_receive"],
    )

    m = re.search(
        r"전일대비\s*([▲▼+\-]?)\s*([0-9][0-9,\.]*)\s*([+\-]?[0-9][0-9,\.]*%)",
        text,
    )

    if m:
        sign = -1 if m.group(1) in ("▼", "-") else 1
        result["naver_change"] = sign * to_float(m.group(2))
        result["naver_change_pct"] = m.group(3)

    m = re.search(r"(20\d{2}\.\d{2}\.\d{2}\s+\d{2}:\d{2})", text)

    if m:
        result["naver_time"] = m.group(1)

    if result["naver_rub_krw"] is None:
        try:
            r2 = get_url(NAVER_DAILY_URL, headers=HEADERS, timeout=(5, 8))
            r2.raise_for_status()

            raw2 = _decode_response(r2)

            m2 = re.search(
                r"<tr[^>]*>[\s\S]*?<td[^>]*class=[\"']date[\"'][^>]*>\s*(20\d{2}\.\d{2}\.\d{2})\s*</td>[\s\S]*?<td[^>]*>\s*([0-9][0-9,\.]*)\s*</td>",
                raw2,
                flags=re.I,
            )

            if m2:
                fallback_value = to_float(m2.group(2))

                if fallback_value is not None and 10 <= fallback_value <= 40:
                    result["naver_rub_krw"] = fallback_value

        except Exception as exc:
            print(f"Warning: Naver daily fallback failed: {exc}")

    return result


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


def last_bok_value_on_or_before(bok_map, iso_date):
    available_dates = [d for d in bok_map.keys() if d <= iso_date]

    if not available_dates:
        raise RuntimeError(f"No BOK USD/KRW value available on or before {iso_date}")

    last_date = max(available_dates)

    return bok_map[last_date], last_date


def main():
    today = date.today()
    today_iso = today.isoformat()
    existing_rows = load_existing_rows()

    try:
        bok = fetch_bok_usd_krw(days_back=80)
    except Exception as exc:
        print(f"Warning: failed to fetch BOK USD/KRW. Falling back to existing rates.json: {exc}")
        bok = build_bok_map_from_existing(existing_rows)

        if not bok:
            raise RuntimeError("BOK fetch failed and no existing rates.json fallback is available.")

    recent_dates = [
        (today - timedelta(days=i)).isoformat()
        for i in range(9, -1, -1)
    ]

    try:
        naver = fetch_naver_rub_krw()
        print("Fetched Naver RUB/KRW data.")
    except Exception as exc:
        print(f"Warning: failed to fetch Naver RUB/KRW data: {exc}")
        naver = empty_naver_data()

    rows = []
    calc_series = []

    for dt in recent_dates:
        bok_usd_krw, bok_source_date = last_bok_value_on_or_before(bok, dt)

        try:
            cbr_usd_rub = fetch_cbr_usd_rub(dt)
        except Exception as exc:
            old_cbr = (
                existing_value_for_date(existing_rows, dt, "cbr_usd_rub")
                or existing_value_for_date(existing_rows, dt, "usd_rub")
            )

            if old_cbr is None:
                raise

            print(f"Warning: failed to fetch CBR for {dt}. Using existing value {old_cbr}: {exc}")
            cbr_usd_rub = float(old_cbr)

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

        old_row = next((r for r in existing_rows if r.get("date") == dt), None)

        # 과거 날짜는 기존 네이버 값을 유지
        if dt != today_iso and old_row:
            for key in empty_naver_data().keys():
                if old_row.get(key) not in (None, ""):
                    row[key] = old_row.get(key)

        # 오늘 날짜는 새로 가져온 네이버 값만 저장
        # 기존 오늘 값이나 어제 값이 잘못 이어붙는 것을 방지
        if dt == today_iso:
            naver_rate = naver.get("naver_rub_krw")

            if naver_rate is not None:
                naver_rate = float(naver_rate)

                # CALC와 너무 멀면 잘못 읽은 값으로 보고 저장하지 않음
                if abs(naver_rate - calc_rub_krw) <= 3:
                    for key, value in naver.items():
                        if value not in (None, ""):
                            row[key] = value
                else:
                    print(
                        f"Warning: Naver RUB/KRW rejected. "
                        f"naver={naver_rate}, calc={calc_rub_krw}"
                    )

        rows.append(row)

    for i, row in enumerate(rows):
        s = score_system(calc_series[:i + 1], row["calc_rub_krw"])
        row["score"] = s
        row["signal"] = signal(s)

    OUT.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Wrote {OUT} with {len(rows)} rows.")
    print(f"Latest date: {rows[-1]['date']}")
    print(f"Latest BOK source date: {rows[-1]['bok_source_date']}")
    print(f"Latest CBR USD/RUB: {rows[-1]['cbr_usd_rub']}")
    print(f"Latest Naver RUB/KRW: {rows[-1].get('naver_rub_krw')}")


if __name__ == "__main__":
    main()
