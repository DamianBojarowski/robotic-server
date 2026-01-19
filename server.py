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

from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sekret_robotow'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet', ping_timeout=25, ping_interval=10)

# --- GLOBALNA KSIĘGA GOŚCI ---
											  
															 
active_sockets = {} 

# --- BAZA DANYCH ---
MONGO_URI = os.environ.get("MONGO_URI")
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

def player_rejoin(room: str, user_key: str, display_name: str) -> bool:
    """Oznacza gracza jako online."""
    col = get_db()
    res = col.update_one(
        {"_id": room, f"players.{user_key}": {"$exists": True}},
        {
            "$set": {
                f"players.{user_key}.online": True,
                f"players.{user_key}.display_name": display_name,
                "last_active": time.time()
            },
            "$inc": {"player_count": 1}
        }
    )
    return res.modified_count > 0


def player_leave(room: str, user_key: str):
    """Oznacza gracza jako offline."""
    col = get_db()
    col.update_one(
        {"_id": room, f"players.{user_key}.online": True},
        {
            "$set": {
                f"players.{user_key}.online": False,
                "last_active": time.time()
            },
            "$inc": {"player_count": -1}
        }
    )


@app.route('/')
def index():
    return "SERVER ROBOTIC EMPIRE DZIAŁA! (Fix: Logic & KeyError)"

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

																
@socketio.on('disconnect')
def on_disconnect():
    if request.sid not in active_sockets:
        return
    room = active_sockets[request.sid]['room']
    user = active_sockets[request.sid]['user']
    user_key = user.lower()

    player_leave(room, user_key)
    emit('player_left', {'username': user}, to=room)
    del active_sockets[request.sid]
    socketio.emit('rooms_list_update', get_public_rooms_list())

# --- FUNKCJA POMOCNICZA DO PRZEŁĄCZANIA POKOI ---
def handle_implicit_leave(sid):
    """Jeśli socket jest już w innym pokoju, wyloguj go stamtąd."""
    if sid in active_sockets:
        old_room = active_sockets[sid]['room']
        old_user = active_sockets[sid]['user']
        leave_room(old_room)
        player_leave(old_room, old_user.lower())
        emit('player_left', {'username': old_user}, to=old_room)
        # Nie usuwamy z active_sockets tutaj, bo zaraz zostanie nadpisane

