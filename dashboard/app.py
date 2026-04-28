"""Flask dashboard for Thai digital asset exchanges."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify, render_template

from dashboard import insights
from ingest import store
from ingest.adapters import binance_th as adapter_binance_th
from ingest.adapters import bitkub as adapter_bitkub
from ingest.adapters import coingecko as adapter_coingecko
from ingest.adapters import upbit_th as adapter_upbit_th


VENUE_KLINE_ADAPTERS = {
    "bitkub": (adapter_bitkub, lambda base: f"{base}_THB"),
    "binance_th": (adapter_binance_th, lambda base: f"{base}THB"),
    "upbit_th": (adapter_upbit_th, lambda base: f"THB-{base}"),
}

PROJECT_ROOT = Path(__file__).parent.parent
TOKENX_FILE = PROJECT_ROOT / "data" / "tokenx_manual.json"

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


@app.route("/api/tokenx")
def api_tokenx():
    """Manually maintained tokenized-securities data (no public API for ERX/Token X)."""
    if not TOKENX_FILE.exists():
        return jsonify({"error": "tokenx_manual.json not found", "tokens": []}), 404
    with TOKENX_FILE.open() as f:
        return jsonify(json.load(f))


@app.route("/api/insights")
def api_insights():
    conn = store.get_conn()
    try:
        rows = store.latest_tickers(conn)
        depth_rows = conn.execute("""
            SELECT d.venue, d.symbol, d.bids_json, d.asks_json
            FROM depth_snapshots d
            JOIN (
                SELECT venue, symbol, MAX(ts) AS max_ts FROM depth_snapshots
                GROUP BY venue, symbol
            ) m ON m.venue = d.venue AND m.symbol = d.symbol AND m.max_ts = d.ts
        """).fetchall()
    finally:
        conn.close()

    imbalances = []
    for venue, symbol, bids_json, asks_json in depth_rows:
        imb = insights.depth_imbalance(bids_json, asks_json)
        if imb is not None:
            imbalances.append({"venue": venue, "symbol": symbol, **imb})
    imbalances.sort(key=lambda r: abs(r["imbalance"]), reverse=True)

    return jsonify({
        "cross_venue_spreads": insights.cross_venue_summary(rows),
        "top_turnover": insights.top_turnover(rows, 20),
        "top_movers": insights.top_movers(rows, 10),
        "venue_concentration": insights.venue_concentration(rows),
        "depth_imbalance": imbalances[:15],
    })


@app.route("/symbol/<base>")
def symbol_detail(base: str):
    return render_template("symbol.html", base=base.upper())


@app.route("/api/klines/<venue>/<base>")
def api_klines(venue: str, base: str):
    """OHLCV bars for one (venue, base) pair. Query: interval (default 1h), limit (default 200)."""
    from flask import request
    if venue not in VENUE_KLINE_ADAPTERS:
        return jsonify({"error": f"venue {venue} has no kline source"}), 404
    adapter, sym_fn = VENUE_KLINE_ADAPTERS[venue]
    interval = request.args.get("interval", "1h")
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    try:
        bars = adapter.fetch_klines(sym_fn(base.upper()), interval=interval, limit=limit)
    except Exception as e:
        return jsonify({"error": str(e), "bars": []}), 502
    return jsonify({
        "venue": venue,
        "base": base.upper(),
        "interval": interval,
        "bars": bars,
    })


@app.route("/api/volume_chart/<venue_label>")
def api_volume_chart(venue_label: str):
    """Daily exchange volume series for CoinGecko-backed venues. Query: days (default 14)."""
    from flask import request
    cg_id = adapter_coingecko.EXCHANGE_IDS.get(venue_label)
    if not cg_id:
        return jsonify({"error": f"{venue_label} is not a CoinGecko-backed venue"}), 404
    try:
        days = int(request.args.get("days", 14))
    except (TypeError, ValueError):
        days = 14
    try:
        series = adapter_coingecko.fetch_volume_chart(cg_id, days=days)
    except Exception as e:
        return jsonify({"error": str(e), "series": []}), 502
    return jsonify({"venue": venue_label, "cg_id": cg_id, "days": days, "series": series})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5057, debug=True)
