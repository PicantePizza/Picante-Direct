"""Picante Gourmet Pizza + Picante Direct — new application.

The WhatsApp Cloud API connection is supplied through the existing environment
variables. No external customer checkout or customer-facing order web page exists.
"""
from __future__ import annotations

import hashlib
from html import escape as html_escape
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request, url_for

# ---------------------------------------------------------------------------
# Application and preserved WhatsApp connection configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "picante_verify_2026")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v25.0")
DRIVER_GROUP_ID = os.getenv("DRIVER_GROUP_ID", "").strip()

# These are application settings, not new WhatsApp credentials.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "pgp_picante_direct.sqlite3")
APP_SECRET = os.getenv("APP_SECRET", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
PICKUP_MAPS_URL = "https://www.google.com/maps/search/?api=1&query=20.7152721%2C-105.2740877"
PGP_ADDRESS_EN = "110 Valle de los Reyes, Puerto Vallarta, Jalisco, Mexico"
PGP_ADDRESS_ES = "110 Valle de los Reyes, Puerto Vallarta, Jalisco, México"
PGP_WEBSITE_INFO = "Picante Gourmet Pizza — open 24/7, freshly baked in approximately 3 minutes."
BUSINESS_PHONE = "322-240-0147"
BUSINESS_TIMEZONE = ZoneInfo("America/Mazatlan")
PIZZA_PRICE_MXN = 175
DAY_DELIVERY_FEE_MXN = 50
OVERNIGHT_DELIVERY_FEE_MXN = 100
NO_DRIVER_MINUTES = 5
GUARANTEE_MINUTES = 30
MAX_UNSETTLED_DELIVERIES_PER_DRIVER = 2

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("pgp_picante_direct")

MENU = [
    {"id": "pepperoni", "en": "Pepperoni", "es": "Pepperoni"},
    {"id": "hawaiian", "en": "Hawaiian", "es": "Hawaiana"},
    {
        "id": "bacon_mushroom_onion_jalapeno",
        "en": "Bacon, Mushroom, Onion & Jalapeño",
        "es": "Tocino, Champiñón, Cebolla y Jalapeño",
    },
    {"id": "margherita", "en": "Margherita", "es": "Margarita"},
    {"id": "meat_lovers", "en": "Meat Lovers", "es": "Meat Lovers"},
    {"id": "four_cheese", "en": "Four Cheese", "es": "Cuatro Quesos"},
]
MENU_BY_ID = {item["id"]: item for item in MENU}

# State is persisted in the database. It is not held only in worker memory.

# ---------------------------------------------------------------------------
# Database layer: namespaced tables avoid colliding with any previous schema.
# ---------------------------------------------------------------------------

def _is_postgres() -> bool:
    return bool(DATABASE_URL)


@contextmanager
def db_connection(immediate: bool = False):
    """Yield a transaction and always close the connection."""
    if _is_postgres():
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as exc:  # pragma: no cover - deployment configuration
            raise RuntimeError("DATABASE_URL is set but psycopg2 is not installed") from exc
        connection = psycopg2.connect(DATABASE_URL)
        connection.autocommit = False
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    else:
        connection = sqlite3.connect(DATABASE_PATH, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _sql(sql: str) -> str:
    return sql.replace("?", "%s") if _is_postgres() else sql


def execute(connection, sql: str, params: Tuple[Any, ...] = ()):
    if _is_postgres():
        import psycopg2.extras
        cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(_sql(sql), params)
        return cursor
    return connection.execute(sql, params)


def fetch_one(connection, sql: str, params: Tuple[Any, ...] = ()):
    return execute(connection, sql, params).fetchone()


def fetch_all(connection, sql: str, params: Tuple[Any, ...] = ()):
    return execute(connection, sql, params).fetchall()


def row_dict(row) -> Optional[dict]:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return dict(row)


def row_value(row, key: str, default=None):
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _create_table_sqlite(connection, statements):
    for statement in statements:
        connection.execute(statement)


def init_db() -> None:
    if _is_postgres():
        statements = [
            """CREATE TABLE IF NOT EXISTS pgp_customers(
                phone TEXT PRIMARY KEY, language TEXT NOT NULL DEFAULT 'en',
                name TEXT, address TEXT, instructions TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_sessions(
                phone TEXT PRIMARY KEY, state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_drivers(
                phone TEXT PRIMARY KEY, name TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'en',
                approved INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                busy INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_orders(
                id BIGSERIAL PRIMARY KEY, ref TEXT UNIQUE NOT NULL,
                customer_phone TEXT NOT NULL, customer_name TEXT NOT NULL,
                language TEXT NOT NULL, address TEXT NOT NULL, instructions TEXT,
                period TEXT NOT NULL, pizza_total_mxn INTEGER NOT NULL,
                delivery_fee_mxn INTEGER NOT NULL, total_mxn INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN', driver_phone TEXT, driver_name TEXT,
                tracking_url TEXT, created_at TEXT NOT NULL, dispatch_at TEXT NOT NULL,
                claimed_at TEXT, guarantee_accepted_at TEXT, guarantee_deadline TEXT,
                no_driver_notice_at TEXT, no_driver_choice TEXT,
                delivered_at TEXT, cancelled_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS pgp_order_items(
                id BIGSERIAL PRIMARY KEY, order_id BIGINT NOT NULL REFERENCES pgp_orders(id) ON DELETE CASCADE,
                pizza_id TEXT NOT NULL, pizza_name TEXT NOT NULL, quantity INTEGER NOT NULL,
                unit_price_mxn INTEGER NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_deliveries(
                id BIGSERIAL PRIMARY KEY, order_id BIGINT NOT NULL UNIQUE REFERENCES pgp_orders(id),
                driver_phone TEXT NOT NULL, customer_cash_mxn INTEGER NOT NULL DEFAULT 0,
                pizza_total_mxn INTEGER NOT NULL DEFAULT 0, delivery_fee_mxn INTEGER NOT NULL DEFAULT 0,
                tip_mxn INTEGER NOT NULL DEFAULT 0, amount_due_office_mxn INTEGER NOT NULL DEFAULT 0,
                machine_purchase_mxn INTEGER NOT NULL DEFAULT 0, company_card TEXT,
                transaction_reference TEXT, settlement_id BIGINT, completed_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_settlements(
                id BIGSERIAL PRIMARY KEY, driver_phone TEXT NOT NULL, delivery_count INTEGER NOT NULL,
                amount_mxn INTEGER NOT NULL, note TEXT, recorded_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_card_transactions(
                id BIGSERIAL PRIMARY KEY, driver_phone TEXT NOT NULL, order_ref TEXT NOT NULL,
                card_label TEXT NOT NULL, amount_mxn INTEGER NOT NULL,
                reference TEXT, recorded_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_processed_messages(
                message_id TEXT PRIMARY KEY, processed_at TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_pgp_orders_customer ON pgp_orders(customer_phone,id)",
            "CREATE INDEX IF NOT EXISTS idx_pgp_orders_dispatch ON pgp_orders(status,dispatch_at,no_driver_notice_at)",
            "CREATE INDEX IF NOT EXISTS idx_pgp_order_items_order ON pgp_order_items(order_id,id)",
            "CREATE INDEX IF NOT EXISTS idx_pgp_deliveries_driver ON pgp_deliveries(driver_phone,settlement_id)",
        ]
        with db_connection() as connection:
            for statement in statements:
                execute(connection, statement)
    else:
        statements = [
            """CREATE TABLE IF NOT EXISTS pgp_customers(
                phone TEXT PRIMARY KEY, language TEXT NOT NULL DEFAULT 'en',
                name TEXT, address TEXT, instructions TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_sessions(
                phone TEXT PRIMARY KEY, state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_drivers(
                phone TEXT PRIMARY KEY, name TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'en',
                approved INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                busy INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE NOT NULL,
                customer_phone TEXT NOT NULL, customer_name TEXT NOT NULL,
                language TEXT NOT NULL, address TEXT NOT NULL, instructions TEXT,
                period TEXT NOT NULL, pizza_total_mxn INTEGER NOT NULL,
                delivery_fee_mxn INTEGER NOT NULL, total_mxn INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN', driver_phone TEXT, driver_name TEXT,
                tracking_url TEXT, created_at TEXT NOT NULL, dispatch_at TEXT NOT NULL,
                claimed_at TEXT, guarantee_accepted_at TEXT, guarantee_deadline TEXT,
                no_driver_notice_at TEXT, no_driver_choice TEXT,
                delivered_at TEXT, cancelled_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS pgp_order_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL REFERENCES pgp_orders(id) ON DELETE CASCADE,
                pizza_id TEXT NOT NULL, pizza_name TEXT NOT NULL, quantity INTEGER NOT NULL,
                unit_price_mxn INTEGER NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_deliveries(
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL UNIQUE REFERENCES pgp_orders(id),
                driver_phone TEXT NOT NULL, customer_cash_mxn INTEGER NOT NULL DEFAULT 0,
                pizza_total_mxn INTEGER NOT NULL DEFAULT 0, delivery_fee_mxn INTEGER NOT NULL DEFAULT 0,
                tip_mxn INTEGER NOT NULL DEFAULT 0, amount_due_office_mxn INTEGER NOT NULL DEFAULT 0,
                machine_purchase_mxn INTEGER NOT NULL DEFAULT 0, company_card TEXT,
                transaction_reference TEXT, settlement_id INTEGER, completed_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_settlements(
                id INTEGER PRIMARY KEY AUTOINCREMENT, driver_phone TEXT NOT NULL, delivery_count INTEGER NOT NULL,
                amount_mxn INTEGER NOT NULL, note TEXT, recorded_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_card_transactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, driver_phone TEXT NOT NULL, order_ref TEXT NOT NULL,
                card_label TEXT NOT NULL, amount_mxn INTEGER NOT NULL,
                reference TEXT, recorded_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS pgp_processed_messages(
                message_id TEXT PRIMARY KEY, processed_at TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_pgp_orders_customer ON pgp_orders(customer_phone,id)",
            "CREATE INDEX IF NOT EXISTS idx_pgp_orders_dispatch ON pgp_orders(status,dispatch_at,no_driver_notice_at)",
            "CREATE INDEX IF NOT EXISTS idx_pgp_order_items_order ON pgp_order_items(order_id,id)",
            "CREATE INDEX IF NOT EXISTS idx_pgp_deliveries_driver ON pgp_deliveries(driver_phone,settlement_id)",
        ]
        with db_connection() as connection:
            _create_table_sqlite(connection, statements)


init_db()

# ---------------------------------------------------------------------------
# General helpers and persistent state
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds")


def local_period_and_fee(at: Optional[datetime] = None) -> Tuple[str, int]:
    local = (at or now_utc()).astimezone(BUSINESS_TIMEZONE)
    if 6 <= local.hour <= 23:
        return "day", DAY_DELIVERY_FEE_MXN
    return "overnight", OVERNIGHT_DELIVERY_FEE_MXN


def new_order_reference(connection) -> str:
    for _ in range(100):
        ref = "PD-" + secrets.token_hex(3).upper()
        if not fetch_one(connection, "SELECT id FROM pgp_orders WHERE ref=?", (ref,)):
            return ref
    raise RuntimeError("Unable to allocate a unique delivery reference")


def normalize_phone(phone: str) -> str:
    return re.sub(r"\D", "", str(phone or ""))


def customer_get(phone: str) -> Optional[dict]:
    with db_connection() as connection:
        return row_dict(fetch_one(connection, "SELECT * FROM pgp_customers WHERE phone=?", (phone,)))


def customer_save(phone: str, *, language=None, name=None, address=None, instructions=None) -> None:
    current = customer_get(phone) or {}
    lang = language if language in ("en", "es") else current.get("language", "en")
    values = {
        "name": name if name is not None else current.get("name"),
        "address": address if address is not None else current.get("address"),
        "instructions": instructions if instructions is not None else current.get("instructions"),
    }
    ts = iso_now()
    with db_connection() as connection:
        exists = fetch_one(connection, "SELECT phone FROM pgp_customers WHERE phone=?", (phone,))
        if exists:
            execute(connection,
                "UPDATE pgp_customers SET language=?,name=?,address=?,instructions=?,updated_at=? WHERE phone=?",
                (lang, values["name"], values["address"], values["instructions"], ts, phone))
        else:
            execute(connection,
                "INSERT INTO pgp_customers(phone,language,name,address,instructions,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (phone, lang, values["name"], values["address"], values["instructions"], ts, ts))


def session_get(phone: str) -> dict:
    with db_connection() as connection:
        row = fetch_one(connection, "SELECT state_json FROM pgp_sessions WHERE phone=?", (phone,))
    if row:
        try:
            return json.loads(row_value(row, "state_json", "{}"))
        except (TypeError, json.JSONDecodeError):
            log.exception("Invalid session state for %s", phone)
    customer = customer_get(phone)
    return {"stage": "APP_HOME" if customer else "LANGUAGE", "language": (customer or {}).get("language", "en"), "cart": []}


def session_save(phone: str, state: dict) -> None:
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    ts = iso_now()
    with db_connection() as connection:
        exists = fetch_one(connection, "SELECT phone FROM pgp_sessions WHERE phone=?", (phone,))
        if exists:
            execute(connection, "UPDATE pgp_sessions SET state_json=?,updated_at=? WHERE phone=?", (payload, ts, phone))
        else:
            execute(connection, "INSERT INTO pgp_sessions(phone,state_json,updated_at) VALUES(?,?,?)", (phone, payload, ts))


def session_reset(phone: str, language: Optional[str] = None) -> dict:
    customer = customer_get(phone) or {}
    state = {"stage": "APP_HOME", "language": language or customer.get("language", "en"), "cart": []}
    session_save(phone, state)
    return state


def order_get(ref: str, include_items: bool = True) -> Optional[dict]:
    with db_connection() as connection:
        order = row_dict(fetch_one(connection, "SELECT * FROM pgp_orders WHERE ref=?", (ref.upper(),)))
        if not order:
            return None
        if include_items:
            rows = fetch_all(connection, "SELECT * FROM pgp_order_items WHERE order_id=? ORDER BY id", (order["id"],))
            order["items"] = [row_dict(item) for item in rows]
    return order


def order_items_text(order: dict, language: str) -> str:
    return "\n".join(f"{item['quantity']} × {item['pizza_name']}" for item in order.get("items", []))


def is_valid_maps_link(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme == "https" and parsed.netloc.lower() in {
            "maps.google.com", "www.google.com", "google.com", "maps.app.goo.gl", "goo.gl"
        }
    except Exception:
        return False

# ---------------------------------------------------------------------------
# WhatsApp Cloud API functions — same existing connection, no new setup
# ---------------------------------------------------------------------------

def wa_request(payload: dict) -> bool:
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        log.error("WhatsApp is not configured: existing WHATSAPP_TOKEN/PHONE_NUMBER_ID missing")
        return False
    endpoint = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=20)
        if not response.ok:
            log.error("WhatsApp send failed: %s %s", response.status_code, response.text[:1000])
        return response.ok
    except requests.RequestException:
        log.exception("WhatsApp API request failed")
        return False


def normalize_recipient(to: str) -> str:
    # DRIVER_GROUP_ID is a configured WhatsApp group identifier, not a phone number.
    if DRIVER_GROUP_ID and str(to) == DRIVER_GROUP_ID:
        return str(to)
    return normalize_phone(to)


def send_text(to: str, body: str) -> bool:
    return wa_request({
        "messaging_product": "whatsapp", "to": normalize_recipient(to), "type": "text",
        "text": {"preview_url": False, "body": body},
    })


def send_buttons(to: str, body: str, buttons: list, footer: Optional[str] = None) -> bool:
    """Send up to three WhatsApp quick-reply buttons; text fallback if rejected."""
    interactive = {
        "type": "button", "body": {"text": body},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": str(item[0])[:256], "title": str(item[1])[:20]}}
            for item in buttons[:3]
        ]},
    }
    if footer:
        interactive["footer"] = {"text": footer[:60]}
    ok = wa_request({"messaging_product": "whatsapp", "to": normalize_recipient(to), "type": "interactive", "interactive": interactive})
    if not ok:
        # Text fallback uses the visible action labels instead of new numbers,
        # because numeric replies already have meanings in the PGP menus/cart.
        fallback = body + "\n\n" + "\n".join(f"Reply *{label}*" for _, label in buttons[:3])
        return send_text(to, fallback)
    return True


def send_list(to: str, body: str, button_label: str, rows: list, *, section_title: str = "Options") -> bool:
    """Send a WhatsApp list with up to ten rows, falling back to numbered text."""
    interactive = {
        "type": "list", "body": {"text": body},
        "action": {"button": button_label[:20], "sections": [{
            "title": section_title[:24],
            "rows": [
                {"id": str(row[0])[:200], "title": str(row[1])[:24], "description": str(row[2])[:72]}
                for row in rows[:10]
            ],
        }]},
    }
    ok = wa_request({"messaging_product": "whatsapp", "to": normalize_recipient(to), "type": "interactive", "interactive": interactive})
    if not ok:
        fallback = body + "\n\n" + "\n".join(f"{i+1}. {row[1]}" for i, row in enumerate(rows[:10]))
        return send_text(to, fallback)
    return True

# ---------------------------------------------------------------------------
# Bilingual PGP customer experience
# ---------------------------------------------------------------------------

def language_prompt(phone: str) -> None:
    current = session_get(phone)
    if current.get("stage") == "LANGUAGE":
        state = current
    else:
        # Save the in-progress state so changing language does not erase a cart,
        # address, quote or other checkout step.
        resume = {key: value for key, value in current.items() if key != "resume_state"}
        state = {
            "stage": "LANGUAGE",
            "language": current.get("language", "en"),
            "cart": current.get("cart", []),
            "resume_state": resume,
        }
    session_save(phone, state)
    send_buttons(phone,
        "🍕 *PICANTE GOURMET PIZZA*\nOpen 24/7 • Fresh baked in approximately 3 minutes\n\nChoose your language / Elige tu idioma:",
        [("LANG_EN", "🇨🇦 ENGLISH"), ("LANG_ES", "🇲🇽 ESPAÑOL")])


def resume_after_language_change(phone: str, language: str, resume: Optional[dict]) -> None:
    if not resume:
        app_home(phone, language)
        return
    resume["language"] = language
    session_save(phone, resume)
    stage = resume.get("stage", "APP_HOME")
    if stage == "APP_HOME":
        app_home(phone, language, welcome=False)
    elif stage == "PGP_MENU":
        numbered_pgp_menu(phone, language)
    elif stage == "PIZZA":
        send_pizza_selection(phone, language)
    elif stage == "QUANTITY":
        pizza = MENU_BY_ID.get(resume.get("pending_pizza_id"), {})
        name = pizza.get(language, "")
        send_text(phone, (f"¿Cuántas pizzas de {name} deseas?" if language == "es" else f"How many {name} pizzas would you like?"))
    elif stage == "NAME_CONFIRM":
        _ask_customer_name(phone, resume)
    elif stage == "NAME":
        send_text(phone, "¿Cuál es tu nombre?" if language == "es" else "What is your name?")
    elif stage in {"ADDRESS_CONFIRM", "ADDRESS"}:
        _ask_address(phone, resume) if stage == "ADDRESS_CONFIRM" else send_text(
            "Escribe tu dirección completa o comparte tu ubicación." if language == "es" else
            "Enter your full address or share your WhatsApp location.")
    elif stage in {"INSTRUCTIONS_CONFIRM", "INSTRUCTIONS"}:
        _ask_instructions(phone, resume) if stage == "INSTRUCTIONS_CONFIRM" else send_text(
            "Escribe las instrucciones o responde *NINGUNA*." if language == "es" else
            "Enter special delivery instructions or reply *NONE*.")
    elif stage == "CONFIRM":
        build_confirmation(phone, resume)
    else:
        app_home(phone, language, welcome=False)


def app_home(phone: str, language: str, *, welcome: bool = True) -> None:
    state = {"stage": "APP_HOME", "language": language, "cart": []}
    session_save(phone, state)
    if language == "es":
        body = ("🍕 *PICANTE GOURMET PIZZA*\n\n" + ("¡Bienvenido! ¿Cómo podemos ayudarte?" if welcome else "¿Qué deseas hacer?") +
                "\n\nSelecciona una opción. También puedes responder 1–5.\nEscribe *PGP MENU* para abrir el menú principal numerado.\nEscribe *HISTORIAL* para ver tus pedidos o *REPETIR* para repetir el último.")
        rows = [
            ("APP_DELIVERY", "🛵 Pedir con entrega", "Iniciar pedido Picante Direct por WhatsApp"),
            ("APP_LOCATION", "📍 Máquina más cercana", "Ubicación y Google Maps"),
            ("APP_MENU", "🍕 Ver menú / información", "Pizzas, precios y recogida"),
            ("APP_HELP", "❓ Ayuda", "Contacto y horario"),
            ("APP_LANGUAGE", "🌐 Cambiar idioma", "English / Español"),
        ]
        send_list(phone, body, "Ver opciones", rows, section_title="Picante Gourmet Pizza")
    else:
        body = ("🍕 *PICANTE GOURMET PIZZA*\n\n" + ("Welcome! How can we help?" if welcome else "What would you like to do?") +
                "\n\nChoose an option. You can also reply 1–5.\nReply *PGP MENU* for the numbered main menu.\nReply *HISTORY* to view orders or *REORDER* to repeat the last one.")
        rows = [
            ("APP_DELIVERY", "🛵 Order for delivery", "Start Picante Direct ordering in WhatsApp"),
            ("APP_LOCATION", "📍 Find closest machine", "Location and Google Maps directions"),
            ("APP_MENU", "🍕 View menu / information", "Pizzas, prices and pickup"),
            ("APP_HELP", "❓ Help", "Contact and opening hours"),
            ("APP_LANGUAGE", "🌐 Change language", "English / Español"),
        ]
        send_list(phone, body, "Choose", rows, section_title="Picante Gourmet Pizza")


def numbered_pgp_menu(phone: str, language: str) -> None:
    state = session_get(phone)
    state.update({"stage": "PGP_MENU", "language": language})
    session_save(phone, state)
    if language == "es":
        send_text(phone,
            "🍕 *MENÚ PRINCIPAL PGP*\n\n"
            "1 — Ver menú 🍕\n2 — Recoger 🛍️\n3 — Ubicación 📍\n4 — Entrega 🚗\n\n"
            "9 — Cambiar idioma\n0 — Volver al menú de opciones")
    else:
        send_text(phone,
            "🍕 *PGP MAIN MENU*\n\n"
            "1 — View menu 🍕\n2 — Pickup 🛍️\n3 — Location 📍\n4 — Delivery 🚗\n\n"
            "9 — Change language\n0 — Return to app choices")


def menu_text(language: str) -> str:
    if language == "es":
        names = [f"{i}. {item['es']} — MX${PIZZA_PRICE_MXN}" for i, item in enumerate(MENU, 1)]
        return ("🍕 *MENÚ PICANTE GOURMET PIZZA*\n\n*RECOGER — MX$175 por pizza*\n"
                "*ENTREGA — MX$175 por pizza + cargo de entrega*\n\n" + "\n".join(names) +
                "\n\n🔥 Recién horneada en aproximadamente 3 minutos\n🕐 Disponible 24/7")
    names = [f"{i}. {item['en']} — MX${PIZZA_PRICE_MXN}" for i, item in enumerate(MENU, 1)]
    return ("🍕 *PICANTE GOURMET PIZZA MENU*\n\n*PICKUP — MX$175 per pizza*\n"
            "*DELIVERY — MX$175 per pizza + delivery fee*\n\n" + "\n".join(names) +
            "\n\n🔥 Fresh baked in approximately 3 minutes\n🕐 Available 24/7")


def show_menu(phone: str, language: str) -> None:
    state = session_get(phone)
    state.update({"stage": "PGP_MENU", "language": language})
    session_save(phone, state)
    body = menu_text(language)
    if language == "es":
        send_buttons(phone, body + "\n\n¿Qué deseas hacer?", [
            ("PGP_PICKUP", "Recoger"), ("APP_DELIVERY", "Pedir con entrega"), ("APP_HOME", "Menú principal")])
    else:
        send_buttons(phone, body + "\n\nWhat would you like to do?", [
            ("PGP_PICKUP", "Pickup"), ("APP_DELIVERY", "Delivery"), ("APP_HOME", "Main menu")])


def pickup_instructions(phone: str, language: str) -> None:
    if language == "es":
        message = ("🛍️ *RECOGER — MX$175 POR PIZZA*\n\n"
                   "1. Visita la máquina Picante Gourmet Pizza.\n"
                   "2. Elige tu pizza en la máquina.\n"
                   "3. Paga *MX$175* en la máquina.\n"
                   "4. Espera aproximadamente *3 minutos* mientras se hornea tu pizza.\n\n"
                   "No necesitas hacer un pedido por WhatsApp antes de llegar.\n\n"
                   f"📍 Ubicación: {PGP_ADDRESS_ES}\n{PICKUP_MAPS_URL}\n\n"
                   "Responde *3* para ubicación o *0* para volver al menú.")
    else:
        message = ("🛍️ *PICKUP — MX$175 PER PIZZA*\n\n"
                   "1. Visit the Picante Gourmet Pizza machine.\n"
                   "2. Choose your pizza at the machine.\n"
                   "3. Pay *MX$175* at the machine.\n"
                   "4. Wait approximately *3 minutes* while your pizza is freshly baked.\n\n"
                   "No advance WhatsApp order is required.\n\n"
                   f"📍 Location: {PGP_ADDRESS_EN}\n{PICKUP_MAPS_URL}\n\n"
                   "Reply *3* for location or *0* to return to the menu.")
    state = session_get(phone)
    state.update({"stage": "PGP_MENU", "language": language})
    session_save(phone, state)
    send_text(phone, message)


def location_information(phone: str, language: str) -> None:
    if language == "es":
        message = ("📍 *UBICACIÓN PICANTE GOURMET PIZZA*\n\nActualmente tenemos una máquina Picante.\n" + PGP_ADDRESS_ES +
                   "\n🕐 Abierto 24/7\n🍕 Recién horneada en aproximadamente 3 minutos\n\n" + PICKUP_MAPS_URL)
    else:
        message = ("📍 *PICANTE GOURMET PIZZA LOCATION*\n\nPicante currently has one machine.\n" + PGP_ADDRESS_EN +
                   "\n🕐 Open 24/7\n🍕 Fresh baked in approximately 3 minutes\n\n" + PICKUP_MAPS_URL)
    state = session_get(phone)
    state.update({"stage": "APP_HOME", "language": language})
    session_save(phone, state)
    send_text(phone, message)
    app_home(phone, language, welcome=False)


def help_information(phone: str, language: str) -> None:
    if language == "es":
        text = ("❓ *AYUDA — PICANTE GOURMET PIZZA*\n\nWhatsApp Picante: " + BUSINESS_PHONE +
                "\n🕐 Abierto 24/7\n🍕 Pizza recién horneada en aproximadamente 3 minutos\n\n"
                "Puedes consultar el menú, recoger en la máquina o pedir entrega directa por WhatsApp.")
    else:
        text = ("❓ *PICANTE GOURMET PIZZA HELP*\n\nPicante WhatsApp: " + BUSINESS_PHONE +
                "\n🕐 Open 24/7\n🍕 Fresh baked in approximately 3 minutes\n\n"
                "You can view the menu, pick up at the machine, or order direct delivery through WhatsApp.")
    send_text(phone, text)
    app_home(phone, language, welcome=False)

# ---------------------------------------------------------------------------
# Cart, customer checkout and price lock
# ---------------------------------------------------------------------------

def cart_quantity(cart: list) -> int:
    return sum(int(item["quantity"]) for item in cart)


def cart_pizza_total(cart: list) -> int:
    return cart_quantity(cart) * PIZZA_PRICE_MXN


def cart_summary(cart: list, language: str) -> str:
    if not cart:
        return "Tu carrito está vacío." if language == "es" else "Your cart is empty."
    lines = ["🛒 *TU PEDIDO*" if language == "es" else "🛒 *YOUR ORDER*"]
    for item in cart:
        pizza = MENU_BY_ID[item["pizza_id"]]
        name = pizza["es"] if language == "es" else pizza["en"]
        qty = int(item["quantity"])
        menu_number = next((index for index, entry in enumerate(MENU, 1) if entry["id"] == item["pizza_id"]), 0)
        lines.append(f"{menu_number}. {qty} × {name} — MX${qty * PIZZA_PRICE_MXN}")
    lines.append(f"Cantidad de pizzas: {cart_quantity(cart)}" if language == "es" else f"Pizza quantity: {cart_quantity(cart)}")
    lines.append(f"Subtotal de pizzas: MX${cart_pizza_total(cart)}" if language == "es" else f"Pizza subtotal: MX${cart_pizza_total(cart)}")
    return "\n".join(lines)


def pricing_for_cart(cart: list, at: Optional[datetime] = None) -> dict:
    period, fee = local_period_and_fee(at)
    quantity = cart_quantity(cart)
    pizza_total = cart_pizza_total(cart)
    delivery_total = fee * quantity
    return {
        "period": period,
        "fee_each": fee,
        "quantity": quantity,
        "pizza_total": pizza_total,
        "delivery_total": delivery_total,
        "total": pizza_total + delivery_total,
    }


def send_pizza_selection(phone: str, language: str) -> None:
    rows = []
    for index, item in enumerate(MENU, 1):
        name = item["es"] if language == "es" else item["en"]
        rows.append((f"PIZZA_{item['id']}", name, f"MX${PIZZA_PRICE_MXN} each / cada una"))
    state = session_get(phone)
    state["stage"] = "PIZZA"
    session_save(phone, state)
    body = ("🍕 *ELIGE TUS PIZZAS*\n\nSelecciona una pizza y después indica la cantidad.\n"
            "También puedes escribir, por ejemplo, *1 x2*." if language == "es" else
            "🍕 *CHOOSE YOUR PIZZAS*\n\nSelect a pizza, then enter the quantity.\n"
            "You can also type, for example, *1 x2*.")
    send_list(phone, body, "Ver pizzas" if language == "es" else "Choose pizza", rows,
              section_title="Menú PGP" if language == "es" else "PGP Pizza Menu")
    if state.get("cart"):
        send_text(phone, cart_summary(state["cart"], language))


def show_cart_actions(phone: str, language: str) -> None:
    state = session_get(phone)
    summary = cart_summary(state.get("cart", []), language)
    edit_hint = ("\n\nPara quitar una pizza: *QUITAR 2*. Para cambiar cantidad: *CAMBIAR 2 x1*."
                 if language == "es" else
                 "\n\nTo remove a pizza: *REMOVE 2*. To change quantity: *SET 2 x1*.")
    if language == "es":
        send_buttons(phone, summary + edit_hint + "\n\n¿Qué deseas hacer?", [
            ("CART_ADD", "Agregar otra"), ("CART_CHECKOUT", "Continuar"), ("CART_CLEAR", "Vaciar pedido")])
    else:
        send_buttons(phone, summary + edit_hint + "\n\nWhat would you like to do?", [
            ("CART_ADD", "Add another"), ("CART_CHECKOUT", "Checkout"), ("CART_CLEAR", "Clear order")])


def _add_to_cart(state: dict, menu_index: int, quantity: int) -> None:
    pizza = MENU[menu_index - 1]
    current = next((entry for entry in state.setdefault("cart", []) if entry["pizza_id"] == pizza["id"]), None)
    if current:
        current["quantity"] = int(current["quantity"]) + quantity
    else:
        state["cart"].append({"pizza_id": pizza["id"], "quantity": quantity})


def _ask_customer_name(phone: str, state: dict) -> None:
    customer = customer_get(phone) or {}
    language = state["language"]
    if state.get("name"):
        _ask_address(phone, state)
        return
    if customer.get("name"):
        state["stage"] = "NAME_CONFIRM"
        session_save(phone, state)
        if language == "es":
            send_buttons(phone, f"¿Usamos tu nombre guardado *{customer['name']}*?", [("NAME_YES", "Sí"), ("NAME_NEW", "Cambiar")])
        else:
            send_buttons(phone, f"Use your saved name *{customer['name']}*?", [("NAME_YES", "Yes"), ("NAME_NEW", "Change")])
    else:
        state["stage"] = "NAME"
        session_save(phone, state)
        send_text(phone, "¿Cuál es tu nombre?" if language == "es" else "What is your name?")


def _ask_address(phone: str, state: dict) -> None:
    customer = customer_get(phone) or {}
    language = state["language"]
    if state.get("address"):
        _ask_instructions(phone, state)
        return
    if customer.get("address"):
        state["stage"] = "ADDRESS_CONFIRM"
        session_save(phone, state)
        if language == "es":
            send_buttons(phone, f"¿Usamos tu dirección guardada?\n\n{customer['address']}", [("ADDRESS_YES", "Sí"), ("ADDRESS_NEW", "Nueva dirección")])
        else:
            send_buttons(phone, f"Use your saved address?\n\n{customer['address']}", [("ADDRESS_YES", "Yes"), ("ADDRESS_NEW", "New address")])
    else:
        state["stage"] = "ADDRESS"
        session_save(phone, state)
        send_text(phone, "¿Cuál es la dirección completa de entrega? También puedes compartir tu ubicación." if language == "es" else
                  "What is the full delivery address? You can also share your WhatsApp location.")


def _ask_instructions(phone: str, state: dict) -> None:
    customer = customer_get(phone) or {}
    language = state["language"]
    if "instructions" in state:
        state["stage"] = "INSTRUCTIONS_CONFIRM"
        session_save(phone, state)
        if language == "es":
            send_buttons(phone, f"¿Usamos estas instrucciones para este pedido?\n\n{state.get('instructions') or 'Ninguna'}", [("INSTRUCTIONS_YES", "Sí"), ("INSTRUCTIONS_NEW", "Cambiar"), ("INSTRUCTIONS_NONE", "Ninguna")])
        else:
            send_buttons(phone, f"Use these instructions for this order?\n\n{state.get('instructions') or 'None'}", [("INSTRUCTIONS_YES", "Yes"), ("INSTRUCTIONS_NEW", "Change"), ("INSTRUCTIONS_NONE", "None")])
        return
    if customer.get("instructions"):
        state["stage"] = "INSTRUCTIONS_CONFIRM"
        session_save(phone, state)
        if language == "es":
            send_buttons(phone, f"¿Usamos tus instrucciones guardadas?\n\n{customer['instructions']}",
                         [("INSTRUCTIONS_YES", "Sí"), ("INSTRUCTIONS_NEW", "Cambiar"), ("INSTRUCTIONS_NONE", "Ninguna")])
        else:
            send_buttons(phone, f"Use your saved instructions?\n\n{customer['instructions']}",
                         [("INSTRUCTIONS_YES", "Yes"), ("INSTRUCTIONS_NEW", "Change"), ("INSTRUCTIONS_NONE", "None")])
    else:
        state["stage"] = "INSTRUCTIONS"
        session_save(phone, state)
        send_text(phone, "¿Alguna instrucción especial de entrega? Responde *NINGUNA* si no hay." if language == "es" else
                  "Any special delivery instructions? Reply *NONE* if there are none.")


def build_confirmation(phone: str, state: dict, *, force_new_quote: bool = False) -> None:
    language = state["language"]
    quote = pricing_for_cart(state["cart"])
    state["quote"] = quote
    state["quote_at"] = iso_now()
    state["stage"] = "CONFIRM"
    session_save(phone, state)
    summary = cart_summary(state["cart"], language)
    period_text = "DÍA 06:00–23:59" if quote["period"] == "day" else "NOCHE 00:00–05:59"
    if language == "es":
        body = (summary + f"\n\nEntrega: MX${quote['fee_each']} × {quote['quantity']} = MX${quote['delivery_total']}"
                f"\nPeriodo: {period_text}\n*TOTAL: MX${quote['total']}*"
                f"\n\nNombre: {state['name']}\nWhatsApp: {phone}\nDirección: {state['address']}"
                f"\nInstrucciones: {state.get('instructions') or 'Ninguna'}"
                "\n\nEl pago se realiza al conductor cuando recibas tu pedido."
                "\n\nConfirma para colocar tu pedido o elige editar las pizzas.")
        send_buttons(phone, body, [("PLACE_DIRECT_DELIVERY", "Confirmar pedido"), ("EDIT_ORDER", "Editar pizzas"), ("CANCEL_CHECKOUT", "Cancelar")])
    else:
        period_text = "DAY 06:00–23:59" if quote["period"] == "day" else "OVERNIGHT 00:00–05:59"
        body = (summary + f"\n\nDelivery: MX${quote['fee_each']} × {quote['quantity']} = MX${quote['delivery_total']}"
                f"\nPeriod: {period_text}\n*TOTAL: MX${quote['total']}*"
                f"\n\nName: {state['name']}\nWhatsApp: {phone}\nAddress: {state['address']}"
                f"\nInstructions: {state.get('instructions') or 'None'}"
                "\n\nPayment is made to your driver when your order arrives."
                "\n\nConfirm to place your order or choose to edit the pizzas.")
        send_buttons(phone, body, [("PLACE_DIRECT_DELIVERY", "Place delivery"), ("EDIT_ORDER", "Edit pizzas"), ("CANCEL_CHECKOUT", "Cancel")])


def persist_order(phone: str, state: dict) -> dict:
    latest_pricing = pricing_for_cart(state["cart"])
    quoted = state.get("quote") or {}
    if quoted and (quoted.get("period") != latest_pricing["period"] or quoted.get("total") != latest_pricing["total"]):
        # If the time boundary changed before confirmation, show the new price
        # and require the customer to confirm it explicitly.
        state["quote"] = latest_pricing
        state["quote_at"] = iso_now()
        session_save(phone, state)
        raise PriceQuoteChanged(latest_pricing)
    with db_connection(immediate=not _is_postgres()) as connection:
        if _is_postgres():
            ref = new_order_reference(connection)
            cursor = execute(connection, """INSERT INTO pgp_orders
                (ref,customer_phone,customer_name,language,address,instructions,period,
                 pizza_total_mxn,delivery_fee_mxn,total_mxn,status,created_at,dispatch_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                (ref, phone, state["name"], state["language"], state["address"], state.get("instructions", ""),
                 latest_pricing["period"], latest_pricing["pizza_total"], latest_pricing["delivery_total"],
                 latest_pricing["total"], "OPEN", iso_now(), iso_now()))
            order_id = cursor.fetchone()["id"]
        else:
            ref = new_order_reference(connection)
            cursor = execute(connection, """INSERT INTO pgp_orders
                (ref,customer_phone,customer_name,language,address,instructions,period,
                 pizza_total_mxn,delivery_fee_mxn,total_mxn,status,created_at,dispatch_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ref, phone, state["name"], state["language"], state["address"], state.get("instructions", ""),
                 latest_pricing["period"], latest_pricing["pizza_total"], latest_pricing["delivery_total"],
                 latest_pricing["total"], "OPEN", iso_now(), iso_now()))
            order_id = cursor.lastrowid
        for cart_item in state["cart"]:
            pizza = MENU_BY_ID[cart_item["pizza_id"]]
            display_name = pizza["es"] if state["language"] == "es" else pizza["en"]
            execute(connection, """INSERT INTO pgp_order_items
                (order_id,pizza_id,pizza_name,quantity,unit_price_mxn) VALUES(?,?,?,?,?)""",
                (order_id, pizza["id"], display_name, int(cart_item["quantity"]), PIZZA_PRICE_MXN))
    customer_save(phone, language=state["language"], name=state["name"], address=state["address"], instructions=state.get("instructions", ""))
    return order_get(ref)


class PriceQuoteChanged(Exception):
    def __init__(self, quote):
        self.quote = quote

# ---------------------------------------------------------------------------
# Driver registration, eligibility, atomic claims and dispatch
# ---------------------------------------------------------------------------

def driver_get(phone: str) -> Optional[dict]:
    with db_connection() as connection:
        return row_dict(fetch_one(connection, "SELECT * FROM pgp_drivers WHERE phone=?", (normalize_phone(phone),)))


def driver_unsettled_count(phone: str) -> int:
    with db_connection() as connection:
        row = fetch_one(connection, "SELECT COUNT(*) AS n FROM pgp_deliveries WHERE driver_phone=? AND settlement_id IS NULL", (normalize_phone(phone),))
    return int(row_value(row, "n", 0) or 0)


def driver_is_eligible(phone: str) -> bool:
    driver = driver_get(phone)
    if not driver or not driver.get("approved") or not driver.get("active") or driver.get("busy"):
        return False
    return driver_unsettled_count(phone) < MAX_UNSETTLED_DELIVERIES_PER_DRIVER


def driver_message(driver: dict, en: str, es: str) -> str:
    return en if driver.get("language", "en") == "en" else es


def dispatch_message(order: dict, language: str = "both") -> str:
    items = "\n".join(f"{item['quantity']} × {item['pizza_name']}" for item in order.get("items", []))
    if language == "es":
        return (f"🚨 *PICANTE DIRECT — {order['ref']}*\n\nPizzas:\n{items}\n\n"
                f"Total del cliente: MX${order['total_mxn']}\nPeriodo: {order['period']}\n\n"
                f"Responde *CLAIM {order['ref']}* para reclamar. El primer conductor elegible gana.\n"
                "Los datos privados del cliente se enviarán después de reclamar correctamente.")
    if language == "both":
        return (f"🚨 *PICANTE DIRECT — {order['ref']}*\n\nPIZZAS / PIZZAS:\n{items}\n\n"
                f"CUSTOMER TOTAL / TOTAL CLIENTE: MX${order['total_mxn']}\n"
                f"PERIOD / PERIODO: {order['period']}\n\n"
                f"Reply *CLAIM {order['ref']}* to claim / Responde *CLAIM {order['ref']}* para reclamar.\n"
                "First eligible driver wins / El primer conductor elegible gana.\n"
                "Private customer details are sent only after a successful claim / Los datos privados se envían después de reclamar.")
    return (f"🚨 *PICANTE DIRECT — {order['ref']}*\n\nPizzas:\n{items}\n\n"
            f"Customer total: MX${order['total_mxn']}\nPeriod: {order['period']}\n\n"
            f"Reply *CLAIM {order['ref']}* to claim. First eligible driver wins.\n"
            "Private customer details are sent after a successful claim.")


def dispatch_order(order: dict) -> bool:
    message = dispatch_message(order, "both")
    if DRIVER_GROUP_ID and send_text(DRIVER_GROUP_ID, message):
        return True
    # If group delivery is unavailable, privately alert available registered drivers.
    with db_connection() as connection:
        drivers = [row_dict(row) for row in fetch_all(connection,
            "SELECT * FROM pgp_drivers WHERE approved=1 AND active=1 AND busy=0")]
    sent_any = False
    for driver in drivers:
        if driver_unsettled_count(driver["phone"]) < MAX_UNSETTLED_DELIVERIES_PER_DRIVER:
            sent_any = send_text(driver["phone"], dispatch_message(order, driver.get("language", "en"))) or sent_any
    return sent_any


def claim_order(driver_phone: str, ref: str) -> Tuple[bool, str, Optional[dict]]:
    phone = normalize_phone(driver_phone)
    with db_connection(immediate=not _is_postgres()) as connection:
        if _is_postgres():
            order_row = fetch_one(connection, "SELECT * FROM pgp_orders WHERE ref=? FOR UPDATE", (ref.upper(),))
            driver_row = fetch_one(connection, "SELECT * FROM pgp_drivers WHERE phone=? FOR UPDATE", (phone,))
        else:
            order_row = fetch_one(connection, "SELECT * FROM pgp_orders WHERE ref=?", (ref.upper(),))
            driver_row = fetch_one(connection, "SELECT * FROM pgp_drivers WHERE phone=?", (phone,))
        order = row_dict(order_row)
        driver = row_dict(driver_row)
        if not order:
            return False, "Order not found.", None
        if not driver or not driver["approved"] or not driver["active"] or driver["busy"]:
            return False, driver_message(driver or {"language": "en"}, "You are not eligible for this delivery.", "No eres elegible para esta entrega."), None
        unsettled = fetch_one(connection, "SELECT COUNT(*) AS n FROM pgp_deliveries WHERE driver_phone=? AND settlement_id IS NULL", (phone,))
        if int(row_value(unsettled, "n", 0) or 0) >= MAX_UNSETTLED_DELIVERIES_PER_DRIVER:
            return False, driver_message(driver, "You have reached the unsettled-delivery limit.", "Has alcanzado el límite de entregas pendientes de liquidación."), None
        if order["status"] != "OPEN":
            return False, driver_message(driver, "That order is no longer available.", "Ese pedido ya no está disponible."), None
        cursor = execute(connection, """UPDATE pgp_orders SET status='AWAITING_CUSTOMER_GUARANTEE',
            driver_phone=?,driver_name=?,claimed_at=? WHERE id=? AND status='OPEN'""",
            (phone, driver["name"], iso_now(), order["id"]))
        if cursor.rowcount != 1:
            return False, driver_message(driver, "Another driver has already claimed that order.", "Otro conductor ya reclamó ese pedido."), None
        execute(connection, "UPDATE pgp_drivers SET busy=1,updated_at=? WHERE phone=?", (iso_now(), phone))
    return True, "CLAIMED", order_get(ref)


def driver_private_details(order: dict, language: str) -> str:
    items = "\n".join(f"{item['quantity']} × {item['pizza_name']}" for item in order.get("items", []))
    if language == "es":
        return (f"✅ *PEDIDO RECLAMADO — {order['ref']}*\n\nCliente: {order['customer_name']}\n"
                f"WhatsApp: {order['customer_phone']}\nDirección: {order['address']}\n"
                f"Instrucciones: {order.get('instructions') or 'Ninguna'}\n\nPizzas:\n{items}\n\n"
                f"TOTAL A COBRAR: MX${order['total_mxn']}\nEntrega para conductor: MX${order['delivery_fee_mxn']}\n\n"
                "Espera la aceptación del cliente de la nueva garantía de 30 minutos antes de salir.")
    return (f"✅ *CLAIMED — {order['ref']}*\n\nCustomer: {order['customer_name']}\n"
            f"WhatsApp: {order['customer_phone']}\nAddress: {order['address']}\n"
            f"Instructions: {order.get('instructions') or 'None'}\n\nPizzas:\n{items}\n\n"
            f"TOTAL TO COLLECT: MX${order['total_mxn']}\nDriver delivery fee: MX${order['delivery_fee_mxn']}\n\n"
            "Wait for the customer's acceptance of the fresh 30-minute guarantee before leaving.")


def notify_customer_driver_claimed(order: dict) -> None:
    if order["language"] == "es":
        body = (f"🛵 *CONDUCTOR ENCONTRADO — {order['ref']}*\n\n"
                f"Tu conductor es *{order['driver_name']}*.\n\n"
                "¿Aceptas una nueva garantía de entrega de 30 minutos a partir de ahora?\n"
                "Responde *1* para aceptar o *2* para cancelar.")
        send_buttons(order["customer_phone"], body, [
            (f"GUARANTEE_ACCEPT_{order['ref']}", "Aceptar 30 min"),
            (f"GUARANTEE_CANCEL_{order['ref']}", "Cancelar")])
    else:
        body = (f"🛵 *DRIVER FOUND — {order['ref']}*\n\n"
                f"Your driver is *{order['driver_name']}*.\n\n"
                "Do you accept a fresh 30-minute delivery guarantee starting now?\n"
                "Reply *1* to accept or *2* to cancel.")
        send_buttons(order["customer_phone"], body, [
            (f"GUARANTEE_ACCEPT_{order['ref']}", "Accept 30 min"),
            (f"GUARANTEE_CANCEL_{order['ref']}", "Cancel")])


def accept_customer_guarantee(phone: str, ref: str, accept: bool) -> str:
    order = order_get(ref, include_items=False)
    if not order or normalize_phone(order["customer_phone"]) != normalize_phone(phone):
        return "No matching active order." if not order or order.get("language") == "en" else "No encontramos un pedido activo."
    if order["status"] != "AWAITING_CUSTOMER_GUARANTEE":
        return "This order is no longer waiting for confirmation." if order["language"] == "en" else "Este pedido ya no espera confirmación."
    if accept:
        accepted = now_utc()
        deadline = accepted + timedelta(minutes=GUARANTEE_MINUTES)
        with db_connection(immediate=not _is_postgres()) as connection:
            cursor = execute(connection, """UPDATE pgp_orders SET status='ASSIGNED',
                guarantee_accepted_at=?,guarantee_deadline=?
                WHERE ref=? AND customer_phone=? AND status='AWAITING_CUSTOMER_GUARANTEE'""",
                (accepted.isoformat(), deadline.isoformat(), ref, normalize_phone(phone)))
            if cursor.rowcount != 1:
                return "This order has already changed status." if order["language"] == "en" else "El estado de este pedido ya cambió."
        driver = driver_get(order["driver_phone"])
        if driver:
            send_text(driver["phone"], driver_message(driver,
                f"✅ Customer accepted the 30-minute guarantee for {ref}. You may begin the delivery. Send ON MY WAY {ref} when leaving.",
                f"✅ El cliente aceptó la garantía de 30 minutos para {ref}. Puedes iniciar la entrega. Envía EN CAMINO {ref} al salir."))
        customer_message = (f"✅ {ref}: You accepted. Your 30-minute delivery guarantee starts now. Your driver will notify you when on the way."
                            if order["language"] == "en" else
                            f"✅ {ref}: Aceptaste. Tu garantía de entrega de 30 minutos comienza ahora. Tu conductor te avisará cuando vaya en camino.")
        return customer_message
    with db_connection(immediate=not _is_postgres()) as connection:
        execute(connection, "UPDATE pgp_orders SET status='CANCELLED',cancelled_at=? WHERE ref=? AND status='AWAITING_CUSTOMER_GUARANTEE'", (iso_now(), ref))
        if order.get("driver_phone"):
            execute(connection, "UPDATE pgp_drivers SET busy=0,updated_at=? WHERE phone=?", (iso_now(), order["driver_phone"]))
    driver = driver_get(order.get("driver_phone") or "")
    if driver:
        send_text(driver["phone"], driver_message(driver, f"Order {ref} was cancelled by the customer. You are available again.", f"El cliente canceló el pedido {ref}. Ya estás disponible."))
    return f"{ref} cancelled." if order["language"] == "en" else f"Pedido {ref} cancelado."

# ---------------------------------------------------------------------------
# Driver commands and delivery accounting
# ---------------------------------------------------------------------------

def _parse_reference(parts: list) -> Optional[str]:
    for part in parts:
        if part.upper().startswith("PD-"):
            return part.upper()
    return None


def driver_flow(phone: str, text: str) -> Optional[str]:
    driver = driver_get(phone)
    if not driver:
        return None
    if not driver["approved"] or not driver["active"]:
        return driver_message(driver, "Your driver account is not approved and active.", "Tu cuenta de conductor no está aprobada y activa.")
    cleaned = text.strip()
    if not cleaned:
        return None
    parts = cleaned.split()
    cmd = parts[0].lower()
    if cmd not in {"claim", "on", "en", "location", "delivered", "entregado", "cancel", "cancelar", "release", "tip", "help", "ayuda"}:
        return None
    if cmd in {"help", "ayuda"}:
        return driver_message(driver,
            "Driver commands:\nCLAIM PD-XXXX\nON MY WAY PD-XXXX\nLOCATION PD-XXXX <Google Maps link>\nDELIVERED PD-XXXX [TIP amount]\nTIP PD-XXXX amount (after delivery)\nCANCEL PD-XXXX",
            "Comandos:\nCLAIM PD-XXXX\nEN CAMINO PD-XXXX\nLOCATION PD-XXXX <enlace Google Maps>\nENTREGADO PD-XXXX [PROPINA cantidad]\nTIP PD-XXXX cantidad (después de entregar)\nCANCEL PD-XXXX")
    if cmd == "claim":
        if len(parts) < 2:
            return "Use CLAIM PD-XXXX" if driver["language"] == "en" else "Usa CLAIM PD-XXXX"
        ref = parts[1].upper()
        ok, message, order = claim_order(phone, ref)
        if not ok:
            return message
        # Customer details are private and only sent after the successful DB claim.
        send_text(phone, driver_private_details(order, driver["language"]))
        notify_customer_driver_claimed(order)
        return "Waiting for customer confirmation." if driver["language"] == "en" else "Esperando la confirmación del cliente."
    if cmd in {"on", "en"}:
        ref = _parse_reference(parts)
        if not ref:
            return "Use ON MY WAY PD-XXXX" if driver["language"] == "en" else "Usa EN CAMINO PD-XXXX"
        order = order_get(ref, include_items=False)
        if not order or normalize_phone(order.get("driver_phone")) != normalize_phone(phone):
            return "Active delivery not found." if driver["language"] == "en" else "No encontramos una entrega activa."
        if order["status"] != "ASSIGNED" or not order.get("guarantee_accepted_at"):
            return "Wait until the customer accepts the 30-minute guarantee." if driver["language"] == "en" else "Espera hasta que el cliente acepte la garantía de 30 minutos."
        # Accept both ON MY WAY and EN CAMINO; optional URL is handled separately by LOCATION.
        with db_connection() as connection:
            execute(connection, "UPDATE pgp_orders SET status='EN_ROUTE' WHERE ref=? AND driver_phone=? AND status='ASSIGNED'", (ref, normalize_phone(phone)))
        customer_msg = (f"🛵 *YOUR DRIVER IS ON THE WAY — {ref}*\n\nYour driver is on the way. You will receive a location update if one is shared."
                        if order["language"] == "en" else
                        f"🛵 *TU CONDUCTOR VA EN CAMINO — {ref}*\n\nTu conductor ya va en camino. Recibirás la ubicación cuando la comparta.")
        send_text(order["customer_phone"], customer_msg)
        return "Customer notified." if driver["language"] == "en" else "Cliente notificado."
    if cmd == "location":
        if len(parts) < 3:
            return "Use LOCATION PD-XXXX <Google Maps link>" if driver["language"] == "en" else "Usa LOCATION PD-XXXX <enlace Google Maps>"
        ref = parts[1].upper()
        link = " ".join(parts[2:]).strip()
        if not is_valid_maps_link(link):
            return "Please provide a valid Google Maps https link." if driver["language"] == "en" else "Proporciona un enlace https válido de Google Maps."
        order = order_get(ref, include_items=False)
        if not order or normalize_phone(order.get("driver_phone")) != normalize_phone(phone):
            return "Active delivery not found." if driver["language"] == "en" else "No encontramos una entrega activa."
        if order["status"] not in {"ASSIGNED", "EN_ROUTE"} or not order.get("guarantee_accepted_at"):
            return "The delivery is not active yet." if driver["language"] == "en" else "La entrega todavía no está activa."
        with db_connection() as connection:
            execute(connection, "UPDATE pgp_orders SET tracking_url=? WHERE ref=? AND driver_phone=?", (link, ref, normalize_phone(phone)))
        send_text(order["customer_phone"], (f"📍 *LIVE DELIVERY LOCATION — {ref}*\n\n{link}" if order["language"] == "en" else f"📍 *UBICACIÓN DE ENTREGA — {ref}*\n\n{link}"))
        return "Location sent." if driver["language"] == "en" else "Ubicación enviada."
    if cmd == "tip":
        ref = _parse_reference(parts)
        if not ref or len(parts) < 3:
            return "Use TIP PD-XXXX amount after delivery." if driver["language"] == "en" else "Usa TIP PD-XXXX cantidad después de entregar."
        try:
            tip_amount = max(0, int(parts[-1]))
        except ValueError:
            return "Enter the tip as a whole MXN amount." if driver["language"] == "en" else "Escribe la propina como cantidad entera en MXN."
        order = order_get(ref, include_items=False)
        if not order or normalize_phone(order.get("driver_phone")) != normalize_phone(phone) or order.get("status") != "DELIVERED":
            return "Delivered order not found for your driver account." if driver["language"] == "en" else "No encontramos una entrega tuya para actualizar la propina."
        with db_connection() as connection:
            execute(connection, "UPDATE pgp_deliveries SET tip_mxn=? WHERE order_id=? AND driver_phone=?", (tip_amount, order["id"], normalize_phone(phone)))
        return "Tip updated." if driver["language"] == "en" else "Propina actualizada."

    if cmd in {"delivered", "entregado"}:
        ref = _parse_reference(parts)
        if not ref:
            return "Use DELIVERED PD-XXXX [TIP amount]" if driver["language"] == "en" else "Usa ENTREGADO PD-XXXX [PROPINA cantidad]"
        order = order_get(ref, include_items=False)
        if not order or normalize_phone(order.get("driver_phone")) != normalize_phone(phone):
            return "Active delivery not found." if driver["language"] == "en" else "No encontramos una entrega activa."
        if order["status"] not in {"ASSIGNED", "EN_ROUTE"} or not order.get("guarantee_accepted_at"):
            return "This delivery is not active." if driver["language"] == "en" else "Esta entrega no está activa."
        tip = 0
        if "tip" in [p.lower() for p in parts] or "propina" in [p.lower() for p in parts]:
            try:
                tip = max(0, int(parts[-1]))
            except ValueError:
                return "Enter the tip as a whole MXN amount." if driver["language"] == "en" else "Escribe la propina como cantidad entera en MXN."
        with db_connection(immediate=not _is_postgres()) as connection:
            if _is_postgres():
                live_order = fetch_one(connection, "SELECT * FROM pgp_orders WHERE ref=? FOR UPDATE", (ref,))
            else:
                live_order = fetch_one(connection, "SELECT * FROM pgp_orders WHERE ref=?", (ref,))
            if not live_order or row_value(live_order, "status") not in {"ASSIGNED", "EN_ROUTE"}:
                return "This delivery has already changed status."
            # One completed-delivery record per order.
            existing = fetch_one(connection, "SELECT id FROM pgp_deliveries WHERE order_id=?", (order["id"],))
            if not existing:
                card_total = fetch_one(connection, "SELECT COALESCE(SUM(amount_mxn),0) AS total FROM pgp_card_transactions WHERE order_ref=?", (ref,))
                last_card = fetch_one(connection, "SELECT card_label,reference FROM pgp_card_transactions WHERE order_ref=? ORDER BY id DESC LIMIT 1", (ref,))
                machine_purchase = int(row_value(card_total, "total", 0) or 0)
                execute(connection, """INSERT INTO pgp_deliveries
                    (order_id,driver_phone,customer_cash_mxn,pizza_total_mxn,delivery_fee_mxn,
                     tip_mxn,amount_due_office_mxn,machine_purchase_mxn,company_card,
                     transaction_reference,completed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (order["id"], normalize_phone(phone), order["total_mxn"], order["pizza_total_mxn"],
                     order["delivery_fee_mxn"], tip, order["pizza_total_mxn"], machine_purchase,
                     row_value(last_card, "card_label"), row_value(last_card, "reference"), iso_now()))
            execute(connection, "UPDATE pgp_orders SET status='DELIVERED',delivered_at=? WHERE ref=?", (iso_now(), ref))
            execute(connection, "UPDATE pgp_drivers SET busy=0,updated_at=? WHERE phone=?", (iso_now(), normalize_phone(phone)))
        customer_msg = (f"✅ *DELIVERED — {ref}*\n\nYour Picante Gourmet Pizza order has been delivered. Thank you!"
                        if order["language"] == "en" else
                        f"✅ *ENTREGADO — {ref}*\n\nTu pedido de Picante Gourmet Pizza ha sido entregado. ¡Gracias!")
        send_text(order["customer_phone"], customer_msg)
        return "Delivery completed. You are available for another order." if driver["language"] == "en" else "Entrega completada. Ya estás disponible para otro pedido."
    if cmd in {"cancel", "cancelar", "release"}:
        ref = _parse_reference(parts)
        if not ref:
            return "Use CANCEL PD-XXXX" if driver["language"] == "en" else "Usa CANCEL PD-XXXX"
        order = order_get(ref, include_items=False)
        if not order or normalize_phone(order.get("driver_phone")) != normalize_phone(phone):
            return "Active delivery not found." if driver["language"] == "en" else "No encontramos una entrega activa."
        if order["status"] not in {"AWAITING_CUSTOMER_GUARANTEE", "ASSIGNED"}:
            return "This delivery cannot be released at this stage." if driver["language"] == "en" else "Esta entrega no se puede liberar en esta etapa."
        with db_connection(immediate=not _is_postgres()) as connection:
            execute(connection, """UPDATE pgp_orders SET status='OPEN',driver_phone=NULL,driver_name=NULL,
                claimed_at=NULL,guarantee_accepted_at=NULL,guarantee_deadline=NULL,
                no_driver_notice_at=NULL,no_driver_choice=NULL,dispatch_at=? WHERE ref=?""", (iso_now(), ref))
            execute(connection, "UPDATE pgp_drivers SET busy=0,updated_at=? WHERE phone=?", (iso_now(), normalize_phone(phone)))
        customer_msg = (f"⚠️ Your driver is no longer assigned to {ref}. We are searching for another eligible driver."
                        if order["language"] == "en" else
                        f"⚠️ Tu conductor ya no está asignado a {ref}. Buscaremos otro conductor disponible.")
        send_text(order["customer_phone"], customer_msg)
        new_order = order_get(ref)
        dispatch_order(new_order)
        return "Delivery released and re-dispatched." if driver["language"] == "en" else "Entrega liberada y enviada nuevamente."
    return None

# ---------------------------------------------------------------------------
# Five-minute exception and customer-order state machine
# ---------------------------------------------------------------------------

def active_exception_order(phone: str) -> Optional[dict]:
    with db_connection() as connection:
        row = fetch_one(connection, """SELECT * FROM pgp_orders WHERE customer_phone=? AND status='OPEN'
            AND no_driver_notice_at IS NOT NULL AND no_driver_choice IS NULL ORDER BY id DESC LIMIT 1""", (normalize_phone(phone),))
    return row_dict(row)


def handle_no_driver_reply(phone: str, text: str, interactive_id: str = "") -> Optional[str]:
    order = active_exception_order(phone)
    if not order:
        return None
    low = text.strip().lower()
    if interactive_id == f"NO_DRIVER_CONTINUE_{order['ref']}" or low in {"1", "continue", "continuar", "yes", "sí", "si"}:
        with db_connection() as connection:
            execute(connection, "UPDATE pgp_orders SET no_driver_choice='CONTINUE' WHERE ref=? AND no_driver_choice IS NULL", (order["ref"],))
        return (f"✅ {order['ref']}: We will continue searching for a driver. If a driver is found, we will ask you to accept the fresh 30-minute guarantee."
                if order["language"] == "en" else
                f"✅ {order['ref']}: Continuaremos buscando conductor. Si encontramos uno, te pediremos aceptar la nueva garantía de 30 minutos.")
    if interactive_id == f"NO_DRIVER_CANCEL_{order['ref']}" or low in {"2", "cancel", "cancelar", "no"}:
        with db_connection(immediate=not _is_postgres()) as connection:
            execute(connection, "UPDATE pgp_orders SET status='CANCELLED',cancelled_at=? WHERE ref=? AND status='OPEN'", (iso_now(), order["ref"]))
        return (f"Order {order['ref']} has been cancelled." if order["language"] == "en" else f"El pedido {order['ref']} ha sido cancelado.")
    if order["language"] == "es":
        return "Responde *1* para continuar buscando o *2* para cancelar."
    return "Reply *1* to continue searching or *2* to cancel."


def handle_customer_guarantee_reply(phone: str, text: str, interactive_id: str = "") -> Optional[str]:
    low = text.strip().lower()
    ref = None
    accept = None
    match = re.match(r"GUARANTEE_(ACCEPT|CANCEL)_(PD-[A-F0-9]+)$", interactive_id)
    if match:
        accept = match.group(1) == "ACCEPT"
        ref = match.group(2)
    else:
        with db_connection() as connection:
            row = fetch_one(connection, """SELECT ref,language FROM pgp_orders WHERE customer_phone=?
                AND status='AWAITING_CUSTOMER_GUARANTEE' ORDER BY id DESC LIMIT 1""", (normalize_phone(phone),))
        if not row:
            return None
        ref = row_value(row, "ref")
        if low in {"1", "yes", "accept", "accept 30", "accept 30 min", "aceptar", "aceptar 30 min", "sí", "si"} or low.startswith("accept "):
            accept = True
        elif low in {"2", "cancel", "cancelar", "no"}:
            accept = False
        else:
            language = row_value(row, "language", "en")
            return "Responde 1 para aceptar o 2 para cancelar." if language == "es" else "Reply 1 to accept or 2 to cancel."
    return accept_customer_guarantee(phone, ref, bool(accept)) if ref and accept is not None else None


def handle_customer_active_order_cancel(phone: str, text: str) -> Optional[str]:
    match = re.match(r"^(?:CANCEL|CANCELAR)\s+(PD-[A-F0-9]+)$", text.strip(), re.I)
    if not match:
        return None
    ref = match.group(1).upper()
    order = order_get(ref, include_items=False)
    if not order or normalize_phone(order["customer_phone"]) != normalize_phone(phone):
        return "Order not found." if not order or order.get("language") == "en" else "Pedido no encontrado."
    if order["status"] not in {"OPEN", "AWAITING_CUSTOMER_GUARANTEE"}:
        return "This order can no longer be cancelled through chat." if order["language"] == "en" else "Este pedido ya no se puede cancelar por chat."
    with db_connection(immediate=not _is_postgres()) as connection:
        execute(connection, "UPDATE pgp_orders SET status='CANCELLED',cancelled_at=? WHERE ref=?", (iso_now(), ref))
        if order.get("driver_phone"):
            execute(connection, "UPDATE pgp_drivers SET busy=0,updated_at=? WHERE phone=?", (iso_now(), order["driver_phone"]))
    if order.get("driver_phone"):
        driver = driver_get(order["driver_phone"])
        if driver:
            send_text(driver["phone"], driver_message(driver, f"Customer cancelled {ref}. You are available again.", f"El cliente canceló {ref}. Ya estás disponible."))
    return f"Order {ref} has been cancelled." if order["language"] == "en" else f"El pedido {ref} ha sido cancelado."


def handle_customer_message(phone: str, text: str, interactive_id: str = "", shared_location: Optional[dict] = None) -> None:
    phone = normalize_phone(phone)
    if not phone:
        return
    low = (text or "").strip().lower()
    button_id = (interactive_id or "").strip()
    state = session_get(phone)
    language = state.get("language", "en")

    if state.get("stage") == "LANGUAGE" or button_id in {"LANG_EN", "LANG_ES"}:
        if button_id == "LANG_EN" or low in {"en", "english", "1"} or "english" in low:
            language = "en"
        elif button_id == "LANG_ES" or low in {"es", "español", "espanol", "spanish", "2"} or "español" in low or "espanol" in low:
            language = "es"
        else:
            language_prompt(phone)
            return
        resume = state.pop("resume_state", None)
        customer_save(phone, language=language)
        resume_after_language_change(phone, language, resume)
        return

    # Resolve any live order decision before allowing menu navigation. This
    # prevents a driver from remaining reserved while the customer wanders away.
    cancellation = handle_customer_active_order_cancel(phone, text)
    if cancellation is not None:
        send_text(phone, cancellation)
        return
    guarantee_reply = handle_customer_guarantee_reply(phone, text, button_id)
    if guarantee_reply is not None:
        send_text(phone, guarantee_reply)
        return
    no_driver_reply = handle_no_driver_reply(phone, text, button_id)
    if no_driver_reply is not None:
        send_text(phone, no_driver_reply)
        return

    # Global navigation after pending delivery decisions have been resolved.
    if button_id == "APP_LANGUAGE" or low in {"language", "idioma", "change language", "cambiar idioma"} or (low == "9" and state.get("stage") in {"APP_HOME", "PGP_MENU"}):
        language_prompt(phone)
        return
    if low in {"pgp menu", "menu principal", "menú principal", "main menu"}:
        numbered_pgp_menu(phone, language)
        return
    if low in {"home", "start over", "restart", "reiniciar", "inicio"} or button_id == "APP_HOME":
        app_home(phone, language, welcome=False)
        return

    # Returning-customer history and one-touch reorder remain inside WhatsApp.
    if low in {"history", "historial", "my orders", "mis pedidos"}:
        with db_connection() as connection:
            recent = fetch_all(connection, "SELECT ref,status,total_mxn,created_at FROM pgp_orders WHERE customer_phone=? ORDER BY id DESC LIMIT 5", (phone,))
        if not recent:
            send_text(phone, "Todavía no tienes pedidos." if language == "es" else "You do not have any orders yet.")
        else:
            lines = ["📋 *TUS ÚLTIMOS PEDIDOS*" if language == "es" else "📋 *YOUR RECENT ORDERS*"]
            for order_row in recent:
                order_item = row_dict(order_row)
                lines.append(f"{order_item['ref']} — {order_item['status']} — MX${order_item['total_mxn']} — {order_item['created_at']}")
            send_text(phone, "\n".join(lines))
        return
    if low in {"reorder", "repeat", "repetir", "repetir último", "repetir ultimo"} or button_id == "REORDER_LAST":
        with db_connection() as connection:
            last = fetch_one(connection, "SELECT * FROM pgp_orders WHERE customer_phone=? ORDER BY id DESC LIMIT 1", (phone,))
            last_order = row_dict(last)
            last_items = fetch_all(connection, "SELECT pizza_id,quantity FROM pgp_order_items WHERE order_id=? ORDER BY id", (last_order['id'],)) if last_order else []
        if not last_order or not last_items:
            send_text(phone, "No encontramos un pedido anterior para repetir." if language == "es" else "We could not find a previous order to repeat.")
            return
        state["cart"] = [{"pizza_id": item["pizza_id"], "quantity": int(item["quantity"])} for item in last_items]
        state["stage"] = "PIZZA"
        state["language"] = language
        session_save(phone, state)
        show_cart_actions(phone, language)
        return

    state = session_get(phone)
    stage = state.get("stage", "APP_HOME")
    language = state.get("language", language)

    # App-choice screen. Interactive IDs avoid colliding with the numbered PGP menu.
    if stage == "APP_HOME":
        choice = button_id
        if not choice and low in {"1", "2", "3", "4", "5"}:
            choice = {"1": "APP_DELIVERY", "2": "APP_LOCATION", "3": "APP_MENU", "4": "APP_HELP", "5": "APP_LANGUAGE"}[low]
        if choice == "APP_DELIVERY":
            state.update({"stage": "PIZZA", "cart": []})
            session_save(phone, state)
            send_pizza_selection(phone, language)
            return
        if choice == "APP_LOCATION":
            location_information(phone, language)
            return
        if choice == "APP_MENU":
            show_menu(phone, language)
            return
        if choice == "APP_HELP":
            help_information(phone, language)
            return
        if choice == "APP_LANGUAGE":
            language_prompt(phone)
            return
        app_home(phone, language, welcome=False)
        return

    if stage == "PGP_MENU":
        if button_id == "PGP_PICKUP" or low in {"2", "pickup", "recoger"}:
            pickup_instructions(phone, language)
            return
        if button_id == "APP_DELIVERY" or low in {"4", "delivery", "entrega"}:
            state.update({"stage": "PIZZA", "cart": []})
            session_save(phone, state)
            send_pizza_selection(phone, language)
            return
        if low == "1":
            show_menu(phone, language)
            return
        if low == "3":
            location_information(phone, language)
            return
        if low == "0":
            app_home(phone, language, welcome=False)
            return
        numbered_pgp_menu(phone, language)
        return

    if stage == "PIZZA":
        remove_match = re.fullmatch(r"(?:remove|quitar)\s+(\d{1,2})", low)
        if remove_match:
            index = int(remove_match.group(1))
            if not 1 <= index <= len(MENU):
                send_text(phone, "Número de pizza no válido." if language == "es" else "Invalid pizza number.")
                return
            pizza_id = MENU[index - 1]["id"]
            state["cart"] = [entry for entry in state.get("cart", []) if entry["pizza_id"] != pizza_id]
            session_save(phone, state)
            if state["cart"]:
                show_cart_actions(phone, language)
            else:
                send_pizza_selection(phone, language)
            return
        set_match = re.fullmatch(r"(?:set|change|cambiar)\s+(\d{1,2})\s*[x×]\s*(\d{1,2})", low)
        if set_match:
            index, quantity = int(set_match.group(1)), int(set_match.group(2))
            if not 1 <= index <= len(MENU) or not 0 <= quantity <= 20:
                send_text(phone, "Pizza o cantidad no válida." if language == "es" else "Invalid pizza or quantity.")
                return
            pizza_id = MENU[index - 1]["id"]
            existing = next((entry for entry in state.get("cart", []) if entry["pizza_id"] == pizza_id), None)
            if quantity == 0:
                state["cart"] = [entry for entry in state.get("cart", []) if entry["pizza_id"] != pizza_id]
            elif existing:
                existing["quantity"] = quantity
            else:
                _add_to_cart(state, index, quantity)
            session_save(phone, state)
            if state["cart"]:
                show_cart_actions(phone, language)
            else:
                send_pizza_selection(phone, language)
            return
        if button_id.startswith("PIZZA_"):
            pizza_id = button_id[len("PIZZA_"):]
            if pizza_id not in MENU_BY_ID:
                send_pizza_selection(phone, language)
                return
            state["pending_pizza_id"] = pizza_id
            state["stage"] = "QUANTITY"
            session_save(phone, state)
            pizza = MENU_BY_ID[pizza_id]
            name = pizza["es"] if language == "es" else pizza["en"]
            send_text(phone, f"¿Cuántas pizzas de {name} deseas?" if language == "es" else f"How many {name} pizzas would you like?")
            return
        if button_id == "CART_CHECKOUT" or low in {"0", "done", "listo", "checkout", "continuar", "finish", "terminar"}:
            if not state.get("cart"):
                send_text(phone, "Elige al menos una pizza." if language == "es" else "Choose at least one pizza.")
                send_pizza_selection(phone, language)
                return
            _ask_customer_name(phone, state)
            return
        if button_id == "CART_CLEAR" or low in {"clear", "clear cart", "clear order", "vaciar", "vaciar pedido"}:
            state["cart"] = []
            session_save(phone, state)
            send_pizza_selection(phone, language)
            return
        if button_id == "CART_ADD" or low in {"add", "add another", "agregar", "agregar otra"}:
            send_pizza_selection(phone, language)
            return
        # Text shortcut: 1, 1 x2, or 1x2.
        match = re.fullmatch(r"\s*(\d{1,2})(?:\s*[x×]\s*(\d{1,2}))?\s*", text or "", re.I)
        if match:
            index = int(match.group(1))
            quantity = int(match.group(2) or "1")
            if not 1 <= index <= len(MENU) or not 1 <= quantity <= 20:
                send_text(phone, "Cantidad o pizza no válida." if language == "es" else "Invalid pizza or quantity.")
                return
            _add_to_cart(state, index, quantity)
            session_save(phone, state)
            show_cart_actions(phone, language)
            return
        if not state.get("cart"):
            send_pizza_selection(phone, language)
        else:
            show_cart_actions(phone, language)
        return

    if stage == "QUANTITY":
        try:
            quantity = int(text.strip())
        except (TypeError, ValueError):
            quantity = 0
        if not 1 <= quantity <= 20:
            send_text(phone, "Escribe una cantidad entre 1 y 20." if language == "es" else "Enter a quantity from 1 to 20.")
            return
        pizza_id = state.get("pending_pizza_id")
        index = next((i for i, item in enumerate(MENU, 1) if item["id"] == pizza_id), 0)
        if not index:
            state["stage"] = "PIZZA"
            session_save(phone, state)
            send_pizza_selection(phone, language)
            return
        _add_to_cart(state, index, quantity)
        state.pop("pending_pizza_id", None)
        state["stage"] = "PIZZA"
        session_save(phone, state)
        show_cart_actions(phone, language)
        return

    if stage == "NAME_CONFIRM":
        if button_id == "NAME_YES" or low in {"1", "yes", "sí", "si"}:
            customer = customer_get(phone) or {}
            state["name"] = customer.get("name", "")
            _ask_address(phone, state)
            return
        if button_id == "NAME_NEW" or low in {"2", "no", "change", "cambiar"}:
            state["stage"] = "NAME"
            session_save(phone, state)
            send_text(phone, "¿Cuál es tu nombre?" if language == "es" else "What is your name?")
            return
        send_text(phone, "Responde 1 para usarlo o 2 para cambiarlo." if language == "es" else "Reply 1 to use it or 2 to change it.")
        return

    if stage == "NAME":
        if len((text or "").strip()) < 2:
            send_text(phone, "Escribe tu nombre completo." if language == "es" else "Please enter your name.")
            return
        state["name"] = text.strip()
        _ask_address(phone, state)
        return

    if stage == "ADDRESS_CONFIRM":
        if button_id == "ADDRESS_YES" or low in {"1", "yes", "sí", "si"}:
            customer = customer_get(phone) or {}
            state["address"] = customer.get("address", "")
            _ask_instructions(phone, state)
            return
        if button_id == "ADDRESS_NEW" or low in {"2", "no", "new", "nueva", "new address", "nueva dirección"}:
            state["stage"] = "ADDRESS"
            session_save(phone, state)
            send_text(phone, "Escribe tu dirección completa o comparte tu ubicación." if language == "es" else "Enter your full address or share your WhatsApp location.")
            return
        send_text(phone, "Responde 1 para usarla o 2 para cambiarla." if language == "es" else "Reply 1 to use it or 2 to change it.")
        return

    if stage == "ADDRESS":
        address = (text or "").strip()
        if shared_location:
            lat, lon = shared_location.get("latitude"), shared_location.get("longitude")
            try:
                address = f"Shared WhatsApp location: https://www.google.com/maps/search/?api=1&query={float(lat)},{float(lon)}"
            except (TypeError, ValueError):
                address = ""
        if len(address) < 5:
            send_text(phone, "Escribe una dirección completa o comparte tu ubicación." if language == "es" else "Enter a full address or share your location.")
            return
        state["address"] = address
        _ask_instructions(phone, state)
        return

    if stage == "INSTRUCTIONS_CONFIRM":
        if button_id == "INSTRUCTIONS_YES" or low in {"1", "yes", "sí", "si"}:
            customer = customer_get(phone) or {}
            state["instructions"] = customer.get("instructions", "")
            build_confirmation(phone, state)
            return
        if button_id == "INSTRUCTIONS_NONE" or low in {"3", "none", "ninguna", "no"}:
            state["instructions"] = ""
            build_confirmation(phone, state)
            return
        if button_id == "INSTRUCTIONS_NEW" or low in {"2", "change", "cambiar"}:
            state["stage"] = "INSTRUCTIONS"
            session_save(phone, state)
            send_text(phone, "Escribe las instrucciones o responde *NINGUNA*." if language == "es" else "Enter instructions or reply *NONE*.")
            return
        send_text(phone, "Elige Sí, Cambiar o Ninguna." if language == "es" else "Choose Yes, Change or None.")
        return

    if stage == "INSTRUCTIONS":
        state["instructions"] = "" if low in {"none", "ninguna", "no", "nada"} else (text or "").strip()
        build_confirmation(phone, state)
        return

    if stage == "CONFIRM":
        if button_id == "EDIT_ORDER" or low in {"2", "edit", "edit order", "edit pizzas", "editar", "editar pedido", "editar pizzas"}:
            state["stage"] = "PIZZA"
            state.pop("quote", None)
            session_save(phone, state)
            send_pizza_selection(phone, language)
            return
        if low in {"edit address", "change address", "editar dirección", "cambiar dirección"}:
            state["stage"] = "ADDRESS"
            session_save(phone, state)
            send_text(phone, "Escribe la nueva dirección." if language == "es" else "Enter the new delivery address.")
            return
        if low in {"edit instructions", "change instructions", "editar instrucciones", "cambiar instrucciones"}:
            state["stage"] = "INSTRUCTIONS"
            session_save(phone, state)
            send_text(phone, "Escribe las nuevas instrucciones o NINGUNA." if language == "es" else "Enter the new instructions or NONE.")
            return
        if button_id == "CANCEL_CHECKOUT" or low in {"cancel", "cancelar", "3"}:
            session_reset(phone, language)
            send_text(phone, "Pedido cancelado. No se creó ningún pedido." if language == "es" else "Checkout cancelled. No order was created.")
            app_home(phone, language, welcome=False)
            return
        if button_id == "PLACE_DIRECT_DELIVERY" or low in {"place direct delivery", "place delivery", "confirm", "confirm order", "confirmar", "confirmar pedido", "colocar entrega directa", "1", "yes", "sí", "si"}:
            try:
                order = persist_order(phone, state)
            except PriceQuoteChanged as changed:
                # A day/night boundary changed between quote and confirmation;
                # show the new total and require a new explicit confirmation.
                state["stage"] = "CONFIRM"
                state["quote"] = changed.quote
                session_save(phone, state)
                send_text(phone, "El precio cambió por el horario. Revisa el nuevo total y confirma otra vez." if language == "es" else "The delivery period changed. Please review the updated total and confirm again.")
                build_confirmation(phone, state, force_new_quote=True)
                return
            except Exception:
                log.exception("Failed to create Picante Direct order for %s", phone)
                send_text(phone, "No pudimos crear el pedido. Tus datos siguen guardados; responde *CONFIRMAR* para intentar de nuevo." if language == "es" else "We could not create the order. Your details are saved; reply *CONFIRM* to try again.")
                return
            session_reset(phone, language)
            customer_message = (f"✅ *PEDIDO CONFIRMADO — {order['ref']}*\n\nTotal: *MX${order['total_mxn']}*\n\nEstamos buscando un conductor Picante Direct. Te avisaremos cuando se encuentre uno."
                                if language == "es" else
                                f"✅ *ORDER CONFIRMED — {order['ref']}*\n\nTotal: *MX${order['total_mxn']}*\n\nWe are finding a Picante Direct driver now. We will update you when one is found.")
            send_text(phone, customer_message)
            dispatch_order(order)
            return
        send_text(phone, "Responde CONFIRMAR para colocar el pedido o CANCELAR." if language == "es" else "Reply CONFIRM to place the order or CANCEL to stop.")
        return

    app_home(phone, language, welcome=False)

# ---------------------------------------------------------------------------
# Webhook parsing, verification, idempotency and health
# ---------------------------------------------------------------------------

def parse_whatsapp_message(data: dict) -> Optional[dict]:
    try:
        value = data["entry"][0]["changes"][0]["value"]
        messages = value.get("messages", [])
        if not messages:
            return None
        message = messages[0]
        msg_type = message.get("type")
        parsed = {
            "id": message.get("id", ""),
            "from": normalize_phone(message.get("from", "")),
            "text": "",
            "interactive_id": "",
            "location": None,
            "type": msg_type,
        }
        if msg_type == "text":
            parsed["text"] = message.get("text", {}).get("body", "").strip()
        elif msg_type == "interactive":
            interactive = message.get("interactive", {})
            button = interactive.get("button_reply", {})
            list_reply = interactive.get("list_reply", {})
            parsed["interactive_id"] = button.get("id") or list_reply.get("id") or ""
            parsed["text"] = button.get("title") or list_reply.get("title") or ""
        elif msg_type == "location":
            parsed["location"] = message.get("location", {})
        return parsed
    except (KeyError, IndexError, TypeError):
        log.exception("Could not parse WhatsApp webhook message")
        return None


def mark_message_processed(message_id: str) -> bool:
    if not message_id:
        return True
    try:
        with db_connection(immediate=not _is_postgres()) as connection:
            if _is_postgres():
                cursor = execute(connection, "INSERT INTO pgp_processed_messages(message_id,processed_at) VALUES(?,?) ON CONFLICT(message_id) DO NOTHING", (message_id, iso_now()))
            else:
                cursor = execute(connection, "INSERT OR IGNORE INTO pgp_processed_messages(message_id,processed_at) VALUES(?,?)", (message_id, iso_now()))
            return cursor.rowcount == 1
    except Exception:
        log.exception("Webhook idempotency insert failed")
        return False


def verify_meta_signature(raw_body: bytes, signature: str) -> bool:
    if not META_APP_SECRET:
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(META_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + digest, signature)


@app.get("/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and VERIFY_TOKEN and hmac.compare_digest(token or "", VERIFY_TOKEN):
        return challenge, 200
    return "Verification failed", 403


@app.post("/webhook")
def receive_webhook():
    raw = request.get_data(cache=True)
    if not verify_meta_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
        return "Invalid signature", 403
    data = request.get_json(silent=True) or {}
    parsed = parse_whatsapp_message(data)
    if not parsed:
        return "EVENT_RECEIVED", 200
    if not mark_message_processed(parsed["id"]):
        return "EVENT_RECEIVED", 200
    phone = parsed["from"]
    if not phone:
        return "EVENT_RECEIVED", 200
    try:
        driver_reply = driver_flow(phone, parsed["text"])
        if driver_reply is not None:
            send_text(phone, driver_reply)
        else:
            handle_customer_message(phone, parsed["text"], parsed["interactive_id"], parsed["location"])
    except Exception:
        log.exception("Message processing failed while handling a WhatsApp customer/driver message")
        language = (customer_get(phone) or {}).get("language", "en")
        send_text(phone, "Tuvimos un problema temporal. Escribe *INICIO* para volver al menú." if language == "es" else
                  "We had a temporary problem. Reply *START* to return to the menu.")
    return "EVENT_RECEIVED", 200


@app.get("/health")
def health():
    try:
        with db_connection() as connection:
            fetch_one(connection, "SELECT 1 AS ok")
        db_status = "ok"
    except Exception:
        log.exception("Health check database test failed")
        db_status = "error"
    return jsonify({
        "status": "ok" if db_status == "ok" else "degraded",
        "database": db_status,
        "whatsapp_configured": bool(WHATSAPP_TOKEN and PHONE_NUMBER_ID and VERIFY_TOKEN),
        "service": "Picante Gourmet Pizza / Picante Direct",
    }), (200 if db_status == "ok" else 503)


@app.get("/privacy")
def privacy():
    return ("<!doctype html><html><head><title>PGP Privacy</title></head><body>"
            "<h1>Picante Gourmet Pizza Privacy</h1>"
            "<p>Information shared through WhatsApp is used to provide menu information, pickup instructions, delivery service and customer support.</p>"
            "<p>Customer information is not sold. Customers may request correction or deletion by contacting Picante Gourmet Pizza through WhatsApp.</p>"
            f"<p>Contact: {BUSINESS_PHONE}</p></body></html>", 200)

# ---------------------------------------------------------------------------
# Five-minute watchdog. A conditional DB update prevents duplicate alerts
# when more than one application worker is running.
# ---------------------------------------------------------------------------
def run_no_driver_check(now: Optional[datetime] = None) -> int:
    current = now or now_utc()
    cutoff = current - timedelta(minutes=NO_DRIVER_MINUTES)
    with db_connection() as connection:
        candidates = [row_dict(row) for row in fetch_all(connection,
            "SELECT * FROM pgp_orders WHERE status='OPEN' AND no_driver_notice_at IS NULL")]
    sent = 0
    for order in candidates:
        try:
            dispatch_time = datetime.fromisoformat(order["dispatch_at"])
            if dispatch_time.tzinfo is None:
                dispatch_time = dispatch_time.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if dispatch_time > cutoff:
            continue
        # Claim the one-time alert atomically before sending it.
        with db_connection(immediate=not _is_postgres()) as connection:
            cursor = execute(connection, "UPDATE pgp_orders SET no_driver_notice_at=? WHERE ref=? AND status='OPEN' AND no_driver_notice_at IS NULL", (current.isoformat(), order["ref"]))
            should_send = cursor.rowcount == 1
        if not should_send:
            continue
        if order["language"] == "es":
            body = (f"⚠️ *{order['ref']} — ACTUALIZACIÓN DE ENTREGA*\n\n"
                    "Todavía no hemos confirmado un conductor.\n\n"
                    "1 — Continuar buscando conductor. Si encontramos uno, te pediremos aceptar una nueva garantía de 30 minutos.\n"
                    "2 — Cancelar este pedido.")
            buttons = [(f"NO_DRIVER_CONTINUE_{order['ref']}", "Continuar"), (f"NO_DRIVER_CANCEL_{order['ref']}", "Cancelar")]
        else:
            body = (f"⚠️ *{order['ref']} — DELIVERY SEARCH UPDATE*\n\n"
                    "We have not confirmed a driver yet.\n\n"
                    "1 — Continue searching. If we find a driver, we will ask you to accept a fresh 30-minute guarantee.\n"
                    "2 — Cancel this order.")
            buttons = [(f"NO_DRIVER_CONTINUE_{order['ref']}", "Continue"), (f"NO_DRIVER_CANCEL_{order['ref']}", "Cancel")]
        send_buttons(order["customer_phone"], body, buttons)
        sent += 1
    return sent


def _watchdog_loop():
    while True:
        try:
            run_no_driver_check()
        except Exception:
            log.exception("Five-minute no-driver watchdog failure")
        time.sleep(30)


if os.getenv("DISABLE_BACKGROUND_JOBS", "0") != "1":
    threading.Thread(target=_watchdog_loop, name="pgp-no-driver-watchdog", daemon=True).start()

# ---------------------------------------------------------------------------
# Authenticated internal administration — never a customer ordering surface
# ---------------------------------------------------------------------------

def admin_authorized() -> bool:
    key = request.headers.get("X-Admin-Key", "")
    if ADMIN_KEY and hmac.compare_digest(key, ADMIN_KEY):
        return True
    auth = request.authorization
    if auth and ADMIN_USERNAME and ADMIN_PASSWORD:
        return (hmac.compare_digest(auth.username or "", ADMIN_USERNAME)
                and hmac.compare_digest(auth.password or "", ADMIN_PASSWORD))
    return False


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not admin_authorized():
            return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="PGP Operations"'})
        return fn(*args, **kwargs)
    return wrapped


ADMIN_BASE = """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>PGP / Picante Direct Operations</title><style>
body{font-family:Arial,sans-serif;background:#111;color:#f5f5f5;margin:0;padding:20px}main{max-width:1250px;margin:auto}
nav{display:flex;gap:14px;flex-wrap:wrap;padding:12px;background:#222;border-radius:8px}a{color:#baff00}h1,h2{color:#baff00}
.card{background:#202020;padding:16px;border-radius:9px;margin:12px 0;overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:9px;border-bottom:1px solid #444;text-align:left;vertical-align:top}input,select{padding:9px;margin:5px 0;background:#111;color:#fff;border:1px solid #555;border-radius:5px}
button{padding:9px 13px;background:#baff00;color:#111;border:0;border-radius:5px;font-weight:bold;cursor:pointer}.stats{display:flex;gap:12px;flex-wrap:wrap}.stat{background:#202020;padding:18px;border-radius:8px;min-width:160px}.number{font-size:28px;font-weight:bold}
label{display:inline-block;margin-right:12px}.muted{color:#aaa}.formrow{display:flex;gap:8px;flex-wrap:wrap;align-items:end}
</style></head><body><main><h1>PGP / PICANTE DIRECT OPERATIONS</h1><nav>
<a href='/admin'>Dashboard</a><a href='/admin/orders'>Orders</a><a href='/admin/drivers'>Drivers</a><a href='/admin/settlements'>Settlements</a><a href='/admin/cards'>Company Cards</a>
</nav><div class='card'>{{content|safe}}</div></main></body></html>"""


def admin_page(content: str):
    return render_template_string(ADMIN_BASE, content=content)


@app.get("/admin")
@admin_required
def admin_dashboard():
    with db_connection() as connection:
        open_count = row_value(fetch_one(connection, "SELECT COUNT(*) AS n FROM pgp_orders WHERE status IN ('OPEN','AWAITING_CUSTOMER_GUARANTEE','ASSIGNED','EN_ROUTE')"), "n", 0)
        driver_count = row_value(fetch_one(connection, "SELECT COUNT(*) AS n FROM pgp_drivers WHERE approved=1 AND active=1"), "n", 0)
        unsettled_count = row_value(fetch_one(connection, "SELECT COUNT(*) AS n FROM pgp_deliveries WHERE settlement_id IS NULL"), "n", 0)
        office_due = row_value(fetch_one(connection, "SELECT COALESCE(SUM(amount_due_office_mxn),0) AS n FROM pgp_deliveries WHERE settlement_id IS NULL"), "n", 0)
        recent = fetch_all(connection, "SELECT ref,customer_name,total_mxn,status,created_at FROM pgp_orders ORDER BY id DESC LIMIT 10")
    rows = "".join(f"<tr><td>{html_escape(str(r['ref']))}</td><td>{html_escape(str(r['customer_name']))}</td><td>MX${int(r['total_mxn'])}</td><td>{html_escape(str(r['status']))}</td><td>{html_escape(str(r['created_at']))}</td></tr>" for r in recent)
    content = (f"<div class='stats'><div class='stat'>Active orders<div class='number'>{open_count}</div></div>"
               f"<div class='stat'>Approved active drivers<div class='number'>{driver_count}</div></div>"
               f"<div class='stat'>Unsettled deliveries<div class='number'>{unsettled_count}</div></div>"
               f"<div class='stat'>Amount due to office<div class='number'>MX${office_due}</div></div></div>"
               "<h2>Recent orders</h2><table><tr><th>Reference</th><th>Customer</th><th>Total</th><th>Status</th><th>Created</th></tr>"+rows+"</table>")
    return admin_page(content)


@app.get("/admin/orders")
@admin_required
def admin_orders():
    with db_connection() as connection:
        orders = [row_dict(row) for row in fetch_all(connection, "SELECT * FROM pgp_orders ORDER BY id DESC LIMIT 200")]
        drivers = [row_dict(row) for row in fetch_all(connection, "SELECT * FROM pgp_drivers WHERE approved=1 AND active=1 AND busy=0 ORDER BY name")]
        items_by_order = {}
        for order in orders:
            items_by_order[order["id"]] = fetch_all(connection, "SELECT pizza_name,quantity FROM pgp_order_items WHERE order_id=?", (order["id"],))
    rows = []
    for order in orders:
        items = "<br>".join(f"{int(i['quantity'])} × {html_escape(str(i['pizza_name']))}" for i in items_by_order[order["id"]])
        action = ""
        if order["status"] == "OPEN" and drivers:
            options = "".join(f"<option value='{d['phone']}'>{d['name']}</option>" for d in drivers if driver_unsettled_count(d['phone']) < MAX_UNSETTLED_DELIVERIES_PER_DRIVER)
            action = (f"<form method='post' action='/admin/orders/{order['ref']}/assign'><select name='driver_phone'>{options}</select>"
                      "<button>Assign and request customer approval</button></form>")
        rows.append(f"<tr><td>{html_escape(str(order['ref']))}</td><td>{html_escape(str(order['customer_name']))}<br><span class='muted'>{html_escape(str(order['customer_phone']))}</span></td>"
                    f"<td>{items}</td><td>MX${int(order['total_mxn'])}</td><td>{html_escape(str(order['period']))}</td><td>{html_escape(str(order['status']))}</td><td>{html_escape(str(order.get('driver_name') or ''))}</td><td>{action}</td></tr>")
    return admin_page("<h2>Orders</h2><table><tr><th>Reference</th><th>Customer</th><th>Items</th><th>Total</th><th>Period</th><th>Status</th><th>Driver</th><th>Admin action</th></tr>"+"".join(rows)+"</table>")


@app.post("/admin/orders/<ref>/assign")
@admin_required
def admin_assign_order(ref):
    phone = normalize_phone(request.form.get("driver_phone", ""))
    ok, message, order = claim_order(phone, ref)
    if ok and order:
        driver = driver_get(phone)
        send_text(phone, driver_private_details(order, (driver or {}).get("language", "en")))
        notify_customer_driver_claimed(order)
    return redirect(url_for("admin_orders"))


@app.route("/admin/drivers", methods=["GET", "POST"])
@admin_required
def admin_drivers():
    if request.method == "POST":
        phone = normalize_phone(request.form.get("phone", ""))
        name = request.form.get("name", "").strip()
        language = request.form.get("language", "en")
        approved = 1 if request.form.get("approved") == "1" else 0
        if not phone or not name or language not in {"en", "es"}:
            abort(400)
        with db_connection() as connection:
            if _is_postgres():
                execute(connection, """INSERT INTO pgp_drivers(phone,name,language,approved,active,busy,created_at,updated_at)
                    VALUES(?,?,?,?,?,0,?,?) ON CONFLICT(phone) DO UPDATE SET name=EXCLUDED.name,language=EXCLUDED.language,approved=EXCLUDED.approved,active=1,updated_at=EXCLUDED.updated_at""",
                    (phone,name,language,approved,1,iso_now(),iso_now()))
            else:
                execute(connection, """INSERT INTO pgp_drivers(phone,name,language,approved,active,busy,created_at,updated_at)
                    VALUES(?,?,?,?,?,0,?,?) ON CONFLICT(phone) DO UPDATE SET name=excluded.name,language=excluded.language,approved=excluded.approved,active=1,updated_at=excluded.updated_at""",
                    (phone,name,language,approved,1,iso_now(),iso_now()))
        return redirect(url_for("admin_drivers"))
    with db_connection() as connection:
        drivers = fetch_all(connection, "SELECT * FROM pgp_drivers ORDER BY name")
    rows = "".join(f"<tr><td>{html_escape(str(d['name']))}</td><td>{html_escape(str(d['phone']))}</td><td>{html_escape(str(d['language']))}</td><td>{'Yes' if d['approved'] else 'No'}</td><td>{'Yes' if d['active'] else 'No'}</td><td>{'Busy' if d['busy'] else 'Available'}</td><td>{driver_unsettled_count(d['phone'])}</td>"
                   f"<td><form method='post' action='/admin/drivers/{html_escape(str(d['phone']))}/status'><button name='action' value='approve'>{'Unapprove' if d['approved'] else 'Approve'}</button> "
                   f"<button name='action' value='active'>{'Deactivate' if d['active'] else 'Activate'}</button></form></td></tr>" for d in drivers)
    form = ("<h3>Add / update driver</h3><form method='post' class='formrow'><label>Name<br><input name='name' required></label>"
            "<label>WhatsApp phone<br><input name='phone' required></label><label>Language<br><select name='language'><option value='en'>English</option><option value='es'>Español</option></select></label>"
            "<label>Approval<br><select name='approved'><option value='1'>Approved</option><option value='0'>Pending</option></select></label><button type='submit'>Save driver</button></form>")
    return admin_page("<h2>Drivers</h2>"+form+"<table><tr><th>Name</th><th>Phone</th><th>Language</th><th>Approved</th><th>Active</th><th>Status</th><th>Unsettled</th><th>Controls</th></tr>"+rows+"</table>")


@app.post("/admin/drivers/<phone>/status")
@admin_required
def admin_driver_status(phone):
    phone = normalize_phone(phone)
    action = request.form.get("action", "")
    with db_connection() as connection:
        driver = fetch_one(connection, "SELECT * FROM pgp_drivers WHERE phone=?", (phone,))
        if not driver:
            abort(404)
        if action == "approve":
            execute(connection, "UPDATE pgp_drivers SET approved=?,updated_at=? WHERE phone=?", (0 if driver['approved'] else 1, iso_now(), phone))
        elif action == "active":
            execute(connection, "UPDATE pgp_drivers SET active=?,updated_at=? WHERE phone=?", (0 if driver['active'] else 1, iso_now(), phone))
        else:
            abort(400)
    return redirect(url_for("admin_drivers"))


@app.get("/admin/settlements")
@admin_required
def admin_settlements():
    with db_connection() as connection:
        drivers = fetch_all(connection, "SELECT DISTINCT driver_phone FROM pgp_deliveries WHERE settlement_id IS NULL")
        history = fetch_all(connection, "SELECT * FROM pgp_settlements ORDER BY id DESC LIMIT 100")
        due_rows = []
        for driver in drivers:
            phone = driver["driver_phone"]
            row = fetch_one(connection, "SELECT COUNT(*) AS n,COALESCE(SUM(amount_due_office_mxn),0) AS total FROM pgp_deliveries WHERE driver_phone=? AND settlement_id IS NULL", (phone,))
            due_rows.append((phone, int(row["n"]), int(row["total"])))
    forms = []
    for phone, count, total in due_rows:
        forms.append(f"<tr><td>{html_escape(str(phone))}</td><td>{count}</td><td>MX${total}</td><td><form method='post' action='/admin/settlements/create'><input type='hidden' name='driver_phone' value='{html_escape(str(phone))}'><input name='note' placeholder='Note'><button>Record settlement</button></form></td></tr>")
    hist = "".join(f"<tr><td>{int(r['id'])}</td><td>{html_escape(str(r['driver_phone']))}</td><td>{int(r['delivery_count'])}</td><td>MX${int(r['amount_mxn'])}</td><td>{html_escape(str(r['note'] or ''))}</td><td>{html_escape(str(r['recorded_at']))}</td></tr>" for r in history)
    content = "<h2>Outstanding driver settlements</h2><table><tr><th>Driver</th><th>Deliveries</th><th>Amount due</th><th>Action</th></tr>"+"".join(forms)+"</table><h2>Settlement history</h2><table><tr><th>ID</th><th>Driver</th><th>Count</th><th>Amount</th><th>Note</th><th>Date</th></tr>"+hist+"</table>"
    return admin_page(content)


@app.post("/admin/settlements/create")
@admin_required
def create_settlement():
    phone = normalize_phone(request.form.get("driver_phone", ""))
    note = request.form.get("note", "").strip()
    with db_connection(immediate=not _is_postgres()) as connection:
        rows = fetch_all(connection, "SELECT id,amount_due_office_mxn FROM pgp_deliveries WHERE driver_phone=? AND settlement_id IS NULL", (phone,))
        if not rows:
            return redirect(url_for("admin_settlements"))
        total = sum(int(row["amount_due_office_mxn"]) for row in rows)
        if _is_postgres():
            cursor = execute(connection, "INSERT INTO pgp_settlements(driver_phone,delivery_count,amount_mxn,note,recorded_at) VALUES(?,?,?,?,?) RETURNING id", (phone,len(rows),total,note,iso_now()))
            settlement_id = cursor.fetchone()["id"]
        else:
            cursor = execute(connection, "INSERT INTO pgp_settlements(driver_phone,delivery_count,amount_mxn,note,recorded_at) VALUES(?,?,?,?,?)", (phone,len(rows),total,note,iso_now()))
            settlement_id = cursor.lastrowid
        for row in rows:
            execute(connection, "UPDATE pgp_deliveries SET settlement_id=? WHERE id=? AND settlement_id IS NULL", (settlement_id,row["id"]))
    return redirect(url_for("admin_settlements"))


@app.route("/admin/cards", methods=["GET", "POST"])
@admin_required
def admin_cards():
    if request.method == "POST":
        phone = normalize_phone(request.form.get("driver_phone", ""))
        ref = request.form.get("order_ref", "").strip().upper()
        card = request.form.get("card_label", "").strip()
        amount = max(0, int(request.form.get("amount_mxn", "0")))
        reference = request.form.get("reference", "").strip()
        if not order_get(ref, include_items=False) or not card:
            abort(400)
        with db_connection() as connection:
            execute(connection, "INSERT INTO pgp_card_transactions(driver_phone,order_ref,card_label,amount_mxn,reference,recorded_at) VALUES(?,?,?,?,?,?)",
                    (phone,ref,card,amount,reference,iso_now()))
            execute(connection, "UPDATE pgp_deliveries SET machine_purchase_mxn=machine_purchase_mxn+?,company_card=?,transaction_reference=? WHERE order_id=(SELECT id FROM pgp_orders WHERE ref=?)",
                    (amount,card,reference,ref))
        return redirect(url_for("admin_cards"))
    with db_connection() as connection:
        txs = fetch_all(connection, "SELECT * FROM pgp_card_transactions ORDER BY id DESC LIMIT 100")
    rows = "".join(f"<tr><td>{html_escape(str(t['driver_phone']))}</td><td>{html_escape(str(t['order_ref']))}</td><td>{html_escape(str(t['card_label']))}</td><td>MX${int(t['amount_mxn'])}</td><td>{html_escape(str(t['reference'] or ''))}</td><td>{html_escape(str(t['recorded_at']))}</td></tr>" for t in txs)
    form = ("<h3>Record company-card machine purchase</h3><form method='post' class='formrow'><label>Driver phone<br><input name='driver_phone' required></label>"
            "<label>Order reference<br><input name='order_ref' required></label><label>Card label<br><input name='card_label' required></label>"
            "<label>Amount MXN<br><input type='number' min='0' name='amount_mxn' required></label><label>Reference<br><input name='reference'></label><button>Record transaction</button></form>")
    return admin_page("<h2>Company card transactions</h2>"+form+"<table><tr><th>Driver</th><th>Order</th><th>Card</th><th>Amount</th><th>Reference</th><th>Date</th></tr>"+rows+"</table>")


@app.get("/")
def root():
    return jsonify({"service": "Picante Gourmet Pizza / Picante Direct", "status": "ok", "message": "Customer ordering is handled through the existing WhatsApp connection."})

# ---------------------------------------------------------------------------
# Test and local-run support
# ---------------------------------------------------------------------------

def _test_reset_database():
    """Test helper; only intended for an isolated test DATABASE_PATH."""
    if _is_postgres():
        raise RuntimeError("Test database reset is disabled for PostgreSQL")
    with db_connection() as connection:
        for table in ["pgp_processed_messages", "pgp_card_transactions", "pgp_settlements", "pgp_deliveries", "pgp_order_items", "pgp_orders", "pgp_drivers", "pgp_sessions", "pgp_customers"]:
            execute(connection, f"DELETE FROM {table}")


if __name__ == "__main__":  # pragma: no cover
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
