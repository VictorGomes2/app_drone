import os
import base64
import math
import random
import logging
import time
import urllib.request
import urllib.error
from threading import RLock, Thread, Event
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from sqlalchemy import text

# =========================================================
# AEROENGINE — API, AUTENTICAÇÃO E ESPAÇO AÉREO MULTIPLAYER
# =========================================================
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


# Entrega a interface principal do simulador na URL raiz do serviço.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

db_url = os.environ.get("DATABASE_URL", "sqlite:///local_aeroengine.db")

# Neon/Render: use the installed psycopg2-binary driver explicitly.
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


# =========================================================
# KEEP ALIVE — RENDER + NEON
# =========================================================
def env_bool(name, default=False):
    """Lê variáveis booleanas do Render com valores previsíveis."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "sim")


def env_int(name, default, minimum=1):
    """Lê intervalos numéricos sem derrubar a aplicação por configuração inválida."""
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        app.logger.warning("Variável %s inválida; usando %s segundos.", name, default)
        return default


KEEP_ALIVE_ENABLED = env_bool("KEEP_ALIVE_ENABLED", False)
KEEP_ALIVE_INTERVAL = env_int("KEEP_ALIVE_INTERVAL", 60, minimum=15)
KEEP_ALIVE_TIMEOUT = env_int("KEEP_ALIVE_TIMEOUT", 30, minimum=5)
KEEP_ALIVE_URL = os.environ.get(
    "KEEP_ALIVE_URL",
    "https://app-drone.onrender.com/healthz/render"
).strip()

KEEP_ALIVE_DB_ENABLED = env_bool("KEEP_ALIVE_DB_ENABLED", False)
KEEP_ALIVE_DB_INTERVAL = env_int("KEEP_ALIVE_DB_INTERVAL", 60, minimum=15)

_keep_alive_stop = Event()
_keep_alive_thread = None
_keep_alive_lock = RLock()


def keep_alive_render():
    """Faz uma requisição HTTP externa para a URL configurada do próprio serviço."""
    if not KEEP_ALIVE_URL:
        app.logger.warning(
            "Keep Alive Render está habilitado, mas KEEP_ALIVE_URL está vazia."
        )
        return

    request_obj = urllib.request.Request(
        KEEP_ALIVE_URL,
        headers={"User-Agent": "AeroEngine-KeepAlive/1.0"},
        method="GET"
    )

    try:
        with urllib.request.urlopen(
            request_obj, timeout=KEEP_ALIVE_TIMEOUT
        ) as response:
            status_code = response.getcode()
            if 200 <= status_code < 400:
                app.logger.info(
                    "[KEEP-ALIVE][RENDER] Sucesso: HTTP %s em %s",
                    status_code, KEEP_ALIVE_URL
                )
            else:
                app.logger.warning(
                    "[KEEP-ALIVE][RENDER] Resposta HTTP %s em %s",
                    status_code, KEEP_ALIVE_URL
                )
    except Exception:
        app.logger.exception(
            "[KEEP-ALIVE][RENDER] Falha ao acessar %s", KEEP_ALIVE_URL
        )


def keep_alive_neon():
    """Executa SELECT 1 para manter/verificar a conexão com PostgreSQL/Neon."""
    try:
        with app.app_context():
            db.session.execute(text("SELECT 1"))
            db.session.commit()
            app.logger.info("[KEEP-ALIVE][NEON] Banco respondeu ao SELECT 1.")
    except Exception:
        with app.app_context():
            db.session.rollback()
        app.logger.exception("[KEEP-ALIVE][NEON] Falha na consulta ao banco.")
    finally:
        with app.app_context():
            db.session.remove()


def keep_alive_worker():
    """Executa cada rotina no seu próprio intervalo, sem bloquear as requisições."""
    now = time.monotonic()
    next_render = now + KEEP_ALIVE_INTERVAL
    next_db = now + KEEP_ALIVE_DB_INTERVAL

    app.logger.info(
        "[KEEP-ALIVE] Worker iniciado | Render=%s (%ss) | Neon=%s (%ss)",
        KEEP_ALIVE_ENABLED, KEEP_ALIVE_INTERVAL,
        KEEP_ALIVE_DB_ENABLED, KEEP_ALIVE_DB_INTERVAL
    )

    while not _keep_alive_stop.is_set():
        now = time.monotonic()

        if KEEP_ALIVE_ENABLED and now >= next_render:
            keep_alive_render()
            next_render = time.monotonic() + KEEP_ALIVE_INTERVAL

        if KEEP_ALIVE_DB_ENABLED and now >= next_db:
            keep_alive_neon()
            next_db = time.monotonic() + KEEP_ALIVE_DB_INTERVAL

        enabled_intervals = []
        if KEEP_ALIVE_ENABLED:
            enabled_intervals.append(KEEP_ALIVE_INTERVAL)
        if KEEP_ALIVE_DB_ENABLED:
            enabled_intervals.append(KEEP_ALIVE_DB_INTERVAL)

        wait_seconds = min(enabled_intervals) if enabled_intervals else 60
        _keep_alive_stop.wait(min(5, max(1, wait_seconds)))


def start_keep_alive_worker():
    """Inicia somente uma thread Keep Alive por processo Python."""
    global _keep_alive_thread

    if not (KEEP_ALIVE_ENABLED or KEEP_ALIVE_DB_ENABLED):
        app.logger.info("[KEEP-ALIVE] Rotinas desativadas pelas variáveis de ambiente.")
        return

    with _keep_alive_lock:
        if _keep_alive_thread and _keep_alive_thread.is_alive():
            return

        _keep_alive_stop.clear()
        _keep_alive_thread = Thread(
            target=keep_alive_worker,
            name="AeroEngineKeepAlive",
            daemon=True
        )
        _keep_alive_thread.start()


socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    ping_interval=25,
    ping_timeout=60,
    max_http_buffer_size=1_000_000,
)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    approved = db.Column(db.Boolean, default=False)
    flightTimeSec = db.Column(db.Integer, default=0)
    missions = db.Column(db.JSON, default=lambda: [False] * 15)

# Pontos de nascimento posicionados em cruzamentos da malha viária.
SPAWN_POINTS = [
    {"x": 60, "y": 2, "z": 60},
    {"x": -60, "y": 2, "z": 180},
    {"x": 180, "y": 2, "z": -60},
    {"x": -180, "y": 2, "z": -180},
    {"x": 300, "y": 2, "z": 60},
    {"x": -300, "y": 2, "z": 60},
    {"x": 60, "y": 2, "z": -300},
    {"x": 60, "y": 2, "z": 300},
    {"x": 420, "y": 2, "z": -180},
    {"x": -420, "y": 2, "z": 300},
]

active_pilots = {}
spawn_index = 0
state_lock = RLock()
delivery_scheduler_started = False

def safe_number(value, default=0.0, limit=10000):
    try:
        number = float(value)
        if not math.isfinite(number):
            return default
        return max(-limit, min(limit, number))
    except (TypeError, ValueError):
        return default

def public_airspace():
    return {
        sid: {
            "id": sid,
            "username": pilot["username"],
            "droneType": pilot["droneType"],
            "position": dict(pilot["position"]),
            "rotation": dict(pilot["rotation"]),
            "weapon": pilot["weapon"],
            "score": pilot["score"],
            "health": pilot.get("health", 100),
        }
        for sid, pilot in active_pilots.items()
    }

def broadcast_airspace():
    snapshot = public_airspace()
    socketio.emit("airspace_update", snapshot)
    socketio.emit("player_count", {"count": len(snapshot)})


def delivery_scheduler():
    """Publica uma encomenda em um dos pontos a cada ciclo do multiplayer."""
    while True:
        socketio.sleep(60)
        with state_lock:
            has_players = bool(active_pilots)
        if not has_players:
            continue
        event_data = {"padIndex": random.randrange(4), "expiresIn": 45000}
        socketio.emit("delivery_available", event_data)
        socketio.sleep(45)
        socketio.emit("delivery_expired", {})

@app.get("/healthz/render")
def healthz_render():
    """Endpoint leve para verificar se o backend está respondendo."""
    return jsonify({
        "status": "ok",
        "service": "aeroengine",
        "backend": "ok",
        "online": len(active_pilots)
    }), 200


@app.get("/healthz/neon")
def healthz_neon():
    """Verifica a conexão real com o banco PostgreSQL/Neon."""
    try:
        db.session.execute(text("SELECT 1"))
        db.session.remove()
        return jsonify({
            "status": "ok",
            "service": "aeroengine",
            "database": "ok",
            "check": "SELECT 1"
        }), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("[HEALTHZ][NEON] Falha na verificação do banco.")
        db.session.remove()
        return jsonify({
            "status": "error",
            "service": "aeroengine",
            "database": "unavailable"
        }), 503


@app.get("/healthz")
def healthz():
    """Verifica backend e banco; retorna 503 se o banco estiver indisponível."""
    try:
        db.session.execute(text("SELECT 1"))
        db.session.remove()
        return jsonify({
            "status": "ok",
            "service": "aeroengine",
            "backend": "ok",
            "database": "ok",
            "online": len(active_pilots)
        }), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("[HEALTHZ] Banco indisponível.")
        db.session.remove()
        return jsonify({
            "status": "degraded",
            "service": "aeroengine",
            "backend": "ok",
            "database": "error",
            "online": len(active_pilots)
        }), 503

@app.get("/api/online")
def online_status():
    return jsonify({"online": len(active_pilots), "players": [
        {"username": p["username"], "droneType": p["droneType"], "score": p["score"]}
        for p in active_pilots.values()
    ]})

@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip().upper()
    password = str(data.get("password", ""))
    if not username or not password:
        return jsonify({"success": False, "msg": "Preencha todos os campos."}), 400
    if len(username) > 80 or len(password) > 256:
        return jsonify({"success": False, "msg": "Usuário ou senha excede o limite permitido."}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"success": False, "msg": "Usuário já existe na base."}), 409

    is_first = User.query.count() == 0
    encoded_password = base64.b64encode(password.encode("utf-8")).decode("utf-8")
    new_user = User(
        username=username,
        password=encoded_password,
        approved=is_first or username == "ADMIN",
        missions=[False] * 15,
    )
    db.session.add(new_user)
    db.session.commit()
    return jsonify({"success": True, "msg": "Solicitação enviada ao Comando."})

@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip().upper()
    password = str(data.get("password", ""))
    user = User.query.filter_by(username=username).first()
    if not user:
        return jsonify({"success": False, "msg": "Usuário não encontrado."}), 404
    encoded_password = base64.b64encode(password.encode("utf-8")).decode("utf-8")
    if user.password != encoded_password:
        return jsonify({"success": False, "msg": "Credenciais inválidas."}), 401
    if not user.approved:
        return jsonify({"success": False, "msg": "Acesso pendente de liberação."}), 403

    user_data = {
        "username": user.username,
        "approved": user.approved,
        "flightTimeSec": user.flightTimeSec,
        "missions": user.missions or [False] * 15,
    }
    return jsonify({"success": True, "userData": user_data})

# -------------------------- Socket.IO --------------------------

@socketio.on("connect")
def handle_connect():
    print(f"[+] Rádio conectado: {request.sid}")

@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    with state_lock:
        existed = active_pilots.pop(sid, None)
    if existed:
        socketio.emit("drone_disconnected", sid)
        broadcast_airspace()
    print(f"[-] Rádio desconectado: {sid}")

@socketio.on("join_airspace")
def handle_join(data):
    global spawn_index, delivery_scheduler_started
    data = data if isinstance(data, dict) else {}
    username = str(data.get("username", "Piloto")).strip()[:24] or "Piloto"
    drone_type = str(data.get("droneType", "mini3"))[:24]

    with state_lock:
        spawn = dict(SPAWN_POINTS[spawn_index % len(SPAWN_POINTS)])
        spawn_index += 1
        active_pilots[request.sid] = {
            "id": request.sid,
            "username": username,
            "droneType": drone_type,
            "position": spawn,
            "rotation": {"pitch": 0.0, "yaw": 0.0, "roll": 0.0},
            "weapon": False,
            "score": 0,
            "has_package": False,
            "health": 100,
        }
        snapshot = public_airspace()

    if not delivery_scheduler_started:
        delivery_scheduler_started = True
        socketio.start_background_task(delivery_scheduler)

    emit("flight_clearance", {"spawnPoint": spawn})
    emit("airspace_update", snapshot)
    broadcast_airspace()

@socketio.on("leave_airspace")
def handle_leave():
    with state_lock:
        removed = active_pilots.pop(request.sid, None)
    if removed:
        socketio.emit("drone_disconnected", request.sid)
        broadcast_airspace()

@socketio.on("telemetry_update")
def handle_telemetry(data):
    if not isinstance(data, dict):
        return
    p = data.get("position") or {}
    r = data.get("rotation") or {}
    position = {
        "x": safe_number(p.get("x")),
        "y": safe_number(p.get("y")),
        "z": safe_number(p.get("z")),
    }
    rotation = {
        "pitch": safe_number(r.get("pitch"), limit=6.4),
        "yaw": safe_number(r.get("yaw"), limit=6.4),
        "roll": safe_number(r.get("roll"), limit=6.4),
    }
    with state_lock:
        pilot = active_pilots.get(request.sid)
        if not pilot:
            return
        pilot["position"] = position
        pilot["rotation"] = rotation
        pilot["droneType"] = str(data.get("droneType", pilot["droneType"]))[:24]
        payload = {
            "id": request.sid,
            "position": position,
            "rotation": rotation,
            "weapon": pilot["weapon"],
            "health": pilot.get("health", 100),
        }
    socketio.emit("remote_drone_moved", payload, skip_sid=request.sid)

@socketio.on("weapon_toggle")
def handle_weapon_toggle(data):
    enabled = bool((data or {}).get("enabled", False)) if isinstance(data, dict) else False
    with state_lock:
        pilot = active_pilots.get(request.sid)
        if not pilot:
            return
        pilot["weapon"] = enabled
        username = pilot["username"]
    socketio.emit("weapon_state", {"id": request.sid, "username": username, "enabled": enabled})

@socketio.on("fire_weapon")
def handle_fire_weapon(data):
    if not isinstance(data, dict):
        return
    origin = data.get("position") or {}
    direction = data.get("direction") or {}
    ox, oy, oz = (safe_number(origin.get(k)) for k in ("x", "y", "z"))
    dx, dy, dz = (safe_number(direction.get(k)) for k in ("x", "y", "z"))
    norm = math.sqrt(dx*dx + dy*dy + dz*dz)
    if norm < 0.001:
        return
    dx, dy, dz = dx/norm, dy/norm, dz/norm

    hit = None
    respawn_id = None
    with state_lock:
        shooter = active_pilots.get(request.sid)
        if not shooter or not shooter["weapon"]:
            return
        shooter_name = shooter["username"]
        for sid, target in active_pilots.items():
            if sid == request.sid or target.get("health", 100) <= 0:
                continue
            tp = target["position"]
            vx, vy, vz = tp["x"]-ox, tp["y"]-oy, tp["z"]-oz
            along = vx*dx + vy*dy + vz*dz
            if along < 0 or along > 500:
                continue
            perp2 = (vx-along*dx)**2 + (vy-along*dy)**2 + (vz-along*dz)**2
            if perp2 <= 25:
                target["health"] = max(0, target.get("health", 100) - 25)
                target["score"] = max(0, target["score"] - 10)
                hit = {"id": sid, "username": target["username"], "health": target["health"]}
                if target["health"] <= 0:
                    respawn_id = sid
                break

    socketio.emit("shot_fired", {
        "id": request.sid,
        "position": {"x": ox, "y": oy, "z": oz},
        "direction": {"x": dx, "y": dy, "z": dz},
    })
    if hit:
        socketio.emit("drone_hit", {
            "id": hit["id"], "username": hit["username"],
            "by": shooter_name, "health": hit["health"],
            "message": f"{hit['username']} foi atingido por {shooter_name}. Vida: {hit['health']}%."
        })
        socketio.emit("drone_health", hit)
    if respawn_id:
        socketio.start_background_task(respawn_drone, respawn_id)


def respawn_drone(sid):
    socketio.sleep(3)
    with state_lock:
        pilot = active_pilots.get(sid)
        if not pilot:
            return
        spawn = dict(SPAWN_POINTS[random.randrange(len(SPAWN_POINTS))])
        pilot["position"] = spawn
        pilot["rotation"] = {"pitch": 0.0, "yaw": 0.0, "roll": 0.0}
        pilot["health"] = 100
        pilot["weapon"] = False
        payload = {
            "id": sid, "position": spawn,
            "rotation": pilot["rotation"], "weapon": False, "health": 100
        }
        username = pilot["username"]
    socketio.emit("remote_drone_moved", payload)
    socketio.emit("drone_respawned", {"id": sid, "username": username})


@socketio.on("delivery_action")
def handle_delivery_action(data):
    if not isinstance(data, dict):
        return
    action = data.get("action")
    pad = str(data.get("pad", "terminal"))[:32]
    with state_lock:
        pilot = active_pilots.get(request.sid)
        if not pilot:
            return
        if action == "pickup":
            pilot["has_package"] = True
            message = f"{pilot['username']} coletou uma encomenda em {pad}."
        elif action == "delivered" and pilot["has_package"]:
            pilot["has_package"] = False
            pilot["score"] += 100
            message = f"{pilot['username']} concluiu uma entrega (+100)."
        else:
            return
        payload = {"username": pilot["username"], "score": pilot["score"], "message": message}
    socketio.emit("delivery_event", payload)

with app.app_context():
    db.create_all()

# Em produção (Gunicorn) o módulo é carregado no processo do worker.
# Use 1 worker no Render para evitar rotinas duplicadas.
start_keep_alive_worker()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Iniciando AeroEngine Server na porta {port}...")
    socketio.run(app, host="0.0.0.0", port=port)
