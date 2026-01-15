import collections
import collections.abc
# --- FIX dla dnspython 1.16.0 na Python 3.10+ ---
# Przywracamy funkcję, która została usunięta w nowym Pythonie
if not hasattr(collections, 'MutableMapping'):
    collections.MutableMapping = collections.abc.MutableMapping

import eventlet
eventlet.monkey_patch()

from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import os
import time
from pymongo import MongoClient # Importujemy klienta bazy

# ... (importy i MutableMapping fix bez zmian) ...

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
        # Połączenie nawiązywane jest dopiero wewnątrz workera
        db_client = MongoClient(MONGO_URI)
        db = db_client.robotic_game
        rooms_collection = db.rooms
        print("--- [WORKER] POŁĄCZONO Z BAZĄ MONGODB ---")
    return rooms_collection

# W cleanup_loop i innych miejscach używaj get_db() zamiast rooms_collection

# --------------------------------

# Konfiguracja czyszczenia
INACTIVE_TIMEOUT = 172800  # 48h
EMPTY_ROOM_TIMEOUT = 900   # 15 min

@app.route('/')
def index():
    return "SERVER ROBOTIC EMPIRE DZIAŁA! (MongoDB Connected)"

def cleanup_loop():
    """Wątek sprzątający martwe pokoje z BAZY"""
    while True:
        eventlet.sleep(60)
        # STARE: if not rooms_collection: continue
        # NOWE:
        if rooms_collection is None: continue 
        
        now = time.time()
        
        # Usuń stare pokoje z bazy
        # Kryterium 1: waiting i puste > 15 min
        rooms_collection.delete_many({
            "status": "waiting",
            "last_active": {"$lt": now - EMPTY_ROOM_TIMEOUT},
            "player_count": {"$lt": 2} # Musimy to śledzić
        })
        
        # Kryterium 2: nieaktywne > 48h
        rooms_collection.delete_many({
            "last_active": {"$lt": now - INACTIVE_TIMEOUT}
        })
        
        # Odśwież listę dla graczy
        socketio.emit('rooms_list_update', get_public_rooms_list())

socketio.start_background_task(cleanup_loop)

@socketio.on('connect')
# STARE: def on_connect():
# NOWE (dodaj argument):
def on_connect(auth=None):
    emit('rooms_list_update', get_public_rooms_list())

