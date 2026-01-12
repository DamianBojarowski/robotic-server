# ZAPISZ JAKO: server.py (Na komputerze)
# Wymaga: pip install flask flask-socketio eventlet
from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

rooms = {}

@socketio.on('join_game')
def on_join(data):
    room = data['room']
    user = data['username']
    join_room(room)
    if room not in rooms: rooms[room] = []
    rooms[room].append(user)
    print(f"Gracz {user} dołączył do pokoju {room}")
    # Jeśli są 2 osoby, start!
    if len(rooms[room]) >= 2:
        emit('game_start', {'players': rooms[room]}, to=room)

@socketio.on('update_progress')
def on_update(data):
    # Przekaż wynik gracza do innych w pokoju
    room = data['room']
    emit('opponent_progress', data, to=room, include_self=False)

if __name__ == '__main__':
    # Host 0.0.0.0 pozwala łączyć się z innych urządzeń w WiFi
    socketio.run(app, host='0.0.0.0', port=5000)