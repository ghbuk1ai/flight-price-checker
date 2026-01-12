import os
import json
import requests
from datetime import date, timedelta

# =========================
# REQUIRED / OPTIONAL SECRETS
# =========================
DUFFEL_TOKEN = os.environ["DUFFEL_TOKEN"]
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")  # optional

# =========================
# YOUR SETTINGS
# =========================
ORIGIN = "ORD"
DEST = "LHR"  # Heathrow. Change if you want another London airport.

CURRENCY = "USD"
THRESHOLD = 2500.00

# Window: 2–4 weeks out
START_DAYS_OUT = 14
END_DAYS_OUT = 28

# Trip length (adjust as you want)
MIN_TRIP_DAYS = 3
MAX_TRIP_DAYS = 14

# Stops preference:
# Prefer nonstop, but if none exist for a date, allow 1-stop as fallback.
PREFER_NONSTOP = True
MAX_STOPS_PREFERRED = 0   # 0 = nonstop
MAX_STOPS_FALLBACK = 1    # allow up to 1 stop if no nonstop

HEADERS = {
    "Authorization": f"Bearer {DUFFEL_TOKEN}",
    "Duffel-Version": "v2",
    "Content-Type": "application/json",
}

# =========================
# DUFFEL API HELPERS
# =========================
def create_offer_request(origin: str, destination: str, depart_date: date, cabin_class: str) -> str:
    url = "https://api.duffel.com/air/offer_requests"

    payload = {
        "data": {
            "slices": [
                {
                    "origin": origin,
                    "destination": destination,
                    "departure_date": depart_date.isoformat(),
                }
            ],
            "passengers": [{"type": "adult"}],
            "cabin_class": cabin_class,  # "business", "premium_economy", etc.
        }
    }

    r = requests.post(url, headers=HEADERS, json=payload, timeout=30)

    if not r.ok:
        print("Duffel error status:", r.status_code)
        print("Duffel error body:", r.text)

    r.raise_for_status()
    return r.json()["data"]["id"]

def list_offers(offer_request_id: str, limit: int = 30) -> list:
    url = "https://api.duffel.com/air/offers"
    params = {"offer_request_id": offer_request_id, "limit": limit}
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()["data"]

def offer_stops(offer: dict) -> int:
    """
    Stops = number of segments - 1, for the first slice (one-way).
    """
    slice0 = offer["slices"][0]
    segments = slice0.get("segments", [])
    return max(len(segments) - 1, 0)

def cheapest_offer(offers: list) -> dict | None:
    """
    Picks the cheapest offer in USD, with a preference for nonstop (or max stops).
    Returns:
      {
        "amount": float,
        "offer_id": str,
        "offer": dict,
        "stops": int
      }
    """
    usd_offers = [o for o in offers if o.get("total_currency") == CURRENCY]
    if not usd_offers:
        return None

    if PREFER_NONSTOP:
        preferred = [o for o in usd_offers if offer_stops(o) <= MAX_STOPS_PREFERRED]
        if preferred:
            candidates = preferred
        else:
            candidates = [o for o in usd_offers if offer_stops(o) <= MAX_STOPS_FALLBACK]
            if not candidates:
                candidates = usd_offers
    else:
        candidates = usd_offers

    best = None
    for o in candidates:
        amt = float(o["total_amount"])
        s = offer_stops(o)
        if best is None or amt < best["amount"]:
            best = {"amount": amt, "offer_id": o["id"], "offer": o, "stops": s}

    return best

# =========================
# SLACK HELPERS
# =========================
def notify_slack(text: str) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15).raise_for_status()

def _fmt_time(iso_str: str) -> str:
    return iso_str.replace("T", " ")[:16]

def _carrier_name(segment: dict) -> str:
    mc = segment.get("marketing_carrier") or {}
    return mc.get("name") or mc.get("iata_code") or "Unknown airline"

def _flight_designator(segment: dict) -> str:
    mc = segment.get("marketing_carrier") or {}
    code = mc.get("iata_code") or ""
    num = segment.get("marketing_flight_number") or ""
    if code and num:
        return f"{code}{num}"
    return str(num) if num else "Flight"

def _extract_offer_summary(offer: dict) -> dict:
    """
    Summarize a Duffel offer into fields we can show in Slack.
    Assumes a one-way offer with one slice.
    """
    slice0 = offer["slices"][0]
    segments = slice0.get("segments", [])
    if not segments:
        return {
            "origin": "?",
            "destination": "?",
            "depart": "?",
            "arrive": "?",
            "stops": 0,
            "duration": slice0.get("duration") or "N/A",
            "airlines": [],
            "flights": [],
        }

    stops = max(len(segments) - 1, 0)

    first = segments[0]
    last = segments[-1]

    origin = first["origin"]["iata_code"]
    destination = last["destination"]["iata_code"]
    depart = _fmt_time(first["departing_at"])
    arrive = _fmt_time(last["arriving_at"])

    airlines = []
    flights = []
    for s in segments:
        airlines.append(_carrier_name(s))
        flights.append(_flight_designator(s))

    # De-dupe airlines while preserving order
    seen = set()
    airlines_unique = []
    for a in airlines:
        if a not in seen:
            airlines_unique.append(a)
            seen.add(a)

    return {
        "origin": origin,
        "destination": destination,
        "depart": depart,
        "arrive": arrive,
        "stops": stops,
        "duration": slice0.get("duration") or "N/A",
        "airlines": airlines_unique,
        "flights": flights,
    }