@socketio.on('create_room')
def on_create(data):
    handle_implicit_leave(request.sid) # <--- FIX: Wyloguj ze starego pokoju
    
    rooms_col = get_db()
	
    room = data.get('room', '').strip()
    raw_user = data.get('username', '').strip()
    user_key = raw_user.lower()
    
    pwd = data.get('password', '')
    g_type = data.get('goal_type', 'money')
    g_val = data.get('goal_value', 1000000)

				
														 
			  
    if not room or not raw_user:
        emit('error_log', {'msg': "Dane niekompletne!"})
        return
    if user_key == "gracz":
        emit('error_log', {'msg': "Nick 'Gracz' zabroniony."})
        return

    if rooms_col.find_one({"_id": room}):
        emit('error_log', {'msg': f"Pokój '{room}' już istnieje!"})
        return

    join_room(room)
	
									   
    active_sockets[request.sid] = {'room': room, 'user': raw_user}
    
    room_doc = {
        "_id": room,
        "password": pwd,
        "goal_type": g_type,
        "goal_value": g_val,
        "players": {
            user_key: { 
                'money': 0.0, 'mps': 0.0, 'display_name': raw_user, 'online': True
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
    handle_implicit_leave(request.sid) # <--- FIX: Wyloguj ze starego pokoju

    col = get_db()

    room      = data.get('room')
    raw_user  = data.get('username', '').strip()
    user_key  = raw_user.lower()
    pwd       = data.get('password', '')

    if not raw_user or user_key == "gracz":
        emit('error_log', {'msg': "Nick niepoprawny!"})
        return

    r_data = col.find_one({"_id": room})
    if not r_data:
        emit('error_log', {'msg': "Brak pokoju"})
        return
    if r_data.get('password') and r_data['password'] != pwd:
        emit('error_log', {'msg': "Złe hasło"})
        return

    players = r_data.get('players', {})
    my_doc  = players.get(user_key)

    # 1) REJOIN
    if my_doc:
        if my_doc.get('online', False):
            # Tu wchodzi FIX z handle_implicit_leave:
            # Ponieważ wywołaliśmy to na górze, stary status online powinien zniknąć,
            # chyba że to ten sam pokój i ten sam nick.
            emit('error_log', {'msg': "Gracz o tym nicku już tu jest!"})
            return
					
        ok = player_rejoin(room, user_key, raw_user)
        if not ok:
            emit('error_log', {'msg': "Błąd bazy danych (rejoin)"})
            return
    else:
        # 2) NEW PLAYER
																					   
        if r_data.get('status') != 'waiting':
             emit('error_log', {'msg': "Gra w toku - za późno!"})
             return

        if r_data.get('player_count', 0) >= 2:
            emit('error_log', {'msg': "Pokój pełny"})
            return
        
									  
																   
        col.update_one(
            {"_id": room}, 
            {
                "$set": {
                    f"players.{user_key}": {
                        'money': 0, 'mps': 0,
                        'display_name': raw_user,
                        'online': True
                    },
                    "last_active": time.time()
                },
                "$inc": {"player_count": 1}
            }
        )

    # Dołączenie do pokoju w RAM
    join_room(room)
    active_sockets[request.sid] = {'room': room, 'user': raw_user}

    # Pobieramy świeże dane
    fresh = col.find_one({"_id": room})
    
    # Logika startu gry
									
    current_status = fresh.get('status', 'waiting')
	
																					  
    if fresh.get('player_count', 0) >= 2 and current_status == 'waiting':
        current_status = 'playing'
        col.update_one({"_id": room}, {"$set": {"status": "playing"}})
																			
        socketio.emit('game_start_signal', {'msg': 'START'}, to=room)

    # --- FIX KEYERROR: Bezpieczne pobieranie danych ---
    # Używamy .get() wszędzie, aby nie wywalić serwera, gdy baza ma laga
    p_data = fresh.get('players', {}).get(user_key, {})
    saved_money = p_data.get('money', 0)
    saved_mps   = p_data.get('mps', 0)

															  
													
																			  
    emit('join_success', {
        'room': room,
        'goal_desc': f"{r_data.get('goal_value')} {r_data.get('goal_type')}",
        'is_new': my_doc is None,
        'status': current_status, 
        'saved_money': saved_money,
        'saved_mps': saved_mps
    })

    # Update listy rywali
    players_list = fresh.get('players', {})
    for k, v in players_list.items():
        if k != user_key:  # <--- USUNIĘTO WARUNEK "and v.get('online')"
            emit('opponent_progress', {
                'username': v.get('display_name', 'Nieznany'),
                'money': v.get('money', 0),
                'mps': v.get('mps', 0),
                'is_online': v.get('online', False) # <--- Dodajemy informację o statusie
            })

@socketio.on('update_progress')
def on_update(data):
    rooms_col = get_db()
    if rooms_col is None: return

    room = data.get('room')
    user = data.get('username', '').strip().lower()
    money = data.get('money', 0)
    mps = data.get('mps', 0)

						 
    emit('opponent_progress', data, to=room, include_self=False)
    
				
    r_data = rooms_col.find_one({"_id": room}, {"status": 1, "goal_type": 1, "goal_value": 1})
    if not r_data: return
    if r_data.get('status') == 'finished': return

    rooms_col.update_one(
        {"_id": room},
        {"$set": {
            f"players.{user}.money": money,
            f"players.{user}.mps": mps,
            "last_active": time.time()
        }}
    )
    
    # Win condition
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
    room = data.get('room')
    user = data.get('username')
    user_key = user.lower()

    if request.sid in active_sockets:
        del active_sockets[request.sid]
    leave_room(room)

    player_leave(room, user_key)
    emit('player_left', {'username': user}, to=room)
    socketio.emit('rooms_list_update', get_public_rooms_list())
    
@socketio.on('request_rooms_list')
def on_list_req():
    emit('rooms_list_update', get_public_rooms_list())

def get_public_rooms_list():
    col = get_db()
																			
    cursor = col.find({
        "status": "waiting", 
        "player_count": {"$lt": 2}
    })
    out = []
    for r in cursor:
        g_val = r.get('goal_value', 0)
        suffix = " PLN" if r.get('goal_type') == 'money' else "/s"
        goal_str = "BEZ LIMITU" if g_val == -1 else f"{g_val}{suffix}"
        out.append({
            'name': r['_id'],
            'goal': goal_str,
            'players': r.get('player_count', 0),
            'locked': bool(r.get('password'))
        })
    return out

@socketio.on('register_account')
def on_register(data):
    user = data.get('username', '').strip()
    pwd = data.get('password', '').strip()
    
    if not user or not pwd:
        emit('auth_result', {'success': False, 'msg': "Puste dane!"})
        return
        
    db = get_db().database # Pobieramy obiekt bazy z kolekcji rooms
    users_col = db.users   # Nowa kolekcja 'users'
    
    if users_col.find_one({"_id": user.lower()}):
        emit('auth_result', {'success': False, 'msg': "Nick zajęty!"})
        return
        
    # Tworzymy usera
    users_col.insert_one({
        "_id": user.lower(),
        "display_name": user,
        "password": generate_password_hash(pwd),
        "save_data": None,
        "save_date": None
    })
    
    emit('auth_result', {'success': True, 'msg': "Konto założone! Zaloguj się.", 'action': 'register'})

@socketio.on('login_account')
def on_login(data):
    user = data.get('username', '').strip()
    pwd = data.get('password', '').strip()
    
    db = get_db().database
    users_col = db.users
    
    doc = users_col.find_one({"_id": user.lower()})
    
    if not doc or not check_password_hash(doc['password'], pwd):
        emit('auth_result', {'success': False, 'msg': "Błędny login lub hasło!"})
        return
        
    # Pobieramy datę ostatniego zapisu (jeśli istnieje)
    save_date = doc.get('save_date', 0)
    
    emit('auth_result', {
        'success': True, 
        'msg': f"Witaj, {doc['display_name']}!", 
        'username': doc['display_name'],
        'save_date': save_date,
        'action': 'login'
    })

@socketio.on('upload_cloud_save')
def on_upload_save(data):
    user = data.get('username', '').strip()
    pwd = data.get('password', '').strip() # Weryfikacja przy każdym zapisie dla bezpieczeństwa
    save_json = data.get('save_data', {})
    
    db = get_db().database
    users_col = db.users
    
    doc = users_col.find_one({"_id": user.lower()})
    
    if not doc or not check_password_hash(doc['password'], pwd):
        emit('cloud_action_result', {'success': False, 'msg': "Błąd autoryzacji!"})
        return
        
    now = time.time()
    users_col.update_one(
        {"_id": user.lower()},
        {"$set": {"save_data": save_json, "save_date": now}}
    )
    
    emit('cloud_action_result', {'success': True, 'msg': "Zapisano w chmurze!", 'timestamp': now})

@socketio.on('download_cloud_save')
def on_download_save(data):
    user = data.get('username', '').strip()
    pwd = data.get('password', '').strip()
    
    db = get_db().database
    users_col = db.users
    
    doc = users_col.find_one({"_id": user.lower()})
    
    if not doc or not check_password_hash(doc['password'], pwd):
        emit('cloud_action_result', {'success': False, 'msg': "Błąd autoryzacji!"})
        return
        
    save_data = doc.get('save_data')
    if not save_data:
        emit('cloud_action_result', {'success': False, 'msg': "Brak zapisu na koncie!"})
    else:
        emit('cloud_download_data', {'save_data': save_data})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
