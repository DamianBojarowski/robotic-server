from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import os
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sekret_robotow'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Konfiguracja czyszczenia
INACTIVE_TIMEOUT = 172800  # 48h braku aktywności (48 * 3600)
EMPTY_ROOM_TIMEOUT = 900   # 15 minut pustego pokoju (15 * 60)

# Struktura: { 'ROOM_ID': { ... 'last_active': timestamp } }
rooms_data = {}

def cleanup_loop():
    """Wątek sprzątający martwe pokoje"""
    while True:
        socketio.sleep(60) # Sprawdzaj co minutę
        now = time.time()
        to_delete = []
        
        for r_id, r_data in rooms_data.items():
            # 1. Kryterium: Pusty pokój "waiting" wisi za długo
            if r_data['status'] == 'waiting' and len(r_data['players']) < 2:
                if now - r_data['last_active'] > EMPTY_ROOM_TIMEOUT:
                    to_delete.append(r_id)
                    continue

            # 2. Kryterium: Brak aktywności w grze przez X czasu
            if now - r_data['last_active'] > INACTIVE_TIMEOUT:
                to_delete.append(r_id)
        
        for r_id in to_delete:
            print(f"--- CZYSZCZENIE: Usuwanie nieaktywnego pokoju {r_id} ---")
            del rooms_data[r_id]
        
        if to_delete:
            socketio.emit('rooms_list_update', get_public_rooms_list())

# Uruchomienie tła
socketio.start_background_task(cleanup_loop)

@socketio.on('connect')
def on_connect():
    emit('rooms_list_update', get_public_rooms_list())

@socketio.on('create_room')
def on_create(data):
    room = data['room']
    user = data['username']
    pwd = data.get('password', '')
    g_type = data.get('goal_type', 'money')
    g_val = data.get('goal_value', 1000000)
    
    if room in rooms_data:
        emit('error_log', {'msg': f"Pokój {room} już istnieje!"})
        return

    join_room(room)
    
    rooms_data[room] = {
        'password': pwd,
        'goal_type': g_type,
        'goal_value': g_val,
        'players': {user: {'money': 0, 'mps': 0}},
        'status': 'waiting',
        'last_active': time.time() # Znacznik czasu
    }
    
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
    user = data['username']
    pwd_attempt = data.get('password', '')
    
    if room not in rooms_data:
        emit('error_log', {'msg': "Pokój nie istnieje lub wygasł!"})
        return
    
    r_data = rooms_data[room]
    
    if r_data['password'] and r_data['password'] != pwd_attempt:
        emit('error_log', {'msg': "BŁĘDNE HASŁO!"})
        return

    if len(r_data['players']) >= 2 and user not in r_data['players']:
         emit('error_log', {'msg': "Pokój jest pełny!"})
         return

    join_room(room)
    r_data['players'][user] = {'money': 0, 'mps': 0}
    r_data['last_active'] = time.time() # Odświeżamy aktywność
    
    current_status = r_data['status']
    
    if len(r_data['players']) == 2 and current_status == 'waiting':
        r_data['status'] = 'playing'
        socketio.emit('game_start_signal', {'start_time': 0}, to=room)
        current_status = 'playing'

    emit('join_success', {
        'room': room,
        'goal_desc': f"{r_data['goal_value']} {r_data['goal_type']}",
        'is_new': False,
        'status': current_status
    })
    socketio.emit('rooms_list_update', get_public_rooms_list())

@socketio.on('update_progress')
def on_update(data):
    room = data.get('room')
    if room in rooms_data:
        rooms_data[room]['last_active'] = time.time() # Każdy update resetuje licznik usunięcia
        emit('opponent_progress', data, to=room, include_self=False)

# --- NOWE: Obsługa wyjścia z gry ---
@socketio.on('leave_game')
def on_leave(data):
    room = data.get('room')
    user = data.get('username')
    
    if room in rooms_data:
        if user in rooms_data[room]['players']:
            # Usuwamy gracza z danych pokoju? 
            # W PvP 1vs1 lepiej zostawić "miejsce", ale oznaczyć jako wyjście,
            # albo po prostu powiadomić rywala.
            # Tutaj: powiadamiamy rywala.
            emit('player_left', {'username': user}, to=room)
            
            # Opcjonalnie: Jeśli pokój jest teraz pusty (oba wyszły), usuń go natychmiast
            leave_room(room)
            del rooms_data[room]['players'][user]
            
            if len(rooms_data[room]['players']) == 0:
                del rooms_data[room]
                socketio.emit('rooms_list_update', get_public_rooms_list())

@socketio.on('disconnect')
def on_disconnect():
    pass 

@socketio.on('request_rooms_list')
def on_list_req():
    emit('rooms_list_update', get_public_rooms_list())

def get_public_rooms_list():
    public_list = []
    for r_id, r_data in rooms_data.items():
        if r_data['status'] == 'waiting' or len(r_data['players']) < 2:
            public_list.append({
                'name': r_id,
                'goal': f"{r_data['goal_value']}",
                'players': len(r_data['players']),
                'locked': bool(r_data['password'])
            })
    return public_list

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
