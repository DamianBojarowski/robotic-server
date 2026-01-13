from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sekret_robotow'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Struktura: { 'ROOM_ID': { 'pass': '123', 'goal': '...', 'players': {} } }
rooms_data = {}

@socketio.on('connect')
def on_connect():
    # Wyślij listę pokoi nowemu graczowi
    emit('rooms_list_update', get_public_rooms_list())

@socketio.on('create_room')
def on_create(data):
    room = data['room']
    user = data['username']
    pwd = data.get('password', '') # Puste = brak hasła
    
    # Parametry celu
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
        'players': {user: {'money': 0, 'mps': 0}}
    }
    
    print(f"[CREATE] {user} -> {room} (Hasło: {'TAK' if pwd else 'NIE'})")
    
    # Potwierdzenie dla twórcy
    emit('join_success', {
        'room': room, 
        'goal_desc': f"{g_val} {g_type}",
        'is_new': True
    })
    
    # Aktualizacja listy pokoi dla wszystkich
    socketio.emit('rooms_list_update', get_public_rooms_list())

@socketio.on('join_room_request')
def on_join_req(data):
    room = data['room']
    user = data['username']
    pwd_attempt = data.get('password', '')
    
    if room not in rooms_data:
        emit('error_log', {'msg': "Pokój nie istnieje!"})
        return
    
    real_pwd = rooms_data[room]['password']
    
    # Weryfikacja hasła
    if real_pwd and real_pwd != pwd_attempt:
        emit('error_log', {'msg': "BŁĘDNE HASŁO!"})
        return

    join_room(room)
    rooms_data[room]['players'][user] = {'money': 0, 'mps': 0}
    
    emit('join_success', {
        'room': room,
        'goal_desc': f"{rooms_data[room]['goal_value']} {rooms_data[room]['goal_type']}",
        'is_new': False
    })

@socketio.on('request_rooms_list')
def on_list_req():
    emit('rooms_list_update', get_public_rooms_list())

def get_public_rooms_list():
    # Zwraca listę pokoi (bez haseł, tylko info czy jest wymagane)
    public_list = []
    for r_id, r_data in rooms_data.items():
        public_list.append({
            'name': r_id,
            'goal': f"{r_data['goal_value']}",
            'players': len(r_data['players']),
            'locked': bool(r_data['password'])
        })
    return public_list

# ... (reszta: update_progress bez zmian) ...
@socketio.on('update_progress')
def on_update(data):
    room = data.get('room')
    if room in rooms_data:
        # Rozsyłamy update
        emit('opponent_progress', data, to=room, include_self=False)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
