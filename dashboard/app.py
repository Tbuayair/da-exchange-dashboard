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
    bitkub_sym = f"{base}_THB"
    binance_sym = f"{base}THB"
    conn = store.get_conn()
    try:
        rows = store.latest_tickers(conn)
    finally:
        conn.close()
    out = {"base": base, "venues": {}}
    for r in rows:
        if r["venue"] == "bitkub" and r["symbol"] == bitkub_sym:
            out["venues"]["bitkub"] = r
        elif r["venue"] == "binance_th" and r["symbol"] == binance_sym:
            out["venues"]["binance_th"] = r
    if len(out["venues"]) == 2:
        b = out["venues"]["bitkub"]
        bn = out["venues"]["binance_th"]
        if b.get("last") and bn.get("last"):
            mid = (b["last"] + bn["last"]) / 2
            out["spread_bps"] = round((b["last"] - bn["last"]) / mid * 10000, 2)
    return jsonify(out)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5057, debug=True)
