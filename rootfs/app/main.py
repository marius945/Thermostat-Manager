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


def find_supervisor_token():
    """Find the Supervisor token from environment or file system."""
    # 1. Environment variables
    token = os.environ.get("SUPERVISOR_TOKEN", "") or os.environ.get("HASSIO_TOKEN", "")
    if token:
        logger.info("Token aus Umgebungsvariable geladen")
        return token

    # 2. S6 overlay container environment
    token_paths = [
        "/run/s6/container_environment/SUPERVISOR_TOKEN",
        "/run/s6/container_environment/HASSIO_TOKEN",
        "/config/token",
        "/data/token",
    ]
    for path in token_paths:
        if os.path.isfile(path):
            with open(path, "r") as f:
                token = f.read().strip()
            if token:
                logger.info(f"Token aus {path} geladen")
                return token

    logger.warning("Kein Supervisor-Token gefunden!")
    return ""


SUPERVISOR_TOKEN = find_supervisor_token()

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
    """Apply a temperature offset to selected thermostats."""
    data = request.json
    offset = float(data.get("offset", 0))
    selected_ids = data.get("entity_ids", None)

    if offset == 0:
        return jsonify({"success": False, "error": "Offset darf nicht 0 sein"})

    entities = get_climate_entities()
    originals = load_originals()

    # Filter to selected entities if provided
    if selected_ids:
        selected_set = set(selected_ids)
        target_entities = [e for e in entities if e["entity_id"] in selected_set]
    else:
        target_entities = entities

    if not target_entities:
        return jsonify({"success": False, "error": "Keine Thermostate ausgewählt"})

    # Save originals (merge with existing if already present)
    if originals is None:
        originals = {}
    for entity in target_entities:
        if entity["target_temperature"] is not None and entity["entity_id"] not in originals:
            originals[entity["entity_id"]] = entity["target_temperature"]
    save_originals(originals)

    errors = []
    applied = 0

    for entity in target_entities:
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


@app.route("/api/set_temperature", methods=["POST"])
def set_absolute_temperature():
    """Set an absolute temperature on selected thermostats."""
    data = request.json
    temperature = data.get("temperature")
    selected_ids = data.get("entity_ids", None)

    if temperature is None:
        return jsonify({"success": False, "error": "Keine Temperatur angegeben"})

    temperature = float(temperature)
    entities = get_climate_entities()

    # Filter to selected entities if provided
    if selected_ids:
        selected_set = set(selected_ids)
        target_entities = [e for e in entities if e["entity_id"] in selected_set]
    else:
        target_entities = entities

    if not target_entities:
        return jsonify({"success": False, "error": "Keine Thermostate ausgewählt"})

    # Save originals before changing (merge with existing)
    originals = load_originals() or {}
    for entity in target_entities:
        if entity["target_temperature"] is not None and entity["entity_id"] not in originals:
            originals[entity["entity_id"]] = entity["target_temperature"]
    save_originals(originals)

    errors = []
    applied = 0

    for entity in target_entities:
        new_temp = max(entity["min_temp"], min(entity["max_temp"], temperature))
        success, error = set_temperature(entity["entity_id"], new_temp)
        if success:
            applied += 1
        else:
            errors.append(error)

    if errors:
        return jsonify({
            "success": True,
            "message": f"{applied} Thermostate auf {temperature:.1f}°C gesetzt, {len(errors)} Fehler",
            "errors": errors,
        })

    return jsonify({
        "success": True,
        "message": f"{applied} Thermostate auf {temperature:.1f}°C gesetzt",
    })


@app.route("/api/restore", methods=["POST"])
def restore():
    """Restore selected or all thermostats to their original temperatures."""
    data = request.json or {}
    selected_ids = data.get("entity_ids", None)
    originals = load_originals()

    if originals is None:
        return jsonify({"success": False, "error": "Keine gespeicherten Originaltemperaturen vorhanden"})

    # Determine which to restore
    if selected_ids:
        to_restore = {eid: temp for eid, temp in originals.items() if eid in selected_ids}
    else:
        to_restore = originals

    errors = []
    restored = 0

    for entity_id, temperature in to_restore.items():
        success, error = set_temperature(entity_id, temperature)
        if success:
            restored += 1
        else:
            errors.append(error)

    # Remove restored entries from originals
    if not errors:
        for eid in to_restore:
            originals.pop(eid, None)
        if originals:
            save_originals(originals)
        else:
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
    # Show ALL environment variable names
    all_env_names = sorted(os.environ.keys())

    # Check known file paths
    check_paths = [
        "/run/s6/container_environment/",
        "/run/s6/",
        "/run/",
        "/config/",
        "/data/",
    ]
    found_paths = {}
    for p in check_paths:
        if os.path.isdir(p):
            try:
                found_paths[p] = os.listdir(p)
            except Exception:
                found_paths[p] = "Permission denied"
        else:
            found_paths[p] = "Not found"

    info = {
        "supervisor_token_set": bool(SUPERVISOR_TOKEN),
        "supervisor_token_length": len(SUPERVISOR_TOKEN),
        "supervisor_url": SUPERVISOR_URL,
        "all_env_var_names": all_env_names,
        "filesystem_check": found_paths,
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
