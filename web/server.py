import logging
import os
import traceback
from flask import Flask, jsonify
import threading

from automation.worker import get_worker_status
from storage.db import db_status, telemetry_snapshot

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/")
def index():
    return "Bot running"


@app.route("/health")
def health():
    return _diagnostics_response()


@app.route("/diagnostics")
def diagnostics():
    return _diagnostics_response()


def _diagnostics_response():
    worker = get_worker_status()
    telegram = worker["telegram"]
    db = db_status()
    telemetry = telemetry_snapshot()
    payload = {
        "status": "ok" if telegram["connected"] else "degraded",
        "mongo": {
            "connected": True,
            "caches": {
                "settings": db["settings_cache"],
                "bots": db["bots_cache"],
                "groups": db["groups_cache"],
                "messages": db["messages_cache"],
            },
            "metrics": db["metrics"],
        },
        "telegram": telegram,
        "worker": {
            "running": worker["worker_running"],
            "paused": worker["worker_paused"],
        },
        "automation": {
            "running": worker["worker_running"],
            "paused": worker["worker_paused"],
        },
        "diagnostics": {
            "last_100_operations": telemetry["last_operations"],
            "average_latency_ms": {
                item["name"]: item["avg_ms"] for item in telemetry["operations"]
            },
            "slowest_operations_ms": [
                {"name": item["name"], "slowest_ms": item["slowest_ms"]}
                for item in telemetry["operations"][:10]
            ],
            "error_rates": {
                item["name"]: item["error_rate"] for item in telemetry["operations"]
            },
            "cache_statistics": {
                "settings": db["settings_cache"],
                "bots": db["bots_cache"],
                "groups": db["groups_cache"],
                "messages": db["messages_cache"],
                "entity_cache": telegram["entity_cache_size"],
            },
            "summary": telemetry["summary"],
        },
    }
    return jsonify(payload), (200 if payload["status"] == "ok" else 503)


def run_server(host="0.0.0.0", port=5000):
    try:
        logger.info(f"Starting Flask server on {host}:{port}")
        app.run(host=host, port=port, debug=False, use_reloader=False)
    except Exception:
        print("FATAL ERROR: Flask server failed to start")
        print(traceback.format_exc())
        logger.exception("Flask server failed to start")
        raise


def run_flask_in_thread(host="0.0.0.0", port=5000):
    port = int(os.getenv("PORT", port) or port)
    logger.info("Preparing Flask thread for host=%s port=%s", host, port)
    server_thread = threading.Thread(target=run_server, args=(host, port), daemon=True)
    server_thread.start()
    logger.info("Flask server started in background thread")
    return server_thread
