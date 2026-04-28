"""Flask dashboard for Thai digital asset exchanges (Bitkub, Binance TH)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify, render_template

from ingest import store

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/tickers")
def api_tickers():
    conn = store.get_conn()
    try:
        rows = store.latest_tickers(conn)
    finally:
        conn.close()
    return jsonify(rows)


@app.route("/api/depth/<venue>/<symbol>")
def api_depth(venue: str, symbol: str):
    conn = store.get_conn()
    try:
        d = store.latest_depth(conn, venue, symbol)
    finally:
        conn.close()
    if not d:
        return jsonify({"error": "no depth snapshot"}), 404
    return jsonify(d)


@app.route("/api/cross_venue/<base>")
def api_cross_venue(base: str):
    """Compare price for one base asset across venues (THB quote only)."""
    base = base.upper()
    venue_symbols = {
        "bitkub": f"{base}_THB",
        "binance_th": f"{base}THB",
        "upbit_th": f"THB-{base}",
        "bitazza": f"{base}_THB",
        "orbix_cg": f"{base}_THB",
    }
    conn = store.get_conn()
    try:
        rows = store.latest_tickers(conn)
    finally:
        conn.close()
    out: dict = {"base": base, "venues": {}}
    for r in rows:
        expected = venue_symbols.get(r["venue"])
        if expected and r["symbol"] == expected:
            out["venues"][r["venue"]] = r
    lasts = {v: t["last"] for v, t in out["venues"].items() if t.get("last")}
    if len(lasts) >= 2:
        mn, mx = min(lasts.values()), max(lasts.values())
        mid = (mn + mx) / 2
        out["max_spread_bps"] = round((mx - mn) / mid * 10000, 2)
    return jsonify(out)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5057, debug=True)
