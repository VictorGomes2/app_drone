import os
import base64
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit

# ==========================================
# CONFIGURAÇÃO DO SERVIDOR E BANCO DE DADOS
# ==========================================
app = Flask(__name__)
CORS(app) # Permite que o frontend (HTML) acesse esta API

# O Render injeta a variável DATABASE_URL automaticamente.
# O SQLAlchemy exige 'postgresql://' no lugar de 'postgres://' (padrão antigo)
db_url = os.environ.get('DATABASE_URL', 'sqlite:///local_aeroengine.db')
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# ==========================================
# MODELO DO BANCO DE DADOS (POSTGRESQL)
# ==========================================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    approved = db.Column(db.Boolean, default=False)
    flightTimeSec = db.Column(db.Integer, default=0)
    missions = db.Column(db.JSON, default=lambda: [False]*15)

# ==========================================
# ESTADO DO MULTIPLAYER E 10 SPAWN POINTS
# ==========================================
SPAWN_POINTS = [
    {"x": 25, "y": 1, "z": 30},     # 1. Heliponto Central
    {"x": -100, "y": 1, "z": 100},  # 2. Zona Comercial Norte
    {"x": 200, "y": 1, "z": -150},  # 3. Parque Sul
    {"x": -250, "y": 1, "z": -200}, # 4. Zona Residencial Leste
    {"x": 300, "y": 1, "z": 300},   # 5. Praça Central
    {"x": 0, "y": 1, "z": -400},    # 6. Avenida Principal
    {"x": 400, "y": 1, "z": 0},     # 7. Centro Financeiro
    {"x": -400, "y": 1, "z": 250},  # 8. Periferia Oeste
    {"x": 150, "y": 1, "z": -350},  # 9. Heliponto Secundário
    {"x": -300, "y": 1, "z": -50}   # 10. Zona de Construção
]

active_pilots = {}
spawn_index = 0

# ==========================================
# ROTAS REST (LOGIN E REGISTRO)
# ==========================================
@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json()
    username = data.get('username', '').strip().upper()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({"success": False, "msg": "Preencha todos os campos."}), 400

    existing = User.query.filter_by(username=username).first()
    if existing:
        return jsonify({"success": False, "msg": "Usuário já existe na base."}), 400

    # O primeiro usuário cadastrado vira admin automaticamente
    is_first = User.query.count() == 0
    # Mantemos a lógica base64 para combinar perfeitamente com o btoa() do front-end
    encoded_password = base64.b64encode(password.encode('utf-8')).decode('utf-8')

    new_user = User(
        username=username,
        password=encoded_password,
        approved=is_first or username == 'ADMIN',
        missions=[False]*15
    )
    db.session.add(new_user)
    db.session.commit()

    return jsonify({"success": True, "msg": "Solicitação enviada ao Comando."})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username', '').strip().upper()
    password = data.get('password', '')

    user = User.query.filter_by(username=username).first()
    if not user:
        return jsonify({"success": False, "msg": "Usuário não encontrado."}), 404

    encoded_password = base64.b64encode(password.encode('utf-8')).decode('utf-8')
    if user.password != encoded_password:
        return jsonify({"success": False, "msg": "Credenciais inválidas."}), 401

    if not user.approved:
        return jsonify({"success": False, "msg": "Acesso pendente de liberação."}), 403

    user_data = {
        "username": user.username,
        "approved": user.approved,
        "flightTimeSec": user.flightTimeSec,
        "missions": user.missions
    }
    return jsonify({"success": True, "userData": user_data})

# ==========================================
# WEBSOCKETS (CONTROLE DE TRÁFEGO AÉREO)
# ==========================================
@socketio.on('connect')
def handle_connect():
    print(f"[+] Nova conexão de rádio: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    print(f"[-] Conexão perdida: {request.sid}")
    if request.sid in active_pilots:
        del active_pilots[request.sid]
        # Avisa aos outros drones que este desconectou
        emit('drone_disconnected', request.sid, broadcast=True)

@socketio.on('join_airspace')
def handle_join(data):
    global spawn_index
    # Seleciona um dos 10 pontos de nascimento para evitar colisão
    spawn = SPAWN_POINTS[spawn_index % len(SPAWN_POINTS)]
    spawn_index += 1

    # Registra o piloto no espaço aéreo
    active_pilots[request.sid] = {
        "id": request.sid,
        "username": data.get("username", "Piloto"),
        "droneType": data.get("droneType", "mini3"),
        "position": spawn,
        "rotation": {"pitch": 0, "yaw": 0, "roll": 0}
    }

    # Autoriza o voo e envia as coordenadas iniciais
    emit('flight_clearance', {"spawnPoint": spawn})
    
    # Atualiza o radar de TODOS os pilotos com a nova frota
    emit('airspace_update', active_pilots, broadcast=True)

@socketio.on('telemetry_update')
def handle_telemetry(telemetry):
    if request.sid in active_pilots:
        # Atualiza o estado no servidor
        active_pilots[request.sid]["position"] = telemetry.get("position")
        active_pilots[request.sid]["rotation"] = telemetry.get("rotation")

        # Transmite (broadcast) a posição deste drone para TODOS OS OUTROS
        emit('remote_drone_moved', {
            "id": request.sid,
            "position": telemetry.get("position"),
            "rotation": telemetry.get("rotation")
        }, broadcast=True, include_self=False)

# ==========================================
# INICIALIZAÇÃO
# ==========================================
with app.app_context():
    db.create_all() # Cria as tabelas no PostgreSQL se não existirem

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Iniciando AeroEngine Server na porta {port}...")
    socketio.run(app, host='0.0.0.0', port=port)