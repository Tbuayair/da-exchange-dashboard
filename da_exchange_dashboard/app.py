"""Flask Blueprint + standalone app for the InnovestX DA-Exchange dashboard.

Two integration modes:

  1) Standalone (local dev / standalone Koyeb service):
        gunicorn -w 1 wsgi:app
     The blueprint mounts at "/" so URLs are /, /symbol/<base>, /api/...

  2) Mounted under a parent Flask app (e.g. invxips-dashboard at /da):
        from da_exchange_dashboard import create_blueprint
        app.register_blueprint(create_blueprint(), url_prefix="/da")
     URLs become /da/, /da/symbol/<base>, /da/api/...

Templates use url_for('da.<endpoint>') for all internal links so they
resolve correctly under either mode. JS reads URL_PREFIX from the rendered
template to build fetch URLs.
"""
import json
import os
from pathlib import Path

from flask import Blueprint, Flask, jsonify, render_template, request

from . import insights
from .ingest import store
from .ingest.adapters import binance_th as adapter_binance_th
from .ingest.adapters import bitkub as adapter_bitkub
from .ingest.adapters import coingecko as adapter_coingecko
from .ingest.adapters import upbit_th as adapter_upbit_th


VENUE_KLINE_ADAPTERS = {
    "bitkub": (adapter_bitkub, lambda base: f"{base}_THB"),
    "binance_th": (adapter_binance_th, lambda base: f"{base}THB"),
    "upbit_th": (adapter_upbit_th, lambda base: f"THB-{base}"),
}

PACKAGE_ROOT = Path(__file__).parent
PROJECT_ROOT = PACKAGE_ROOT.parent
DEFAULT_TOKENX_FILE = PACKAGE_ROOT / "data" / "tokenx_manual.json"


def _tokenx_path() -> Path:
    """Resolve tokenx data file, allowing override via env var."""
    override = os.environ.get("DA_TOKENX_FILE")
    if override:
        return Path(override)
    if DEFAULT_TOKENX_FILE.exists():
        return DEFAULT_TOKENX_FILE
    legacy = PROJECT_ROOT / "data" / "tokenx_manual.json"
    return legacy


def create_blueprint(name: str = "da") -> Blueprint:
    """Build the DA-Exchange Blueprint. Endpoint names are prefixed with `name.`."""
    bp = Blueprint(
        name,
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )

    @bp.route("/")
    def index():
        return render_template("index.html", bp_name=name)

    @bp.route("/symbol/<base>")
    def symbol_detail(base: str):
        return render_template("symbol.html", base=base.upper(), bp_name=name)

    @bp.route("/api/tickers")
    def api_tickers():
        conn = store.get_conn()
        try:
            rows = store.latest_tickers(conn)
        finally:
            conn.close()
        return jsonify(rows)

    @bp.route("/api/depth/<venue>/<symbol>")
    def api_depth(venue: str, symbol: str):
        conn = store.get_conn()
        try:
            d = store.latest_depth(conn, venue, symbol)
        finally:
            conn.close()
        if not d:
            return jsonify({"error": "no depth snapshot"}), 404
        return jsonify(d)

    @bp.route("/api/cross_venue/<base>")
    def api_cross_venue(base: str):
        base = base.upper()
        venue_symbols = {
            "bitkub": f"{base}_THB",
            "binance_th": f"{base}THB",
            "upbit_th": f"THB-{base}",
            "bitazza": f"{base}_THB",
            "orbix_cg": f"{base}_THB",
            "innovestx": f"{base}THB",
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

    @bp.route("/api/tokenx")
    def api_tokenx():
        path = _tokenx_path()
        if not path.exists():
            return jsonify({"error": f"{path.name} not found", "tokens": []}), 404
        with path.open() as f:
            return jsonify(json.load(f))

    @bp.route("/api/insights")
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

    @bp.route("/api/klines/<venue>/<base>")
    def api_klines(venue: str, base: str):
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

    @bp.route("/api/daily_turnover")
    def api_daily_turnover():
        venue = request.args.get("venue") or None
        symbol = request.args.get("symbol") or None
        try:
            days = int(request.args.get("days", 30))
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(days, 365))
        conn = store.get_conn()
        try:
            rows = store.daily_turnover_history(conn, venue=venue, symbol=symbol, days=days)
        finally:
            conn.close()
        return jsonify({
            "venue": venue,
            "symbol": symbol,
            "days": days,
            "rows": rows,
        })

    @bp.route("/api/volume_chart/<venue_label>")
    def api_volume_chart(venue_label: str):
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

    return bp


def create_app() -> Flask:
    """Standalone Flask app — used by wsgi.py for direct deployments.

    static_folder=None disables Flask's app-level /static so the blueprint can
    own /static cleanly. Templates already reference {{ url_for('da.static', ...) }}.
    """
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(create_blueprint("da"))
    return app


# Module-level app for compatibility with `flask run` and existing entrypoints.
app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5057, debug=True)
