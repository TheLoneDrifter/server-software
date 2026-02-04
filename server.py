import socket
import threading
import time
import json
import random
import math
import configparser
import os
from enum import Enum

def get_local_ip():
    """Get the local network IP address"""
    try:
        # Create a socket to connect to an external address
        # This forces the system to reveal the local network IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # Connect to a public DNS server (doesn't actually send data)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        return local_ip
    except Exception:
        # Fallback to localhost if network detection fails
        return "127.0.0.1"

class GameState(Enum):
    MENU = 1
    PLAYING = 2
    PAUSED = 3
    GAME_OVER = 4

class Difficulty(Enum):
    EASY = 1
    MEDIUM = 2
    HARD = 3

class GameServer:
    def __init__(self, host=None, port=None, max_players=None):
        # Load configuration from INI file
        self.config = self.load_server_config()
        
        # If no host specified, use the local network IP
        if host is None:
            host = get_local_ip()
        self.host = host
        
        # Use config port or default
        if port is None:
            port = self.config.getint('Server', 'Port', fallback=5555)
        self.port = port
        
        # Use config values or defaults
        if max_players is None:
            max_players = self.config.getint('Server', 'MaxPlayers', fallback=4)
            # Validate max_players is between 0-4 (0 requires token)
            max_players = max(0, min(4, max_players))
            
        # Check if max_players is 0, then require token authentication
        if max_players == 0:
            if not self.authenticate_partnership():
                print("ERROR: MaxPlayers set to 0 requires valid TOKEN file for partnership authentication.")
                print("Please obtain a partnership token and place it in a file named 'TOKEN' in the server directory.")
                exit(1)
        
        self.max_players = max_players
        self.server_description = self.config.get('Server', 'Description', fallback='Stalked Game Server')
        
        # Load difficulty from config
        difficulty_str = self.config.get('Server', 'Difficulty', fallback='MEDIUM').upper()
        try:
            self.difficulty = Difficulty[difficulty_str]
        except KeyError:
            self.difficulty = Difficulty.MEDIUM
            
        self.clients = {}  # {client_id: {'socket': socket, 'address': address, 'player_data': {}}}
        self.game_state = GameState.MENU
        self.running = True
        self.server_socket = None
        self.auto_started = False  # Flag to prevent multiple auto-starts
        
        # Game state
        self.players = {}  # {client_id: player_data}
        self.chasers = []
        self.bullets = []
        self.powerups = []
        self.game_time = 0
        self.last_update = time.time()
        
        # Global server score
        self.global_score = 0
        self.last_global_score_time = 0
        self.global_score_interval = 10  # Add 1 point every 10 seconds
        
        # Server configuration
        self.update_rate = 30  # Hz
        self.tick_rate = 60    # Hz
        
        # Player damage tracking
        self.player_damage_cooldowns = {}  # {player_id: last_damage_time}
        self.damage_cooldown_duration = 1.0  # 1 second cooldown between damage
        
        # Chaser respawn system
        self.chaser_respawn_times = {}  # {chaser_id: respawn_time}
        self.chaser_respawn_delay = 2.0  # Respawn after 2 seconds
        
        # Start server threads
        self.start_server()
        
    def authenticate_partnership(self):
        """Authenticate partnership by reading TOKEN file"""
        try:
            if not os.path.exists('TOKEN'):
                return False
                
            with open('TOKEN', 'r') as token_file:
                token = token_file.read().strip()
                
            # Check for exact partnership token match
            expected_token = "NbwUmTmxRkKRmiTs4C79n3D5Z2NkThWwru4QjQ6LCAeoT3xzjVjRpLaXrcciz0cDgIfBk0BZPQpfHdB0OCFHYHNuwr7L2DnFuWWHt6JhvXgK27tGWMPhz4ZsvCRieMFG"
            
            if token == expected_token:
                print("Partnership authenticated successfully!")
                return True
            else:
                print("Invalid partnership token!")
                return False
            
        except Exception as e:
            print(f"Error reading TOKEN file: {e}")
            return False
            
    def load_server_config(self):
        """Load server configuration from serverconfig.ini, create default if missing"""
        config = configparser.ConfigParser()
        
        # Create default config file if it doesn't exist
        if not os.path.exists('serverconfig.ini'):
            print("Creating default serverconfig.ini...")
            config['Server'] = {
                'Description': 'Stalked Game Server',
                'MaxPlayers': '4',
                'Difficulty': 'MEDIUM',
                'Port': '5555'
            }
            print("Note: Set MaxPlayers to 0 in serverconfig.ini for unlimited players (requires partnership TOKEN file)")
            with open('serverconfig.ini', 'w') as configfile:
                config.write(configfile)
            print("Default serverconfig.ini created successfully!")
        
        try:
            config.read('serverconfig.ini')
            return config
        except Exception as e:
            print(f"Error loading server config: {e}")
            return configparser.ConfigParser()
        
    def start_server(self):
        """Initialize and start the TCP server"""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(self.max_players)
        
        print(f"Server started on {self.host}:{self.port}")
        print(f"Description: {self.server_description}")
        if self.max_players == 0:
            print(f"Maximum players: Unlimited (Partnership mode)")
        else:
            print(f"Maximum players: {self.max_players}")
        print(f"Difficulty: {self.difficulty.name}")
        
        # Start accepting connections
        accept_thread = threading.Thread(target=self.accept_connections, daemon=True)
        accept_thread.start()
        
        # Start game loop
        game_thread = threading.Thread(target=self.game_loop, daemon=True)
        game_thread.start()
        
        # Start broadcast loop
        broadcast_thread = threading.Thread(target=self.broadcast_loop, daemon=True)
        broadcast_thread.start()
        
        # Auto-start the game after a short delay
        auto_start_thread = threading.Thread(target=self.auto_start_game, daemon=True)
        auto_start_thread.start()
        
    def accept_connections(self):
        """Accept new client connections"""
        while self.running:
            try:
                client_socket, client_address = self.server_socket.accept()
                
                if len(self.clients) >= self.max_players and self.max_players != 0:
                    # Server is full
                    response = {'type': 'connection_rejected', 'reason': 'Server is full'}
                    self.send_to_client(client_socket, response)
                    client_socket.close()
                    continue
                
                # Assign client ID
                # Find the lowest available client ID (reuse IDs from disconnected players)
                used_ids = set(self.clients.keys())
                client_id = 1
                while client_id in used_ids:
                    client_id += 1
                
                # Store client info
                self.clients[client_id] = {
                    'socket': client_socket,
                    'address': client_address,
                    'connected': True,
                    'last_heartbeat': time.time()
                }
                
                # Initialize player data
                self.players[client_id] = {
                    'id': client_id,
                    'x': 400,
                    'y': 300,
                    'angle': 0,
                    'health': 6,
                    'max_health': 6,
                    'score': 0,
                    'character': 0,
                    'sword_attacking': False,
                    'speed_boost_active': False,
                    'immunity_boost_active': False
                }
                
                # Send welcome message
                welcome_msg = {
                    'type': 'connected',
                    'client_id': client_id,
                    'max_players': self.max_players,
                    'current_players': len(self.clients),
                    'game_state': self.game_state.value,
                    'server_description': self.server_description,
                    'difficulty': self.difficulty.value
                }
                self.send_to_client(client_socket, welcome_msg)
                
                # Broadcast new player to all clients
                self.broadcast_player_joined(client_id)
                
                print(f"Client {client_id} connected from {client_address}")
                if self.max_players == 0:
                    print(f"Players online: {len(self.clients)} (Unlimited)")
                else:
                    print(f"Players online: {len(self.clients)}/{self.max_players}")
                
                # Start client handler thread
                client_thread = threading.Thread(target=self.handle_client, args=(client_id,), daemon=True)
                client_thread.start()
                
            except Exception as e:
                if self.running:
                    print(f"Error accepting connection: {e}")
                    
    def handle_client(self, client_id):
        """Handle messages from a specific client"""
        client_socket = self.clients[client_id]['socket']
        buffer = ""
        
        while self.running and self.clients[client_id]['connected']:
            try:
                data = client_socket.recv(4096).decode('utf-8')
                if not data:
                    break
                    
                buffer += data
                
                # Process complete messages (assuming JSON messages separated by newlines)
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    if line.strip():
                        try:
                            message = json.loads(line.strip())
                            self.process_client_message(client_id, message)
                        except json.JSONDecodeError:
                            print(f"Invalid JSON from client {client_id}: {line}")
                            
            except ConnectionResetError:
                break
            except Exception as e:
                print(f"Error handling client {client_id}: {e}")
                break
                
        # Client disconnected
        self.disconnect_client(client_id)
        
    def process_client_message(self, client_id, message):
        """Process messages from clients"""
        msg_type = message.get('type')
        
        if msg_type == 'player_update':
            # Update player position and state
            if client_id in self.players:
                player_data = message.get('data', {})
                player = self.players[client_id]
                
                # Update all player data including character
                for key, value in player_data.items():
                    player[key] = value
                
        elif msg_type == 'player_action':
            # Handle player actions (sword attack, etc.)
            action = message.get('action')
            if action == 'sword_attack' and client_id in self.players:
                self.players[client_id]['sword_attacking'] = True
                self.broadcast_sword_attack(client_id)
                
        elif msg_type == 'heartbeat':
            # Update client heartbeat
            if client_id in self.clients:
                self.clients[client_id]['last_heartbeat'] = time.time()
                
        elif msg_type == 'start_game':
            # Start the game if all players are ready
            if self.game_state == GameState.MENU:
                self.start_game()
                
        elif msg_type == 'set_difficulty':
            # Set game difficulty
            difficulty = message.get('difficulty')
            if difficulty in [d.value for d in Difficulty]:
                self.difficulty = Difficulty(difficulty)
                self.broadcast_difficulty_change()
                
        elif msg_type == 'info_request':
            # Send server info without adding client
            info_response = {
                'type': 'server_info',
                'description': self.server_description,
                'max_players': self.max_players,
                'difficulty': self.difficulty.value
            }
            self.send_to_client(client_socket, info_response)
                
    def disconnect_client(self, client_id):
        """Handle client disconnection"""
        if client_id in self.clients:
            self.clients[client_id]['connected'] = False
            self.clients[client_id]['socket'].close()
            del self.clients[client_id]
            
        if client_id in self.players:
            del self.players[client_id]
            
        # Broadcast disconnection to other clients
        self.broadcast_player_left(client_id)
        
        print(f"Client {client_id} disconnected")
        if self.max_players == 0:
            print(f"Players online: {len(self.clients)} (Unlimited)")
        else:
            print(f"Players online: {len(self.clients)}/{self.max_players}")
        
    def start_game(self):
        """Start the game for all players"""
        self.game_state = GameState.PLAYING
        self.game_time = 0
        self.auto_started = True  # Mark as started to prevent auto-start conflicts
        
        # Reset damage cooldowns
        self.player_damage_cooldowns.clear()
        
        # Reset score tracking
        if hasattr(self, 'player_score_times'):
            self.player_score_times.clear()
        
        # Reset global score
        self.global_score = 0
        self.last_global_score_time = 0
        
        # Reset chaser respawn times
        self.chaser_respawn_times.clear()
        
        # Reset all players
        for player_id in self.players:
            self.players[player_id].update({
                'x': 400,
                'y': 300,
                'health': 6,
                'score': 0,
                'sword_attacking': False
            })
            
        # Initialize game entities
        self.spawn_chasers()
        
        # Broadcast game start
        self.broadcast_game_start()
        
    def spawn_chasers(self):
        """Spawn enemy chasers based on difficulty"""
        self.chasers = []
        num_chasers = self.get_chaser_count()
        
        for i in range(num_chasers):
            # Generate random spawn position
            margin = 100
            center_x, center_y = 400, 300
            min_distance_from_center = 200
            
            attempts = 0
            while attempts < 50:
                x = random.randint(margin, 800 - margin)
                y = random.randint(margin, 600 - margin)
                
                distance_from_center = math.sqrt((x - center_x)**2 + (y - center_y)**2)
                
                if distance_from_center >= min_distance_from_center:
                    break
                attempts += 1
                
            chaser = {
                'id': i,
                'x': x,
                'y': y,
                'angle': 0,
                'speed': self.get_chaser_speed(),
                'health': 1
            }
            self.chasers.append(chaser)
            
    def get_chaser_count(self):
        """Get number of chasers based on difficulty"""
        if self.difficulty == Difficulty.EASY:
            return 1
        elif self.difficulty == Difficulty.MEDIUM:
            return 2
        else:  # HARD
            return 3
            
    def get_chaser_speed(self):
        """Get chaser speed based on difficulty"""
        if self.difficulty == Difficulty.EASY:
            return 2.0
        elif self.difficulty == Difficulty.MEDIUM:
            return 1.0
        else:  # HARD
            return 0.5
            
    def game_loop(self):
        """Main game loop"""
        while self.running:
            current_time = time.time()
            dt = current_time - self.last_update
            
            # Check for client heartbeats and disconnect inactive clients
            self.check_client_timeouts()
            
            if self.game_state == GameState.PLAYING:
                self.game_time += dt
                self.update_game_state(dt)
                
            self.last_update = current_time
            time.sleep(1.0 / self.tick_rate)
            
    def check_client_timeouts(self):
        """Check for client timeouts and disconnect inactive clients"""
        current_time = time.time()
        timeout_threshold = 60  # 60 seconds timeout
        
        disconnected_clients = []
        for client_id, client_info in self.clients.items():
            if client_info['connected']:
                # Check if client hasn't sent heartbeat in timeout_threshold seconds
                if current_time - client_info['last_heartbeat'] > timeout_threshold:
                    print(f"Client {client_id} timed out")
                    disconnected_clients.append(client_id)
        
        # Disconnect timed out clients
        for client_id in disconnected_clients:
            self.disconnect_client(client_id)
            
    def update_game_state(self, dt):
        """Update game state"""
        # Update chasers
        self.update_chasers(dt)
        
        # Spawn bullets from chasers
        self.spawn_bullets()
        
        # Update bullets
        self.update_bullets(dt)
        
        # Check collisions
        self.check_collisions()
        
        # Spawn powerups
        self.spawn_powerups()
        
        # Update chaser respawns
        self.update_chaser_respawns()
        
        # Update individual player scores (legacy - kept for compatibility)
        for player_id, player in self.players.items():
            # Use a simple score tracking system
            if not hasattr(self, 'player_score_times'):
                self.player_score_times = {}
            if player_id not in self.player_score_times:
                self.player_score_times[player_id] = self.game_time
                
            if self.game_time - self.player_score_times[player_id] >= 10:  # 10 seconds
                player['score'] += 1
                self.player_score_times[player_id] = self.game_time
        
        # Update global server score (add 1 point every 10 seconds)
        if self.game_time - self.last_global_score_time >= self.global_score_interval:
            self.global_score += 1
            self.last_global_score_time = self.game_time
        
    def update_chasers(self, dt):
        """Update chaser positions"""
        if not self.players:
            return
            
        for chaser in self.chasers:
            # Find nearest player
            nearest_player = None
            min_distance = float('inf')
            
            for player in self.players.values():
                distance = math.sqrt((player['x'] - chaser['x'])**2 + (player['y'] - chaser['y'])**2)
                if distance < min_distance:
                    min_distance = distance
                    nearest_player = player
                    
            if nearest_player:
                # Check if chaser is within player's light radius (128 pixels)
                if min_distance < 128:
                    # Chaser is in light - stop moving
                    continue
                    
                # Move towards nearest player if not in light
                dx = nearest_player['x'] - chaser['x']
                dy = nearest_player['y'] - chaser['y']
                distance = math.sqrt(dx**2 + dy**2)
                
                if distance > 0:
                    dx /= distance
                    dy /= distance
                    
                    chaser['x'] += dx * chaser['speed']
                    chaser['y'] += dy * chaser['speed']
                    
                    # Update angle to face player
                    chaser['angle'] = math.degrees(math.atan2(dy, dx))
                    
    def spawn_bullets(self):
        """Spawn bullets from chasers periodically"""
        current_time = self.game_time
        
        # Check if it's time to spawn bullets (based on difficulty)
        spawn_interval = self.get_bullet_spawn_interval()
        
        if not hasattr(self, 'last_bullet_time'):
            self.last_bullet_time = 0
            
        if current_time - self.last_bullet_time >= spawn_interval:
            for chaser in self.chasers:
                # Find nearest player
                if self.players:
                    nearest_player = None
                    min_distance = float('inf')
                    
                    for player in self.players.values():
                        distance = math.sqrt((player['x'] - chaser['x'])**2 + (player['y'] - chaser['y'])**2)
                        if distance < min_distance:
                            min_distance = distance
                            nearest_player = player
                    
                    if nearest_player and min_distance < 400:  # Only shoot if player is within range
                        # Check if chaser is within player's light radius (128 pixels)
                        if min_distance < 128:
                            # Chaser is in light - don't shoot
                            continue
                        
                        # Calculate direction to player
                        dx = nearest_player['x'] - chaser['x']
                        dy = nearest_player['y'] - chaser['y']
                        distance = math.sqrt(dx**2 + dy**2)
                        
                        if distance > 0:
                            dx /= distance
                            dy /= distance
                            
                            bullet_speed = self.get_bullet_speed()
                            
                            bullet = {
                                'x': chaser['x'],
                                'y': chaser['y'],
                                'dx': dx * bullet_speed,
                                'dy': dy * bullet_speed
                            }
                            self.bullets.append(bullet)
            
            self.last_bullet_time = current_time
            
    def get_bullet_spawn_interval(self):
        """Get bullet spawn interval based on difficulty"""
        if self.difficulty == Difficulty.EASY:
            return 3.0  # 3 seconds between shots
        elif self.difficulty == Difficulty.MEDIUM:
            return 2.0  # 2 seconds between shots
        else:  # HARD
            return 1.0  # 1 second between shots
            
    def get_bullet_speed(self):
        """Get bullet speed based on difficulty"""
        if self.difficulty == Difficulty.EASY:
            return 3.0
        elif self.difficulty == Difficulty.MEDIUM:
            return 5.0
        else:  # HARD
            return 7.0
            
    def update_bullets(self, dt):
        """Update bullet positions"""
        for bullet in self.bullets[:]:
            bullet['x'] += bullet['dx']
            bullet['y'] += bullet['dy']
            
            # Remove bullets that are off-screen
            if (bullet['x'] < 0 or bullet['x'] > 800 or 
                bullet['y'] < 0 or bullet['y'] > 600):
                self.bullets.remove(bullet)
                
    def check_collisions(self):
        """Check for collisions between game entities"""
        # Check player-bullet collisions (not player-chaser)
        for player_id, player in self.players.items():
            for bullet in self.bullets[:]:
                distance = math.sqrt((player['x'] - bullet['x'])**2 + (player['y'] - bullet['y'])**2)
                
                if distance < 20:  # Bullet hit radius
                    if not player.get('immunity_boost_active', False):
                        # Check if player is on damage cooldown
                        current_time = time.time()
                        last_damage_time = self.player_damage_cooldowns.get(player_id, 0)
                        
                        if current_time - last_damage_time >= self.damage_cooldown_duration:
                            # Apply damage
                            player['health'] -= 1
                            self.player_damage_cooldowns[player_id] = current_time
                            
                            if player['health'] <= 0:
                                player['health'] = 0
                                # Handle player death
                                self.handle_player_death(player_id)
                    
                    # Remove bullet after hit
                    self.bullets.remove(bullet)
                    break
                            
        # Check sword-chaser collisions
        for player_id, player in self.players.items():
            if player.get('sword_attacking', False):
                for chaser in self.chasers[:]:
                    distance = math.sqrt((player['x'] - chaser['x'])**2 + (player['y'] - chaser['y'])**2)
                    
                    if distance < 80:  # Sword radius
                        # Mark chaser for respawn instead of removing it
                        self.chaser_respawn_times[chaser['id']] = self.game_time + self.chaser_respawn_delay
                        self.chasers.remove(chaser)
                        player['score'] += 5
                        self.global_score += 5  # Add to global score
                        player['sword_attacking'] = False
                        break
                        
    def handle_player_death(self, player_id):
        """Handle player death"""
        # Respawn player after delay
        player = self.players[player_id]
        player['x'] = 400
        player['y'] = 300
        player['health'] = 6
        
        # Broadcast player respawn to all clients
        self.broadcast_player_respawn(player_id)
        
    def broadcast_player_respawn(self, player_id):
        """Broadcast that a player respawned"""
        message = {
            'type': 'player_respawned',
            'player_id': player_id,
            'player_data': self.players[player_id]
        }
        self.broadcast_to_all(message)
        
    def spawn_powerups(self):
        """Spawn powerups periodically"""
        # Simple powerup spawning logic
        if random.random() < 0.001:  # 0.1% chance per frame
            powerup = {
                'type': random.choice(['health', 'speed', 'immunity']),
                'x': random.randint(50, 750),
                'y': random.randint(50, 550)
            }
            self.powerups.append(powerup)
            
    def update_chaser_respawns(self):
        """Update chaser respawns"""
        current_time = self.game_time
        respawned_chasers = []
        
        for chaser_id, respawn_time in list(self.chaser_respawn_times.items()):
            if current_time >= respawn_time:
                # Respawn the chaser
                self.spawn_single_chaser(chaser_id)
                respawned_chasers.append(chaser_id)
        
        # Remove respawned chasers from the respawn list
        for chaser_id in respawned_chasers:
            del self.chaser_respawn_times[chaser_id]
            
    def spawn_single_chaser(self, chaser_id):
        """Spawn a single chaser with the given ID"""
        # Generate random spawn position
        margin = 100
        center_x, center_y = 400, 300
        min_distance_from_center = 200
        
        attempts = 0
        while attempts < 50:
            x = random.randint(margin, 800 - margin)
            y = random.randint(margin, 600 - margin)
            
            distance_from_center = math.sqrt((x - center_x)**2 + (y - center_y)**2)
            
            if distance_from_center >= min_distance_from_center:
                break
            attempts += 1
            
        chaser = {
            'id': chaser_id,
            'x': x,
            'y': y,
            'angle': 0,
            'speed': self.get_chaser_speed(),
            'health': 1
        }
        self.chasers.append(chaser)
            
    def broadcast_loop(self):
        """Broadcast game state to all clients"""
        while self.running:
            if self.clients:
                game_state = {
                    'type': 'game_state',
                    'state': self.game_state.value,
                    'players': list(self.players.values()),
                    'chasers': self.chasers,
                    'bullets': self.bullets,
                    'powerups': self.powerups,
                    'game_time': self.game_time,
                    'difficulty': self.difficulty.value,
                    'global_score': self.global_score
                }
                
                self.broadcast_to_all(game_state)
                
            time.sleep(1.0 / self.update_rate)
            
    def send_to_client(self, client_socket, message):
        """Send a message to a specific client"""
        try:
            data = json.dumps(message) + '\n'
            client_socket.send(data.encode('utf-8'))
        except Exception as e:
            print(f"Error sending to client: {e}")
            
    def broadcast_to_all(self, message):
        """Broadcast message to all connected clients"""
        disconnected_clients = []
        
        # Create a snapshot of clients to avoid iteration issues
        clients_snapshot = list(self.clients.items())
        
        for client_id, client_info in clients_snapshot:
            try:
                self.send_to_client(client_info['socket'], message)
            except Exception as e:
                print(f"Error broadcasting to client {client_id}: {e}")
                disconnected_clients.append(client_id)
                
        # Remove disconnected clients
        for client_id in disconnected_clients:
            self.disconnect_client(client_id)
            
    def broadcast_player_joined(self, client_id):
        """Broadcast that a player joined"""
        message = {
            'type': 'player_joined',
            'player_id': client_id,
            'player_data': self.players[client_id]
        }
        self.broadcast_to_all(message)
        
    def broadcast_player_left(self, client_id):
        """Broadcast that a player left"""
        message = {
            'type': 'player_left',
            'player_id': client_id
        }
        self.broadcast_to_all(message)
        
    def broadcast_game_start(self):
        """Broadcast that the game started"""
        message = {
            'type': 'game_started',
            'difficulty': self.difficulty.value
        }
        self.broadcast_to_all(message)
        
    def broadcast_difficulty_change(self):
        """Broadcast difficulty change"""
        message = {
            'type': 'difficulty_changed',
            'difficulty': self.difficulty.value
        }
        self.broadcast_to_all(message)
        
    def broadcast_sword_attack(self, client_id):
        """Broadcast sword attack"""
        message = {
            'type': 'sword_attack',
            'player_id': client_id
        }
        self.broadcast_to_all(message)
        
    def stop(self):
        """Stop the server"""
        self.running = False
        
        # Close all client connections
        for client_info in self.clients.values():
            client_info['socket'].close()
            
        # Close server socket
        if self.server_socket:
            self.server_socket.close()
            
        print("Server stopped")
        
    def auto_start_game(self):
        """Automatically start the game after a delay"""
        time.sleep(3)  # Wait 3 seconds for players to potentially connect
        if self.game_state == GameState.MENU and not self.auto_started:
            print("Auto-starting game...")
            self.auto_started = True
            self.start_game()

def main():
    """Main function to run the server"""
    # Automatically detect local network IP and load config
    server = GameServer()
    
    print(f"Server starting on {server.host}:{server.port}")
    print(f"Server Description: {server.server_description}")
    if server.max_players == 0:
        print(f"Max Players: Unlimited (Partnership mode)")
    else:
        print(f"Max Players: {server.max_players}")
    print(f"Difficulty: {server.difficulty.name}")
    print(f"Players can connect to: {server.host}:{server.port}")
    
    try:
        # Keep server running
        while server.running:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.stop()

if __name__ == "__main__":
    main()