def _format_leg_for_slack(title: str, price: float, summary: dict, cabin_label: str) -> str:
    stops_txt = "Nonstop" if summary["stops"] == 0 else f"{summary['stops']} stop"
    if summary["stops"] > 1:
        stops_txt += "s"

    preference_note = ""
    if PREFER_NONSTOP and summary["stops"] > 0:
        preference_note = " _(nonstop not available; best alternative)_"

    airlines_txt = ", ".join(summary["airlines"]) if summary["airlines"] else "Unknown"
    flights_txt = ", ".join(summary["flights"]) if summary["flights"] else "Unknown"

    return (
        f"*{title}* ({cabin_label}) — *${price:.2f}*{preference_note}\n"
        f"{summary['origin']} → {summary['destination']} | {summary['depart']} → {summary['arrive']}\n"
        f"{stops_txt} | Duration {summary['duration']}\n"
        f"Airline(s): {airlines_txt}\n"
        f"Flights: {flights_txt}"
    )

# =========================
# MAIN
# =========================
def main() -> None:
    today = date.today()
    start = today + timedelta(days=START_DAYS_OUT)
    end = today + timedelta(days=END_DAYS_OUT)

    one_way_cache = {}
    results = []
    alerts = []

    def best_one_way(origin: str, dest: str, d: date, cabin: str) -> dict | None:
        key = (origin, dest, d.isoformat(), cabin)
        if key in one_way_cache:
            return one_way_cache[key]

        req_id = create_offer_request(origin, dest, d, cabin)
        offers = list_offers(req_id, limit=30)
        best = cheapest_offer(offers)
        one_way_cache[key] = best
        return best

    out_date = start
    while out_date <= end:
        ret_min = out_date + timedelta(days=MIN_TRIP_DAYS)
        ret_max = min(out_date + timedelta(days=MAX_TRIP_DAYS), end)

        ret_date = ret_min
        while ret_date <= ret_max:
            out_best = best_one_way(ORIGIN, DEST, out_date, "business")
            ret_best = best_one_way(DEST, ORIGIN, ret_date, "premium_economy")

            if out_best and ret_best:
                total = out_best["amount"] + ret_best["amount"]
                row = {
                    "out_date": out_date.isoformat(),
                    "ret_date": ret_date.isoformat(),
                    "out_usd": out_best["amount"],
                    "ret_usd": ret_best["amount"],
                    "total_usd": round(total, 2),
                    "out_offer_id": out_best["offer_id"],
                    "ret_offer_id": ret_best["offer_id"],
                    "out_offer": out_best["offer"],
                    "ret_offer": ret_best["offer"],
                    "out_stops": out_best["stops"],
                    "ret_stops": ret_best["stops"],
                }
                results.append(row)
                if total < THRESHOLD:
                    alerts.append(row)

            ret_date += timedelta(days=1)

        out_date += timedelta(days=1)

    results.sort(key=lambda x: x["total_usd"])

    print("Top 5 cheapest mixed-cabin combos:")
    for r in results[:5]:
        print(
            f'{r["out_date"]} → {r["ret_date"]} | '
            f'Out ${r["out_usd"]:.2f} + Back ${r["ret_usd"]:.2f} = ${r["total_usd"]:.2f} | '
            f'Out stops: {r["out_stops"]}, Back stops: {r["ret_stops"]}'
        )

    if alerts:
        alerts.sort(key=lambda x: x["total_usd"])
        best = alerts[0]

        out_summary = _extract_offer_summary(best["out_offer"])
        ret_summary = _extract_offer_summary(best["ret_offer"])

        out_text = _format_leg_for_slack(
            title="Outbound",
            price=best["out_usd"],
            summary=out_summary,
            cabin_label="Business",
        )
        ret_text = _format_leg_for_slack(
            title="Return",
            price=best["ret_usd"],
            summary=ret_summary,
            cabin_label="Premium Economy",
        )

        msg = (
            f"✈️ *Deal found under ${THRESHOLD:.0f}* — *${best['total_usd']:.2f} total*\n"
            f"Dates: {best['out_date']} → {best['ret_date']}\n\n"
            f"{out_text}\n\n"
            f"{ret_text}\n\n"
            f"Offer IDs: out `{best['out_offer_id']}` / back `{best['ret_offer_id']}`"
        )

        print(msg)
        notify_slack(msg)

    with open("latest_results.json", "w", encoding="utf-8") as f:
        json.dump({"generated": today.isoformat(), "top5": results[:5], "alerts": alerts}, f, indent=2)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", repr(e))
        raise
