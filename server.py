import collections
import collections.abc
# --- FIX dla dnspython 1.16.0 na Python 3.10+ ---
if not hasattr(collections, 'MutableMapping'):
    collections.MutableMapping = collections.abc.MutableMapping

import eventlet
eventlet.monkey_patch()

from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import os
import time
from pymongo import MongoClient

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sekret_robotow'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet', ping_timeout=25, ping_interval=10)

# --- NOWA KONFIGURACJA BAZY (Lazy Loading) ---
MONGO_URI = "mongodb+srv://admin:bojar55555@cluster0.kugbsd0.mongodb.net/?appName=Cluster0"
db_client = None
rooms_collection = None

def get_db():
    global db_client, rooms_collection
    if db_client is None:
        db_client = MongoClient(MONGO_URI)
        db = db_client.robotic_game
        rooms_collection = db.rooms
        print("--- [WORKER] POŁĄCZONO Z BAZĄ MONGODB ---")
    return rooms_collection

# Konfiguracja czyszczenia
INACTIVE_TIMEOUT = 172800  # 48h
EMPTY_ROOM_TIMEOUT = 900   # 15 min

@app.route('/')
def index():
    return "SERVER ROBOTIC EMPIRE DZIAŁA! (MongoDB Connected)"

def cleanup_loop():
    while True:
        eventlet.sleep(60)
        rooms_col = get_db()
        if rooms_col is None: continue 
        
        now = time.time()
        
        # Usuń stare pokoje waiting
        rooms_col.delete_many({
            "status": "waiting",
            "last_active": {"$lt": now - EMPTY_ROOM_TIMEOUT},
            "player_count": {"$lt": 2}
        })
        
        # Usuń bardzo stare pokoje
        rooms_col.delete_many({
            "last_active": {"$lt": now - INACTIVE_TIMEOUT}
        })
        
        socketio.emit('rooms_list_update', get_public_rooms_list())

socketio.start_background_task(cleanup_loop)

@socketio.on('connect')
def on_connect(auth=None):
    emit('rooms_list_update', get_public_rooms_list())

@socketio.on('create_room')
def on_create(data):
    rooms_col = get_db()
    
    room = data.get('room', '').strip()
    raw_user = data.get('username', '').strip()
    user_key = raw_user.lower()
    
    pwd = data.get('password', '')
    g_type = data.get('goal_type', 'money')
    g_val = data.get('goal_value', 1000000)

    # --- WALIDACJA ---
    if not room:
        emit('error_log', {'msg': "Nazwa pokoju nie może być pusta!"})
        return
    if not raw_user:
        emit('error_log', {'msg': "Nick nie może być pusty!"})
        return
    
    if user_key == "gracz":
        emit('error_log', {'msg': "Niedozwolony nick! Zmień 'Gracz' na coś innego."})
        return

    # Sprawdzenie czy pokój już istnieje
    if rooms_col.find_one({"_id": room}):
        emit('error_log', {'msg': f"Pokój '{room}' już istnieje! Użyj opcji DOŁĄCZ."})
        return

    join_room(room)
    
    room_doc = {
        "_id": room,
        "password": pwd,
        "goal_type": g_type,
        "goal_value": g_val,
        "players": {
            user_key: { 
                'money': 0.0, 
                'mps': 0.0,
                'display_name': raw_user
            }
        },
        "player_count": 1,
        "status": "waiting",
        "last_active": time.time()
    }
    
    rooms_col.insert_one(room_doc)
    
    emit('join_success', {
        'room': room, 
        'goal_desc': f"{g_val} {g_type}",
        'is_new': True,
        'status': 'waiting',
        'saved_money': 0,
        'saved_mps': 0
    })
    socketio.emit('rooms_list_update', get_public_rooms_list())

