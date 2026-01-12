import os
import json
import requests
from datetime import date, timedelta

DUFFEL_TOKEN = os.environ["DUFFEL_TOKEN"]
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")  # optional

# Your requirements
ORIGIN = "ORD"
DEST = "LON"      # If you want only Heathrow, change to "LHR"
CURRENCY = "USD"
THRESHOLD = 2500.00

START_DAYS_OUT = 14   # 2 weeks
END_DAYS_OUT = 28     # 4 weeks

MIN_TRIP_DAYS = 3
MAX_TRIP_DAYS = 14

HEADERS = {
    "Authorization": f"Bearer {DUFFEL_TOKEN}",
    "Duffel-Version": "beta",
    "Content-Type": "application/json",
}

def create_offer_request(origin, destination, depart_date, cabin_class):
    # Duffel: create offer request to search flights :contentReference[oaicite:5]{index=5}
    url = "https://api.duffel.com/air/offer_requests"
    payload = {
        "data": {
            "slices": [{"origin": origin, "destination": destination, "departure_date": depart_date.isoformat()}],
            "passengers": [{"type": "adult"}],
            "cabin_class": cabin_class,
            "return_offers": True,
        }
    }
    r = requests.post(url, headers=HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["data"]["id"]

def list_offers(offer_request_id, limit=30):
    url = "https://api.duffel.com/air/offers"
    params = {"offer_request_id": offer_request_id, "limit": limit}
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()["data"]

def cheapest_offer(offers):
    best = None
    for o in offers:
        if o.get("total_currency") != CURRENCY:
            continue
        amt = float(o["total_amount"])
        if best is None or amt < best["amount"]:
            best = {"amount": amt, "offer_id": o["id"]}
    return best

def notify_slack(text):
    if not SLACK_WEBHOOK_URL:
        return
    requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)

def main():
    today = date.today()
    start = today + timedelta(days=START_DAYS_OUT)
    end = today + timedelta(days=END_DAYS_OUT)

    one_way_cache = {}
    results = []
    alerts = []

    def best_one_way(origin, dest, d, cabin):
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
                }
                results.append(row)
                if total < THRESHOLD:
                    alerts.append(row)

            ret_date += timedelta(days=1)
        out_date += timedelta(days=1)

    results.sort(key=lambda x: x["total_usd"])

    # Print top options (shows up in GitHub Actions logs)
    print("Top 5 cheapest mixed-cabin combos:")
    for r in results[:5]:
        print(f'{r["out_date"]} → {r["ret_date"]} | '
              f'Out ${r["out_usd"]:.2f} + Back ${r["ret_usd"]:.2f} = ${r["total_usd"]:.2f}')

    if alerts:
        alerts.sort(key=lambda x: x["total_usd"])
        best = alerts[0]
        msg = (
            f"Deal found under ${THRESHOLD:.0f}!\n"
            f"ORD→LON (Business) {best['out_date']}: ${best['out_usd']:.2f}\n"
            f"LON→ORD (Prem Econ) {best['ret_date']}: ${best['ret_usd']:.2f}\n"
            f"Total: ${best['total_usd']:.2f}\n"
            f"Offer IDs: out {best['out_offer_id']} / back {best['ret_offer_id']}"
        )
        print(msg)
        notify_slack(msg)

    # Optional output file
    with open("latest_results.json", "w") as f:
        json.dump({"generated": today.isoformat(), "top5": results[:5], "alerts": alerts}, f, indent=2)

if __name__ == "__main__":
    main()
