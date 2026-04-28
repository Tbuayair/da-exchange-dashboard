"""Top-level shim — re-exports for `pip install git+...da-exchange-dashboard`.

After install, host apps can do:
    from da_exchange_dashboard import create_blueprint
    app.register_blueprint(create_blueprint(), url_prefix="/da")
"""
from dashboard.app import create_app, create_blueprint

__all__ = ["create_app", "create_blueprint"]