@socketio.on('join_room_request')
def on_join_req(data):
    rooms_col = get_db()
    
    room = data.get('room')
    raw_user = data.get('username', '').strip()
    user_key = raw_user.lower()
    pwd_attempt = data.get('password', '')

    # --- 1. WALIDACJA WEJŚCIOWA ---
    if not raw_user:
        emit('error_log', {'msg': "Nick nie może być pusty!"})
        return
    if user_key == "gracz":
        emit('error_log', {'msg': "Zmień nick! 'Gracz' jest zablokowany."})
        return

    r_data = rooms_col.find_one({"_id": room})
    if not r_data:
        emit('error_log', {'msg': "Pokój nie istnieje!"})
        return

    players = r_data.get('players', {})

    # --- 2. CLEANER (Sprzątacz) ---
    if len(players) > 2:
        print(f"--- [CLEANER] Naprawiam tłok w pokoju {room} ---")
        valid_players = {}
        if user_key in players:
            valid_players[user_key] = players[user_key]
        for k, v in players.items():
            if len(valid_players) < 2 and k != user_key:
                valid_players[k] = v
        rooms_col.update_one({"_id": room}, {"$set": {"players": valid_players}})
        players = valid_players

    is_already_registered = user_key in players
    if not is_already_registered and len(players) >= 2:
        emit('error_log', {'msg': "POKÓJ PEŁNY!"})
        return

    if r_data.get('password') and r_data['password'] != pwd_attempt:
        emit('error_log', {'msg': "BŁĘDNE HASŁO!"})
        return

    join_room(room)

    # --- 3. AKTUALIZACJA BAZY ---
    if not is_already_registered:
        rooms_col.update_one(
            {"_id": room},
            {
                "$set": {
                    f"players.{user_key}": {
                        'money': 0.0, 
                        'mps': 0.0, 
                        'display_name': raw_user
                    },
                    "last_active": time.time()
                }
            }
        )
        rooms_col.update_one({"_id": room}, {"$set": {"player_count": len(players) + 1}})
    else:
        rooms_col.update_one(
            {"_id": room}, 
            {
                "$set": {
                    "last_active": time.time(),
                    f"players.{user_key}.display_name": raw_user
                }
            }
        )

    # --- 4. START I DANE ---
    r_data_fresh = rooms_col.find_one({"_id": room})
    fresh_players = r_data_fresh.get('players', {})
    my_stats = fresh_players.get(user_key, {'money': 0, 'mps': 0})
    
    current_status = r_data_fresh.get('status', 'waiting')
    
    if len(fresh_players) >= 2:
        if current_status == 'waiting':
            rooms_col.update_one({"_id": room}, {"$set": {"status": "playing"}})
            current_status = "playing"
        
        socketio.emit('game_start_signal', {'msg': 'START'}, to=room)

    emit('join_success', {
        'room': room,
        'goal_desc': f"{r_data.get('goal_value')} {r_data.get('goal_type')}",
        'is_new': not is_already_registered,
        'status': current_status,
        'saved_money': my_stats.get('money', 0),
        'saved_mps': my_stats.get('mps', 0)
    })

    for p_id, p_stats in fresh_players.items():
        if p_id != user_key:
            d_name = p_stats.get('display_name', p_id)
            emit('opponent_progress', {
                'username': d_name,
                'money': p_stats.get('money', 0),
                'mps': p_stats.get('mps', 0)
            })

@socketio.on('update_progress')
def on_update(data):
    rooms_col = get_db()
    if rooms_col is None: return

    room = data.get('room')
    user = data.get('username', '').strip().lower()
    money = data.get('money', 0)
    mps = data.get('mps', 0)

    # Optymalizacja: Szybka wysyłka do rywala
    emit('opponent_progress', data, to=room, include_self=False)
    
    # Zapis w bazie (rzadziej lub asynchronicznie w produkcji, tu bezpośrednio)
    # Sprawdzamy czy wygrał
    r_data = rooms_col.find_one({"_id": room}, {"status": 1, "goal_type": 1, "goal_value": 1})
    if not r_data: return
    if r_data['status'] == 'finished': return

    rooms_col.update_one(
        {"_id": room},
        {"$set": {
            f"players.{user}.money": money,
            f"players.{user}.mps": mps,
            "last_active": time.time()
        }}
    )
    
    goal_val = r_data.get('goal_value', 1000000)
    if goal_val != -1:
        has_won = False
        goal_type = r_data.get('goal_type', 'money')
        
        if goal_type == 'money' and money >= goal_val: has_won = True
        elif goal_type == 'mps' and mps >= goal_val: has_won = True
        
        if has_won:
            rooms_col.update_one({"_id": room}, {"$set": {"status": "finished"}})
            emit('game_over', {'winner': data.get('username')}, to=room)

@socketio.on('leave_game')
def on_leave(data):
    rooms_col = get_db()
    if rooms_col is None: return

    room = data.get('room')
    user = data.get('username')
    
    emit('player_left', {'username': user}, to=room)
    leave_room(room)
    
    rooms_col.update_one({"_id": room}, {"$set": {"last_active": time.time()}})
    
    print(f"--- [LEAVE] Gracz {user} wyszedł, slot zajęty ---")
    socketio.emit('rooms_list_update', get_public_rooms_list())
    
@socketio.on('request_rooms_list')
def on_list_req():
    emit('rooms_list_update', get_public_rooms_list())

def get_public_rooms_list():
    rooms_col = get_db()
    if rooms_col is None: return []
    
    cursor = rooms_col.find({
        "$or": [
            {"status": "waiting"},
            {"player_count": {"$lt": 2}}
        ]
    })
    
    public_list = []
    for r_data in cursor:
        g_val = r_data.get('goal_value', 0)
        if g_val == -1:
            goal_str = "BEZ LIMITU"
        else:
            suffix = " PLN" if r_data.get('goal_type') == 'money' else "/s"
            goal_str = f"{g_val}{suffix}"
            
        public_list.append({
            'name': r_data['_id'],
            'goal': goal_str,
            'players': r_data.get('player_count', 0),
            'locked': bool(r_data.get('password'))
        })
    return public_list

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
