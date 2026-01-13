from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sekret_robotow'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Struktura: { 
#   'ROOM_ID': { 
#       'password': '...', 
#       'players': {'Nick': {...}}, 
#       'status': 'waiting'  <-- NOWE: 'waiting' lub 'playing'
#   } 
# }
rooms_data = {}

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
        'status': 'waiting' # Domyślnie czekamy
    }
    
    # Informujemy twórcę, że wszedł, ale musi czekać
    emit('join_success', {
        'room': room, 
        'goal_desc': f"{g_val} {g_type}",
        'is_new': True,
        'status': 'waiting' # Flaga dla klienta, żeby zablokował ekran
    })
    
    socketio.emit('rooms_list_update', get_public_rooms_list())

@socketio.on('join_room_request')
def on_join_req(data):
    room = data['room']
    user = data['username']
    pwd_attempt = data.get('password', '')
    
    if room not in rooms_data:
        emit('error_log', {'msg': "Pokój nie istnieje!"})
        return
    
    r_data = rooms_data[room]
    
    if r_data['password'] and r_data['password'] != pwd_attempt:
        emit('error_log', {'msg': "BŁĘDNE HASŁO!"})
        return

    # Jeśli gra już trwa, a my nie jesteśmy na liście (obserwator) - opcjonalnie blokada
    if len(r_data['players']) >= 2 and user not in r_data['players']:
         emit('error_log', {'msg': "Pokój jest pełny!"})
         return

    join_room(room)
    r_data['players'][user] = {'money': 0, 'mps': 0}
    
    # Sprawdzamy stan
    current_status = r_data['status']
    
    # Jeśli to drugi gracz, uruchamiamy grę!
    if len(r_data['players']) == 2 and current_status == 'waiting':
        r_data['status'] = 'playing'
        # Wysyłamy sygnał START do wszystkich w pokoju (w tym do tego co czekał)
        socketio.emit('game_start_signal', {'start_time': 0}, to=room)
        current_status = 'playing'

    emit('join_success', {
        'room': room,
        'goal_desc': f"{r_data['goal_value']} {r_data['goal_type']}",
        'is_new': False,
        'status': current_status
    })
    
    socketio.emit('rooms_list_update', get_public_rooms_list())

@socketio.on('disconnect')
def on_disconnect():
    # Proste czyszczenie pustych pokoi
    # W prawdziwej produkcji lepiej użyć session_id do mapowania gracza na pokój
    pass 

@socketio.on('request_rooms_list')
def on_list_req():
    emit('rooms_list_update', get_public_rooms_list())

def get_public_rooms_list():
    public_list = []
    for r_id, r_data in rooms_data.items():
        # Nie pokazujemy pokoi, które już grają i są pełne
        if r_data['status'] == 'waiting' or len(r_data['players']) < 2:
            public_list.append({
                'name': r_id,
                'goal': f"{r_data['goal_value']}",
                'players': len(r_data['players']),
                'locked': bool(r_data['password'])
            })
    return public_list

@socketio.on('update_progress')
def on_update(data):
    room = data.get('room')
    if room in rooms_data:
        # Rozsyłamy update
        emit('opponent_progress', data, to=room, include_self=False)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
