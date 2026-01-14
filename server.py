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

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sekret_robotow'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- KONFIGURACJA BAZY DANYCH ---
# Wklej tu swój link, pamiętaj o haśle!
# Najlepiej trzymać to w zmiennych środowiskowych na Renderze, ale do testów wpisz tu:
MONGO_URI = "mongodb+srv://admin:bojar55555@cluster0.kugbsd0.mongodb.net/?appName=Cluster0"

try:
    client = MongoClient(MONGO_URI)
    db = client.robotic_game  # Nazwa bazy
    rooms_collection = db.rooms # Nazwa kolekcji (tabeli)
    print("--- POŁĄCZONO Z BAZĄ MONGODB ---")
except Exception as e:
    print(f"--- BŁĄD BAZY DANYCH: {e} ---")
    rooms_collection = None

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
    room = data['room']
    user = data['username']
    pwd = data.get('password', '')
    g_type = data.get('goal_type', 'money')
    g_val = data.get('goal_value', 1000000)
    
    if rooms_collection is None: return

    # Sprawdź w bazie czy pokój istnieje
    if rooms_collection.find_one({"_id": room}):
        emit('error_log', {'msg': f"Pokój {room} już istnieje!"})
        return

    join_room(room)
    
    # Tworzymy dokument pokoju
    room_doc = {
        "_id": room, # ID pokoju to jego nazwa
        "password": pwd,
        "goal_type": g_type,
        "goal_value": g_val,
        "players": {
            user: {'money': 0, 'mps': 0}
        },
        "player_count": 1,
        "status": "waiting",
        "last_active": time.time()
    }
    
    rooms_collection.insert_one(room_doc)
    
    emit('join_success', {
        'room': room, 
        'goal_desc': f"{g_val} {g_type}",
        'is_new': True,
        'status': 'waiting'
    })
    socketio.emit('rooms_list_update', get_public_rooms_list())

@socketio.on('join_room_request')
def on_join_req(data):
    room = data['room']
    # .strip() usuwa przypadkowe spacje, które psują porównywanie nicków
    user = data['username'].strip() 
    pwd_attempt = data.get('password', '')
    
    if rooms_collection is None: return
    
    # Pobierz pokój z bazy
    r_data = rooms_collection.find_one({"_id": room})
    
    if not r_data:
        emit('error_log', {'msg': "Pokój nie istnieje lub wygasł!"})
        return
    
    if r_data['password'] and r_data['password'] != pwd_attempt:
        emit('error_log', {'msg': "BŁĘDNE HASŁO!"})
        return

    players = r_data.get('players', {})
    
    # Sprawdzenie pełnego pokoju (tylko jeśli gracza nie ma jeszcze na liście)
    if len(players) >= 2 and user not in players:
         emit('error_log', {'msg': "Pokój jest pełny!"})
         return

    join_room(room)
    
    # --- FIX 1: INTELIGENTNA AKTUALIZACJA (BEZ RESETU) ---
    update_ops = {
        "$set": {"last_active": time.time()}
    }
    
    is_returning = False
    if user not in players:
        # Nowy gracz: Dopiero teraz inicjalizujemy go zerami
        update_ops["$set"][f"players.{user}"] = {'money': 0, 'mps': 0}
        # Zwiększamy licznik tylko przy nowym graczu
        update_ops["$set"]["player_count"] = len(players) + 1
    else:
        # Gracz powracający: NIE RUSZAMY JEGO PIENIĘDZY W BAZIE!
        is_returning = True
    
    # Wykonaj update w bazie
    rooms_collection.update_one({"_id": room}, update_ops)
    
    # --- FIX 2: LOGIKA STARTU I WYSYŁANIA DANYCH ---
    current_status = r_data['status']
    
    # Jeśli to drugi gracz (nowy) i gra czekała -> START
    if not is_returning and len(players) == 1 and current_status == 'waiting':
        rooms_collection.update_one({"_id": room}, {"$set": {"status": "playing"}})
        current_status = "playing"
        socketio.emit('game_start_signal', {'start_time': 0}, to=room)

    # Wyślij status do gracza
    emit('join_success', {
        'room': room,
        'goal_desc': f"{r_data['goal_value']} {r_data['goal_type']}",
        'is_new': not is_returning,
        'status': current_status
    })
    
    # --- FIX 3: POBIERANIE DANYCH RYWALA ---
    # Pobieramy świeże dane z bazy (już po update)
    r_data_fresh = rooms_collection.find_one({"_id": room})
    fresh_players = r_data_fresh.get('players', {})
    
    found_opponent = False
    for p_name, p_stats in fresh_players.items():
        # Porównujemy nicki po wyczyszczeniu spacji
        if p_name.strip() != user:
            found_opponent = True
            emit('opponent_progress', {
                'username': p_name,
                'money': p_stats.get('money', 0),
                'mps': p_stats.get('mps', 0)
            })

    # Jeśli nie znaleziono rywala w bazie (bo np. został usunięty starym kodem),
    # wysyłamy pusty status, żeby wyczyścić ewentualne śmieci w UI
    if not found_opponent:
         emit('opponent_progress', {
            'username': 'Oczekiwanie...',
            'money': 0,
            'mps': 0
        })

    socketio.emit('rooms_list_update', get_public_rooms_list())

@socketio.on('update_progress')
def on_update(data):
    room = data.get('room')
    user = data.get('username')
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

    emit('player_left', {'username': user}, to=room)
    leave_room(room)
    
    # --- POPRAWKA: NIE KASUJEMY DANYCH ($unset usunięte) ---
    # Jedynie aktualizujemy czas aktywności, żeby room nie wygasł od razu.
    # Dzięki temu dane zostają w bazie (persistent save).
    
    rooms_collection.update_one(
        {"_id": room},
        {
            "$set": {"last_active": time.time()},
            # Opcjonalnie: Możemy zmniejszyć licznik aktywnych graczy,
            # ale dla logiki "save'a" ważniejsza jest lista 'players'.
            # Jeśli Twoja logika opiera się na 'len(players)', to nie ruszamy tego.
        }
    )
    
    # UWAGA: Usuwamy fragment sprawdzania czy pokój jest pusty i kasowania go natychmiast.
    # Niech cleanup_loop (pętla czyszcząca) zajmie się usuwaniem starych pokoi po 48h.
    # Dzięki temu, jak obaj wyjdziecie, pokój nadal istnieje w bazie.
        
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