@socketio.on('create_room')
def on_create(data):
    rooms_col = get_db()
    
    room = data.get('room', '').strip()
    raw_user = data.get('username', '').strip()
    user_key = raw_user.lower()
    
    # --- FIREWALL: Blokada domyślnego nicku ---
    if user_key == "gracz":
        emit('error_log', {'msg': "Niedozwolony nick! Zmień 'Gracz' na coś innego."})
        return
    # ------------------------------------------

    if not room or not raw_user:
        emit('error_log', {'msg': "Dane nie mogą być puste!"})
        return

    # --- FIREWALL: Blokada nadpisywania ---
    # Jeśli pokój istnieje, NIE WOLNO nic w nim zmieniać przez create_room
    if rooms_col.find_one({"_id": room}):
        emit('error_log', {'msg': f"Pokój '{room}' już istnieje! Użyj opcji DOŁĄCZ."})
        return
    # --------------------------------------

    join_room(room)
    
    # Tworzymy NOWY, CZYSTY pokój
    room_doc = {
        "_id": room,
        "password": data.get('password', ''),
        "goal_type": data.get('goal_type', 'money'),
        "goal_value": data.get('goal_value', 1000000),
        "players": {
            user_key: { 
                'money': 0.0, 
                'mps': 0.0,
                'display_name': raw_user # Tu musi być display_name!
            }
        },
        "player_count": 1,
        "status": "waiting",
        "last_active": time.time()
    }
    
    rooms_col.insert_one(room_doc)
    
    emit('join_success', {
        'room': room, 
        'goal_desc': f"{data.get('goal_value')} {data.get('goal_type')}",
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

    # --- 1. FIREWALL NA NAZWĘ "GRACZ" ---
    if user_key == "gracz" or user_key == "":
        emit('error_log', {'msg': "Niedozwolony nick! Wpisz swoją nazwę."})
        return
    # ------------------------------------

    r_data = rooms_col.find_one({"_id": room})
    if not r_data:
        emit('error_log', {'msg': "Pokój nie istnieje!"})
        return

    players = r_data.get('players', {})

    # --- 2. ODKURZACZ (Auto-naprawa bazy) ---
    # Jeśli wchodzisz do pokoju i widzisz tam 3 osoby (bo wcześniej był błąd),
    # ten kod usunie "Gracza" i zostawi tylko Ciebie i rywala.
    if len(players) > 2:
        print(f"--- [CLEANER] Usuwam nadmiarowych graczy z {room} ---")
        keys_to_keep = []
        
        # Zawsze zachowaj "siebie" (jeśli już tam byłem)
        if user_key in players:
            keys_to_keep.append(user_key)
            
        # Dobierz resztę, ale NIGDY nie bierz "gracz"
        for k in players.keys():
            if len(keys_to_keep) < 2 and k != user_key and k != "gracz":
                keys_to_keep.append(k)
        
        # Nadpisz bazę wersją naprawioną
        new_players = {k: players[k] for k in keys_to_keep}
        rooms_col.update_one({"_id": room}, {"$set": {"players": new_players}})
        # Aktualizuj lokalną zmienną, żeby dalsza część kodu widziała 2 osoby
        players = new_players 
    # ----------------------------------------

    is_already_registered = user_key in players

    # Jeśli po czyszczeniu nadal jest 2 obcych ludzi -> Blokada
    if not is_already_registered and len(players) >= 2:
        emit('error_log', {'msg': "POKÓJ PEŁNY!"})
        return

    if r_data.get('password') and r_data['password'] != pwd_attempt:
        emit('error_log', {'msg': "BŁĘDNE HASŁO!"})
        return

    join_room(room)

    # Aktualizacja (Bezpieczna)
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
        # Update dla powracającego (dla pewności nadpisujemy display_name)
        rooms_col.update_one(
            {"_id": room}, 
            {
                "$set": {
                    "last_active": time.time(),
                    f"players.{user_key}.display_name": raw_user
                }
            }
        )

    # Pobieranie świeżych danych
    r_data_fresh = rooms_col.find_one({"_id": room})
    fresh_players = r_data_fresh.get('players', {})
    my_stats = fresh_players.get(user_key, {'money': 0, 'mps': 0})
    
    current_status = r_data_fresh.get('status', 'waiting')
    
    # --- START GRY ---
    if len(fresh_players) >= 2:
        if current_status == 'waiting':
            rooms_col.update_one({"_id": room}, {"$set": {"status": "playing"}})
            current_status = "playing"
        
        # Wyślij sygnał, że gramy (odblokuj okna)
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
            emit('opponent_progress', {
                'username': p_stats.get('display_name', p_id),
                'money': p_stats.get('money', 0),
                'mps': p_stats.get('mps', 0)
            })
            
@socketio.on('update_progress')
def on_update(data):
    room = data.get('room')
    user = data.get('username').strip().lower()
    money = data.get('money', 0)
    mps = data.get('mps', 0)

    if rooms_collection is None: return

    # Optymalizacja: Nie pobieramy całego dokumentu przy każdym update (za wolno)
    # Po prostu wysyłamy update do bazy i do rywala
    
    # 1. Wyślij do rywala (Szybko, przez RAM)
    emit('opponent_progress', data, to=room, include_self=False)
    
    # 2. Zapisz w bazie (Async w tle? Nie, tu zrobimy prosto)
    # Żeby nie zatykać bazy, można by to robić rzadziej, ale na start OK.
    
    # Sprawdzamy wygraną TYLKO jeśli trzeba (pobranie celu)
    # Żeby nie czytać bazy co ułamek sekundy, zróbmy tak:
    # Czytamy cel tylko raz na jakiś czas?
    # Albo: Zakładamy, że klient wie co robi.
    # Ale dla bezpieczeństwa sprawdźmy w bazie.
    
    r_data = rooms_collection.find_one({"_id": room}, {"status": 1, "goal_type": 1, "goal_value": 1})
    
    if not r_data: return
    if r_data['status'] == 'finished': return

    # Zapisz postęp
    rooms_collection.update_one(
        {"_id": room},
        {"$set": {
            f"players.{user}.money": money,
            f"players.{user}.mps": mps,
            "last_active": time.time()
        }}
    )
    
    # Warunek zwycięstwa
    goal_val = r_data.get('goal_value', 1000000)
    if goal_val != -1:
        has_won = False
        goal_type = r_data.get('goal_type', 'money')
        
        if goal_type == 'money' and money >= goal_val: has_won = True
        elif goal_type == 'mps' and mps >= goal_val: has_won = True
        
        if has_won:
            rooms_collection.update_one({"_id": room}, {"$set": {"status": "finished"}})
            emit('game_over', {'winner': user}, to=room)

@socketio.on('leave_game')
def on_leave(data):
    room = data.get('room')
    user = data.get('username')
    
    if rooms_collection is None: return

    # --- POPRAWKA 3: PERSYSTENCJA (NIE KASUJEMY) ---
    # Informujemy rywala, że wyszliśmy, ale dane zostają w MongoDB
    emit('player_left', {'username': user}, to=room)
    leave_room(room)
    
    # Aktualizujemy tylko czas, żeby cleanup_loop wiedział, że ktoś tu jeszcze "żyje"
    rooms_collection.update_one({"_id": room}, {"$set": {"last_active": time.time()}})
    
    print(f"--- [LEAVE] Gracz {user} wyszedł, ale slot w {room} pozostaje zarezerwowany ---")
    socketio.emit('rooms_list_update', get_public_rooms_list())
    
@socketio.on('request_rooms_list')
def on_list_req():
    emit('rooms_list_update', get_public_rooms_list())

def get_public_rooms_list():
    if rooms_collection is None: return []
    
    # Pobierz tylko pokoje "waiting" lub gdzie jest < 2 graczy
    # Pobieramy z bazy
    cursor = rooms_collection.find({
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
