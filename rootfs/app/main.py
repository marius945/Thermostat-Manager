#!/usr/bin/env python3
"""Thermostat Manager - Home Assistant Add-on for central thermostat control."""

import json
import os
import logging
from flask import Flask, render_template, request, jsonify
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Home Assistant Supervisor API
SUPERVISOR_URL = "http://supervisor/core/api"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "") or os.environ.get("HASSIO_TOKEN", "")

logger.info(f"SUPERVISOR_TOKEN gesetzt: {bool(SUPERVISOR_TOKEN)}")
logger.info(f"Verfügbare Token-Variablen: SUPERVISOR_TOKEN={'ja' if os.environ.get('SUPERVISOR_TOKEN') else 'nein'}, HASSIO_TOKEN={'ja' if os.environ.get('HASSIO_TOKEN') else 'nein'}")

# Persistence file for original temperatures
ORIGINALS_PATH = "/data/original_temps.json"


def ha_headers():
    """Get headers for Home Assistant API requests."""
    return {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }


def load_originals():
    """Load saved original temperatures from disk."""
    if os.path.exists(ORIGINALS_PATH):
        with open(ORIGINALS_PATH, "r") as f:
            return json.load(f)
    return None


def save_originals(originals):
    """Save original temperatures to disk."""
    os.makedirs(os.path.dirname(ORIGINALS_PATH), exist_ok=True)
    with open(ORIGINALS_PATH, "w") as f:
        json.dump(originals, f, indent=2)


def delete_originals():
    """Delete the saved original temperatures file."""
    if os.path.exists(ORIGINALS_PATH):
        os.remove(ORIGINALS_PATH)


def get_climate_entities():
    """Fetch all climate entities from Home Assistant."""
    try:
        resp = requests.get(
            f"{SUPERVISOR_URL}/states",
            headers=ha_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        states = resp.json()

        climate_entities = []
        for entity in states:
            if entity["entity_id"].startswith("climate."):
                attrs = entity.get("attributes", {})
                climate_entities.append({
                    "entity_id": entity["entity_id"],
                    "name": attrs.get("friendly_name", entity["entity_id"]),
                    "current_temperature": attrs.get("current_temperature"),
                    "target_temperature": attrs.get("temperature"),
                    "min_temp": attrs.get("min_temp", 5),
                    "max_temp": attrs.get("max_temp", 30),
                    "hvac_mode": entity.get("state", "unknown"),
                })
        return climate_entities
    except Exception as e:
        logger.error(f"Fehler beim Abrufen der Climate-Entities: {e}")
        return []


def set_temperature(entity_id, temperature):
    """Set the target temperature for a climate entity."""
    try:
        resp = requests.post(
            f"{SUPERVISOR_URL}/services/climate/set_temperature",
            headers=ha_headers(),
            json={
                "entity_id": entity_id,
                "temperature": temperature,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True, None
    except Exception as e:
        error_msg = f"Fehler bei {entity_id}: {e}"
        logger.error(error_msg)
        return False, error_msg


@app.route("/")
def index():
    """Render the main page."""
    return render_template("index.html")


@app.route("/api/thermostats")
def get_thermostats():
    """Get all climate entities with current and original temperatures."""
    entities = get_climate_entities()
    originals = load_originals()

    result = []
    for entity in entities:
        data = {
            "entity_id": entity["entity_id"],
            "name": entity["name"],
            "current_temperature": entity["current_temperature"],
            "target_temperature": entity["target_temperature"],
            "hvac_mode": entity["hvac_mode"],
        }
        if originals and entity["entity_id"] in originals:
            data["original_temperature"] = originals[entity["entity_id"]]
        result.append(data)

    return jsonify({"success": True, "thermostats": result})


@app.route("/api/apply_offset", methods=["POST"])
def apply_offset():
    """Apply a temperature offset to all thermostats."""
    data = request.json
    offset = float(data.get("offset", 0))

    if offset == 0:
        return jsonify({"success": False, "error": "Offset darf nicht 0 sein"})

    entities = get_climate_entities()
    originals = load_originals()

    # Only save originals on first application (protection against double offset)
    if originals is None:
        originals = {}
        for entity in entities:
            if entity["target_temperature"] is not None:
                originals[entity["entity_id"]] = entity["target_temperature"]
        save_originals(originals)

    errors = []
    applied = 0

    for entity in entities:
        if entity["target_temperature"] is None:
            continue

        new_temp = entity["target_temperature"] + offset
        # Clamp to thermostat min/max
        new_temp = max(entity["min_temp"], min(entity["max_temp"], new_temp))

        success, error = set_temperature(entity["entity_id"], new_temp)
        if success:
            applied += 1
        else:
            errors.append(error)

    if errors:
        return jsonify({
            "success": True,
            "message": f"Offset auf {applied} Thermostate angewendet, {len(errors)} Fehler",
            "errors": errors,
        })

    return jsonify({
        "success": True,
        "message": f"Offset {offset:+.1f}°C auf {applied} Thermostate angewendet",
    })


@app.route("/api/restore", methods=["POST"])
def restore():
    """Restore all thermostats to their original temperatures."""
    originals = load_originals()

    if originals is None:
        return jsonify({"success": False, "error": "Keine gespeicherten Originaltemperaturen vorhanden"})

    errors = []
    restored = 0

    for entity_id, temperature in originals.items():
        success, error = set_temperature(entity_id, temperature)
        if success:
            restored += 1
        else:
            errors.append(error)

    # Only delete file if all thermostats were restored successfully
    if not errors:
        delete_originals()
        return jsonify({
            "success": True,
            "message": f"{restored} Thermostate auf Originaltemperaturen zurückgesetzt",
        })

    return jsonify({
        "success": True,
        "message": f"{restored} wiederhergestellt, {len(errors)} Fehler",
        "errors": errors,
    })


@app.route("/api/status")
def status():
    """Check if original temperatures are saved."""
    originals = load_originals()
    return jsonify({
        "success": True,
        "offset_active": originals is not None,
        "saved_count": len(originals) if originals else 0,
    })


@app.route("/api/debug")
def debug():
    """Debug endpoint to check HA API connectivity."""
    # Show all environment variables containing TOKEN, HASSIO, SUPERVISOR
    env_vars = {}
    for key, value in os.environ.items():
        if any(k in key.upper() for k in ["TOKEN", "HASSIO", "SUPERVISOR", "HOME_ASSISTANT"]):
            env_vars[key] = value[:20] + "..." if len(value) > 20 else value

    info = {
        "supervisor_token_set": bool(SUPERVISOR_TOKEN),
        "supervisor_token_length": len(SUPERVISOR_TOKEN),
        "supervisor_url": SUPERVISOR_URL,
        "relevant_env_vars": env_vars,
    }
    try:
        resp = requests.get(
            f"{SUPERVISOR_URL}/states",
            headers=ha_headers(),
            timeout=10,
        )
        info["status_code"] = resp.status_code
        info["response_length"] = len(resp.text)
        if resp.status_code == 200:
            states = resp.json()
            info["total_entities"] = len(states)
            info["climate_entities"] = [
                e["entity_id"] for e in states if e["entity_id"].startswith("climate.")
            ]
        else:
            info["response_body"] = resp.text[:500]
    except Exception as e:
        info["error"] = str(e)
    return jsonify(info)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
