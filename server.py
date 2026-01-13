from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sekret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Struktura: { 'ROOM_ID': { 'goal_type': 'money', 'goal_value': 1000, 'players': {} } }
rooms_data = {}

@app.route('/')
def index():
    return "Serwer Robotic Empire v2 (Lobby Support)"

@socketio.on('create_room')
def on_create(data):
    room = data['room']
    user = data['username']
    goal_type = data.get('goal_type', 'money') # 'money' lub 'prod'
    goal_value = float(data.get('goal_value', 1000000))
    
    join_room(room)
    
    if room not in rooms_data:
        rooms_data[room] = {
            'goal_type': goal_type,
            'goal_value': goal_value,
            'players': {}
        }
    
    # Zapisujemy gracza
    rooms_data[room]['players'][user] = {'money': 0, 'mps': 0}
    
    print(f"[CREATE] {user} stworzył pokój {room} (Cel: {goal_type}={goal_value})")
    
    # Odsyłamy info o sukcesie i parametrach pokoju
    emit('room_joined', {
        'room': room, 
        'goal_type': goal_type, 
        'goal_value': goal_value,
        'players': rooms_data[room]['players']
    }, to=room)

@socketio.on('join_room_request')
def on_join_req(data):
    room = data['room']
    user = data['username']
    
    if room in rooms_data:
        join_room(room)
        rooms_data[room]['players'][user] = {'money': 0, 'mps': 0}
        
        print(f"[JOIN] {user} dołączył do {room}")
        
        # Wysyłamy nowemu graczowi info o pokoju
        emit('room_joined', {
            'room': room,
            'goal_type': rooms_data[room]['goal_type'],
            'goal_value': rooms_data[room]['goal_value'],
            'players': rooms_data[room]['players']
        }, to=room)
    else:
        emit('error_log', {'msg': f"Pokój {room} nie istnieje!"}, to=request.sid)

@socketio.on('update_progress')
def on_update(data):
    room = data['room']
    user = data.get('username', 'Anonim')
    
    if room in rooms_data:
        # Aktualizujemy stan gracza na serwerze
        rooms_data[room]['players'][user] = {
            'money': data.get('money', 0),
            'mps': data.get('mps', 0)
        }
        # Rozsyłamy update do wszystkich w pokoju
        emit('opponent_progress', {
            'user': user,
            'money': data['money'],
            'mps': data['mps']
        }, to=room, include_self=False)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
