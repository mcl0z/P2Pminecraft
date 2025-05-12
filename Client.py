import os
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import uuid
import socket
import json
import logging
import queue
import time
import signal
import asyncio
import websockets
import concurrent.futures
from typing import Dict, List, Optional
import tempfile

# 默认配置
DEFAULT_SERVER = "124.71.76.131"
DEFAULT_PORT = 8080
DEFAULT_LOCAL_PORT = 25565
DEFAULT_REMOTE_PORT = 25565

#################################################
# 兼容性函数
#################################################

# 兼容低版本Python的to_thread函数
async def to_thread(func, *args, **kwargs):
    """在线程池中运行一个函数并返回其结果（兼容Python 3.8）"""
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return await loop.run_in_executor(
            pool, lambda: func(*args, **kwargs)
        )

#################################################
# 内置信令服务器
#################################################

class SignalingServer:
    def __init__(self):
        self.rooms: Dict[str, List[websockets.WebSocketServerProtocol]] = {}
        self.clients: Dict[str, Dict] = {}
        
    async def register(self, websocket, room_id: str, username: str):
        """注册客户端到指定房间"""
        if room_id not in self.rooms:
            self.rooms[room_id] = []
        
        # 生成唯一客户端ID
        client_id = str(uuid.uuid4())
        
        # 保存客户端信息
        self.clients[client_id] = {
            "websocket": websocket,
            "room_id": room_id,
            "username": username
        }
        
        # 将客户端添加到房间
        self.rooms[room_id].append(websocket)
        
        # 通知房间内其他客户端有新用户加入
        if len(self.rooms[room_id]) > 1:
            for client in self.rooms[room_id]:
                if client != websocket:
                    await client.send(json.dumps({
                        "type": "user_joined",
                        "username": username,
                        "client_id": client_id  # 确保包含客户端ID
                    }))
        
        return client_id
    
    async def unregister(self, websocket):
        """注销客户端"""
        # 查找客户端ID
        client_id = None
        for cid, info in self.clients.items():
            if info["websocket"] == websocket:
                client_id = cid
                break
        
        if client_id:
            room_id = self.clients[client_id]["room_id"]
            username = self.clients[client_id]["username"]
            
            # 从房间中移除
            if room_id in self.rooms:
                self.rooms[room_id].remove(websocket)
                if not self.rooms[room_id]:
                    del self.rooms[room_id]
                else:
                    # 通知房间内其他客户端该用户离开
                    for client in self.rooms[room_id]:
                        await client.send(json.dumps({
                            "type": "user_left",
                            "username": username,
                            "client_id": client_id
                        }))
            
            # 从客户端列表中移除
            del self.clients[client_id]
    
    async def relay_message(self, websocket, message: dict):
        """转发消息到房间内其他客户端"""
        # 查找发送者所在房间
        room_id = None
        sender_id = None
        target_id = message.get("target")  # 获取目标客户端ID（如果存在）
        
        for client_id, info in self.clients.items():
            if info["websocket"] == websocket:
                room_id = info["room_id"]
                sender_id = client_id
                break
        
        if room_id:
            # 添加发送者ID到消息中
            message["sender_id"] = sender_id
            
            # 如果有指定目标客户端，只发送给目标客户端
            if target_id:
                # 查找目标客户端
                for client_id, info in self.clients.items():
                    if client_id == target_id and info["room_id"] == room_id:
                        try:
                            await info["websocket"].send(json.dumps(message))
                            logging.info(f"消息已发送给特定客户端: {target_id}")
                        except Exception as e:
                            logging.error(f"发送消息给客户端 {target_id} 失败: {e}")
                        return  # 发送给指定客户端后返回
            else:
                # 如果没有指定目标，转发给房间内所有其他客户端
                for client in self.rooms[room_id]:
                    if client != websocket:
                        await client.send(json.dumps(message))

async def handler(websocket):
    """处理WebSocket连接"""
    try:
        # 等待客户端发送加入房间消息
        message = await websocket.recv()
        data = json.loads(message)
        
        if data["type"] == "join":
            room_id = data["room_id"]
            username = data["username"]
            
            # 获取SignalingServer实例
            signaling_server = websocket.signaling_server
            
            # 注册客户端
            client_id = await signaling_server.register(websocket, room_id, username)
            
            # 发送确认消息
            await websocket.send(json.dumps({
                "type": "joined",
                "room_id": room_id,
                "client_id": client_id,
                "peers": len(signaling_server.rooms[room_id]) - 1
            }))
            
            logging.info(f"客户端 {username} (ID: {client_id}) 加入房间 {room_id}")
            
            # 为每个客户端保存一个映射，记录它与哪些客户端建立了通信
            if not hasattr(websocket, "peer_mappings"):
                websocket.peer_mappings = {}
            
            # 处理客户端消息
            async for message in websocket:
                data = json.loads(message)
                
                # 转发WebRTC信令
                if data["type"] in ["offer", "answer", "ice_candidate"]:
                    # 如果是offer，记录发起连接的目标
                    if data["type"] == "offer" and "sender_id" in data:
                        # 发送者将成为接收者的对等方
                        sender_id = data.get("sender_id")
                        if sender_id:
                            websocket.peer_mappings[sender_id] = client_id
                            # 在offer中添加目标ID
                            data["target"] = sender_id
                    
                    # 如果是answer或ice_candidate，查找映射关系确定目标
                    elif (data["type"] == "answer" or data["type"] == "ice_candidate") and "sender_id" in data:
                        sender_id = data.get("sender_id")
                        if sender_id and sender_id in websocket.peer_mappings:
                            # 添加目标ID
                            data["target"] = websocket.peer_mappings[sender_id]
                    
                    await signaling_server.relay_message(websocket, data)
        
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        logging.error(f"信令服务器错误: {e}")
        import traceback
        logging.error(traceback.format_exc())
    finally:
        try:
            # 获取SignalingServer实例
            signaling_server = getattr(websocket, "signaling_server", None)
            if signaling_server:
                await signaling_server.unregister(websocket)
        except Exception as e:
            logging.error(f"注销客户端时出错: {e}")

async def run_signaling_server(host="0.0.0.0", port=8080):
    """启动WebSocket服务器"""
    signaling_server = SignalingServer()
    
    async def process_request(path, request_headers):
        # 可以在这里添加请求处理逻辑，如CORS支持等
        return None
    
    async def on_connect(websocket, path=None):
        # 将SignalingServer实例附加到websocket对象
        websocket.signaling_server = signaling_server
        await handler(websocket)
    
    try:
        server = await websockets.serve(
            on_connect, 
            host, 
            port,
            process_request=process_request
        )
        logging.info(f"信令服务器运行在 ws://{host}:{port}")
        
        # 创建一个永不完成的future来保持服务器运行
        stop_event = asyncio.Event()
        await stop_event.wait()
    except Exception as e:
        logging.error(f"启动信令服务器失败: {e}")
        import traceback
        logging.error(traceback.format_exc())
        raise

# 工具函数
def check_port_available(port):
    """检查端口是否可用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

def get_available_port(start_port=25565, max_attempts=10):
    """获取可用端口"""
    port = start_port
    for _ in range(max_attempts):
        if check_port_available(port):
            return port
        port += 1
    return None  # 未找到可用端口

# 配置日志系统
class QueueHandler(logging.Handler):
    """将日志消息发送到队列，以便在GUI中显示"""
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        self.log_queue.put(record)

# 设置日志格式
log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

class MinecraftP2PGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Minecraft P2P 连接器")
        self.root.geometry("800x600")
        self.root.minsize(800, 600)
        
        # 创建日志系统
        self.setup_logging()
        
        # 设置变量
        self.role_var = tk.StringVar(value="server")
        self.room_id_var = tk.StringVar()
        self.local_port_var = tk.StringVar(value=str(DEFAULT_LOCAL_PORT))
        self.remote_port_var = tk.StringVar(value=str(DEFAULT_REMOTE_PORT))
        self.username_var = tk.StringVar(value=f"Player-{str(uuid.uuid4())[:4]}")
        self.server_address_var = tk.StringVar(value=DEFAULT_SERVER)
        self.server_port_var = tk.StringVar(value=str(DEFAULT_PORT))
        
        # 创建界面
        self.create_widgets()
        
        # 绑定关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # 为服务端角色生成随机房间ID（不再为客户端角色生成）
        self.generate_room_id()
        
        # 检查端口状态
        self.check_ports()
        
        # 启动日志更新定时器
        self.root.after(100, self.update_log_display)
        
        # 初始化进程变量
        self.process = None
        self.process_thread = None
        self.running = True
    
    def setup_logging(self):
        """设置日志系统"""
        # 创建日志队列
        self.log_queue = queue.Queue()
        
        # 创建自定义日志处理器
        self.queue_handler = QueueHandler(self.log_queue)
        self.queue_handler.setFormatter(log_formatter)
        
        # 获取root logger并添加处理器
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        
        # 移除已有的相同类型处理器，避免重复
        for handler in root_logger.handlers[:]:
            if isinstance(handler, QueueHandler):
                root_logger.removeHandler(handler)
        
        # 添加队列处理器
        root_logger.addHandler(self.queue_handler)
        
        # 添加直接记录到日志面板的方法
        self.direct_logs = []
    
    def create_widgets(self):
        """创建GUI组件"""
        # 创建主框架
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 创建左侧设置面板
        left_frame = ttk.LabelFrame(main_frame, text="连接设置", padding="10")
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=False, padx=(0, 5))
        
        # 角色选择
        role_frame = ttk.Frame(left_frame)
        role_frame.pack(fill=tk.X, pady=5)
        ttk.Label(role_frame, text="角色:").pack(side=tk.LEFT)
        ttk.Radiobutton(role_frame, text="房主", variable=self.role_var, value="server", 
                        command=self.on_role_change).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(role_frame, text="客户端", variable=self.role_var, value="client", 
                        command=self.on_role_change).pack(side=tk.LEFT, padx=5)
        
        # 房间ID
        room_frame = ttk.Frame(left_frame)
        room_frame.pack(fill=tk.X, pady=5)
        ttk.Label(room_frame, text="房间ID:").pack(side=tk.LEFT)
        self.room_entry = ttk.Entry(room_frame, textvariable=self.room_id_var)
        self.room_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.gen_room_btn = ttk.Button(room_frame, text="生成", command=self.generate_room_id)
        self.gen_room_btn.pack(side=tk.LEFT)
        
        # 用户名
        user_frame = ttk.Frame(left_frame)
        user_frame.pack(fill=tk.X, pady=5)
        ttk.Label(user_frame, text="用户名:").pack(side=tk.LEFT)
        ttk.Entry(user_frame, textvariable=self.username_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        # 本地端口
        local_port_frame = ttk.Frame(left_frame)
        local_port_frame.pack(fill=tk.X, pady=5)
        ttk.Label(local_port_frame, text="本地端口:").pack(side=tk.LEFT)
        self.local_port_entry = ttk.Entry(local_port_frame, textvariable=self.local_port_var)
        self.local_port_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.local_port_status = ttk.Label(local_port_frame, text="", foreground="green")
        self.local_port_status.pack(side=tk.LEFT)
        
        # 远程端口
        remote_port_frame = ttk.Frame(left_frame)
        remote_port_frame.pack(fill=tk.X, pady=5)
        ttk.Label(remote_port_frame, text="远程端口:").pack(side=tk.LEFT)
        ttk.Entry(remote_port_frame, textvariable=self.remote_port_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        # 信令服务器设置
        server_frame = ttk.LabelFrame(left_frame, text="信令服务器", padding="5")
        server_frame.pack(fill=tk.X, pady=10)
        
        # 服务器地址
        server_addr_frame = ttk.Frame(server_frame)
        server_addr_frame.pack(fill=tk.X, pady=5)
        ttk.Label(server_addr_frame, text="地址:").pack(side=tk.LEFT)
        ttk.Entry(server_addr_frame, textvariable=self.server_address_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        # 服务器端口
        server_port_frame = ttk.Frame(server_frame)
        server_port_frame.pack(fill=tk.X, pady=5)
        ttk.Label(server_port_frame, text="端口:").pack(side=tk.LEFT)
        ttk.Entry(server_port_frame, textvariable=self.server_port_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        # 状态信息
        status_frame = ttk.LabelFrame(left_frame, text="连接信息", padding="5")
        status_frame.pack(fill=tk.X, pady=10)
        
        self.status_text = tk.Text(status_frame, height=8, width=30, wrap=tk.WORD)
        self.status_text.pack(fill=tk.BOTH, expand=True)
        self.status_text.config(state=tk.DISABLED)
        
        # 按钮区域
        button_frame = ttk.Frame(left_frame)
        button_frame.pack(fill=tk.X, pady=10)
        
        self.start_button = ttk.Button(button_frame, text="启动连接", command=self.start_connection)
        self.start_button.pack(side=tk.LEFT, padx=5)
        
        self.stop_button = ttk.Button(button_frame, text="停止连接", command=self.stop_connection, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=5)
        
        # 创建右侧日志面板
        right_frame = ttk.LabelFrame(main_frame, text="日志", padding="10")
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))
        
        self.log_text = scrolledtext.ScrolledText(right_frame, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.config(state=tk.DISABLED)
        
        # 状态栏
        status_bar = ttk.Frame(self.root)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        
        self.status_label = ttk.Label(status_bar, text="准备就绪")
        self.status_label.pack(side=tk.LEFT, padx=10)
        
        # 初始化根据角色显示/隐藏控件
        self.on_role_change()
    
    def on_role_change(self):
        """根据选择的角色更新界面"""
        role = self.role_var.get()
        
        if role == "server":
            self.gen_room_btn.config(state=tk.NORMAL)
            self.remote_port_var.set(str(DEFAULT_REMOTE_PORT))
            # 服务端角色自动生成房间ID
            self.generate_room_id()
            self.update_status_text("作为房主，您需要:\n"
                                    "1. 确保您的Minecraft服务器在运行\n"
                                    "2. 将生成的房间ID分享给客户端\n"
                                    "3. 其他玩家将通过信令服务器连接到您\n\n"
                                    f"连接信令服务器: {self.server_address_var.get()}:{self.server_port_var.get()}")
        else:  # client
            self.gen_room_btn.config(state=tk.DISABLED)
            # 客户端角色清空房间ID
            self.room_id_var.set("")
            self.update_status_text("作为客户端，您需要:\n"
                                    "1. 输入房主分享的房间ID\n"
                                    "2. 启动连接后，在Minecraft中添加服务器\n"
                                    f"3. 服务器地址: 127.0.0.1:{self.local_port_var.get()}\n\n"
                                    f"连接信令服务器: {self.server_address_var.get()}:{self.server_port_var.get()}")
        
        # 检查端口状态
        self.check_ports()
    
    def generate_room_id(self):
        """生成随机房间ID"""
        room_id = str(uuid.uuid4())[:8]
        self.room_id_var.set(room_id)
    
    def check_ports(self):
        """检查端口可用性并更新状态"""
        try:
            role = self.role_var.get()
            local_port = int(self.local_port_var.get())
            
            if role == "client":
                if check_port_available(local_port):
                    self.local_port_status.config(text="✓", foreground="green")
                else:
                    self.local_port_status.config(text="✗", foreground="red")
                    # 尝试找到可用端口
                    available_port = get_available_port(local_port)
                    if available_port:
                        self.show_port_warning(local_port, available_port)
            else:
                self.local_port_status.config(text="")
        except ValueError:
            self.local_port_status.config(text="✗", foreground="red")
    
    def show_port_warning(self, current_port, available_port):
        """显示端口占用警告并询问是否切换到可用端口"""
        answer = messagebox.askquestion("端口被占用", 
                                        f"端口 {current_port} 已被占用，是否使用端口 {available_port}？",
                                        icon='warning')
        if answer == 'yes':
            self.local_port_var.set(str(available_port))
            self.check_ports()
    
    def update_status_text(self, text):
        """更新状态信息文本框"""
        self.status_text.config(state=tk.NORMAL)
        self.status_text.delete(1.0, tk.END)
        self.status_text.insert(tk.END, text)
        self.status_text.config(state=tk.DISABLED)
    
    def start_connection(self):
        """启动连接"""
        try:
            # 检查输入
            role = self.role_var.get()
            room_id = self.room_id_var.get().strip()
            username = self.username_var.get().strip()
            local_port = self.local_port_var.get().strip()
            remote_port = self.remote_port_var.get().strip()
            server_address = self.server_address_var.get().strip()
            server_port = self.server_port_var.get().strip()
            
            if not room_id:
                messagebox.showerror("错误", "请输入房间ID")
                return
            
            if not username:
                messagebox.showerror("错误", "请输入用户名")
                return
            
            if not local_port.isdigit() or int(local_port) <= 0 or int(local_port) > 65535:
                messagebox.showerror("错误", "本地端口必须是1-65535之间的数字")
                return
            
            if not remote_port.isdigit() or int(remote_port) <= 0 or int(remote_port) > 65535:
                messagebox.showerror("错误", "远程端口必须是1-65535之间的数字")
                return
            
            # 如果是客户端，检查端口是否可用
            if role == "client" and not check_port_available(int(local_port)):
                available_port = get_available_port(int(local_port))
                if available_port:
                    self.show_port_warning(int(local_port), available_port)
                    return
                else:
                    messagebox.showerror("错误", f"端口 {local_port} 已被占用，且未找到可用端口")
                    return
            
            # 显示连接信息
            connection_info = f"角色: {'房主' if role == 'server' else '客户端'}\n"
            connection_info += f"房间ID: {room_id}\n"
            connection_info += f"用户名: {username}\n"
            connection_info += f"信令服务器: {server_address}:{server_port}\n"
            
            if role == "client":
                connection_info += f"\n在Minecraft中连接到: 127.0.0.1:{local_port}"
            else:
                connection_info += f"\n确保Minecraft服务器在端口 {remote_port} 运行"
            
            connection_info += f"\n\n连接状态: 连接中"
            
            self.update_status_text(connection_info)
            
            # 更新UI状态
            self.start_button.config(state=tk.DISABLED)
            self.stop_button.config(state=tk.NORMAL)
            self.update_connection_status("连接中")
            
            # 清空日志显示
            self.log_text.config(state=tk.NORMAL)
            self.log_text.delete(1.0, tk.END)
            self.log_text.config(state=tk.DISABLED)
            
            # 启动连接
            self.log_message("开始启动连接...")
            
            try:
                # 构建信令服务器URL
                protocol = "wss" if USE_SSL else "ws"
                server_url = f"{protocol}://{server_address}:{server_port}"
                
                # 如果是服务器角色且未指定外部信令服务器，启动内置信令服务器
                if role == "server" and server_address == "127.0.0.1" or server_address == "localhost":
                    # 在新线程中启动异步事件循环来运行信令服务器
                    def run_signaling_server_in_thread():
                        try:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            self.log_message(f"启动内置信令服务器，监听端口 {server_port}...")
                            loop.run_until_complete(run_signaling_server(host="0.0.0.0", port=int(server_port)))
                        except Exception as e:
                            self.log_message(f"信令服务器启动失败: {e}")
                    
                    self.signaling_thread = threading.Thread(target=run_signaling_server_in_thread)
                    self.signaling_thread.daemon = True
                    self.signaling_thread.start()
                    
                    # 等待一段时间让信令服务器启动
                    time.sleep(1)
                
                # 创建并启动P2P客户端
                def run_client_in_thread():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        
                        # 创建客户端实例
                        self.client = MinecraftP2PClient(
                            server_url=server_url,
                            room_id=room_id,
                            username=username,
                            local_port=int(local_port),
                            remote_port=int(remote_port),
                            role=role
                        )
                        
                        # 运行客户端
                        self.log_message("启动P2P连接...")
                        loop.run_until_complete(self.client.run())
                        self.log_message("P2P客户端已停止运行")
                    except Exception as e:
                        self.log_message(f"P2P客户端运行出错: {e}")
                        import traceback
                        self.log_message(traceback.format_exc())
                    finally:
                        # 当客户端结束后更新UI
                        self.root.after(0, self.update_ui_after_process_exit)
                
                self.client_thread = threading.Thread(target=run_client_in_thread)
                self.client_thread.daemon = True
                self.client_thread.start()
                
                self.log_message("P2P客户端已在后台启动，等待连接...")
            except Exception as e:
                self.log_message(f"启动P2P连接失败: {e}")
                import traceback
                self.log_message(traceback.format_exc())
                raise e
                
        except Exception as e:
            print(f"启动连接失败: {e}")
            import traceback
            traceback.print_exc()
            self.log_message(f"启动连接失败: {str(e)}")
            messagebox.showerror("错误", f"启动连接失败: {e}")
            self.start_button.config(state=tk.NORMAL)
            self.stop_button.config(state=tk.DISABLED)
            self.update_connection_status("启动失败", error=True)
    
    def stop_connection(self):
        """停止连接"""
        try:
            self.log_message("正在停止连接...")
            
            # 停止客户端
            if hasattr(self, 'client') and self.client:
                # 在新线程中运行停止操作，避免阻塞主线程
                def run_shutdown_in_thread():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(self.client.shutdown())
                        self.log_message("P2P客户端已成功停止")
                    except Exception as e:
                        self.log_message(f"停止P2P客户端时出错: {e}")
                        import traceback
                        self.log_message(traceback.format_exc())
                
                shutdown_thread = threading.Thread(target=run_shutdown_in_thread)
                shutdown_thread.daemon = True
                shutdown_thread.start()
                
                # 给一点时间让客户端停止
                time.sleep(2)
                
                # 重置客户端引用
                self.client = None
            else:
                self.log_message("没有活动的P2P客户端连接")
            
            # 清理资源
            self.cleanup_resources()
            self.log_message("连接已停止")
        except Exception as e:
            print(f"停止连接时出错: {e}")
            import traceback
            traceback.print_exc()
            self.log_message(f"停止连接时出错: {str(e)}")
            # 即使出错也要尝试清理资源
            self.cleanup_resources()
    
    def cleanup_resources(self):
        """清理连接相关资源"""
        try:
            # 重置客户端变量
            self.client = None
            
            # 更新UI状态
            self.start_button.config(state=tk.NORMAL)
            self.stop_button.config(state=tk.DISABLED)
            self.update_connection_status("已断开连接")
            
            # 如果有信令服务器线程在运行，可能需要等待它完成
            if hasattr(self, 'signaling_thread') and self.signaling_thread and self.signaling_thread.is_alive():
                # 由于无法优雅地终止信令服务器线程，这里只记录一下
                self.log_message("信令服务器线程将在程序退出时自动终止")
                
            # 如果有客户端线程在运行，它应该会自行退出
            if hasattr(self, 'client_thread') and self.client_thread and self.client_thread.is_alive():
                self.log_message("等待客户端线程退出...")
                # 这里不使用join，因为可能会阻塞主线程
        except Exception as e:
            print(f"清理资源时出错: {e}")
            self.log_message(f"清理资源时出错: {str(e)}")
    
    def read_process_output(self):
        """读取进程输出并记录到日志"""
        try:
            # 确认进程还在运行
            if not self.process or self.process.poll() is not None:
                print("进程不存在或已结束，停止读取")
                return
                
            # 逐行读取输出
            for line in iter(self.process.stdout.readline, ''):
                if not line:
                    break
                    
                line = line.strip()
                if not line:
                    continue
                    
                # 过滤掉缓冲区日志，不记录也不显示
                if "数据通道缓冲区低" in line:
                    continue
                
                # 控制台输出
                print(f"进程输出: {line}")
                
                # 分析日志行，检查是否包含连接成功的关键信息
                self.analyze_log_line(line)
                
                # 记录到日志系统
                self.log_message(line)
            
            # 进程退出处理
            return_code = self.process.wait()
            self.log_message(f"进程已退出，退出码: {return_code}")
            
            # 更新UI状态
            self.root.after(0, self.update_ui_after_process_exit)
        except Exception as e:
            print(f"读取进程输出时出错: {e}")
            import traceback
            traceback.print_exc()
            self.log_message(f"读取进程输出时出错: {str(e)}")
            self.root.after(0, self.update_ui_after_process_exit)
    
    def analyze_log_line(self, line):
        """分析日志行，检测连接状态变化"""
        try:
            # 忽略特定的频繁日志
            if "数据通道缓冲区低" in line:
                return  # 不处理这类消息
                
            # 检测连接成功的指标
            if "连接状态变更: connected" in line:
                self.root.after(0, lambda: self.update_connection_status("已连接"))
                self.log_message("检测到连接已建立！")
            elif "ICE连接状态: connected" in line or "ICE连接状态: completed" in line:
                self.root.after(0, lambda: self.update_connection_status("ICE连接已建立"))
            elif "数据通道已打开" in line:
                self.root.after(0, lambda: self.update_connection_status("数据通道已打开"))
            elif "P2P连接已建立" in line:
                self.root.after(0, lambda: self.update_connection_status("P2P连接已建立"))
            
            # 检测连接失败的指标
            elif "连接状态变更: failed" in line or "ICE连接状态: failed" in line:
                self.root.after(0, lambda: self.update_connection_status("连接失败", error=True))
            elif "连接状态变更: disconnected" in line:
                self.root.after(0, lambda: self.update_connection_status("已断开连接"))
                
            # 检测特定的游戏状态
            elif "识别到登录消息" in line:
                self.root.after(0, lambda: self.update_connection_status("玩家正在登录"))
            elif "已发送重要消息到Minecraft服务器" in line:
                self.root.after(0, lambda: self.update_connection_status("游戏数据传输中"))
        except Exception as e:
            print(f"分析日志行出错: {e}")
    
    def update_connection_status(self, status, error=False):
        """更新连接状态显示"""
        try:
            # 更新状态标签
            color = "red" if error else "green" if status != "连接中" else "black"
            self.status_label.config(text=status, foreground=color)
            
            # 在连接信息区域也更新状态
            self.log_message(f"连接状态更新: {status}")
            
            current_info = self.status_text.get(1.0, tk.END)
            if "连接状态:" in current_info:
                # 替换旧的状态信息
                lines = current_info.split('\n')
                updated_lines = []
                for line in lines:
                    if line.startswith("连接状态:"):
                        updated_lines.append(f"连接状态: {status}")
                    else:
                        updated_lines.append(line)
                
                new_info = '\n'.join(updated_lines)
                self.update_status_text(new_info)
            else:
                # 添加状态信息到末尾
                self.status_text.config(state=tk.NORMAL)
                if self.status_text.get(1.0, tk.END).strip():
                    self.status_text.insert(tk.END, f"\n\n连接状态: {status}")
                else:
                    self.status_text.insert(tk.END, f"连接状态: {status}")
                self.status_text.config(state=tk.DISABLED)
        except Exception as e:
            print(f"更新连接状态显示时出错: {e}")
    
    def update_ui_after_process_exit(self):
        """进程退出后更新UI"""
        self.process = None
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.update_connection_status("已断开连接")
    
    def update_log_display(self):
        """更新日志显示，使用定时器定期调用"""
        if not hasattr(self, 'log_text'):
            # 如果日志文本组件还没有初始化，跳过此次更新
            self.root.after(100, self.update_log_display)
            return
            
        try:
            # 更新来自队列的日志
            messages_to_show = []
            
            # 从队列获取最多100条消息
            for _ in range(100):
                try:
                    record = self.log_queue.get_nowait()
                    message = self.queue_handler.format(record)
                    messages_to_show.append(message)
                except queue.Empty:
                    break
                except Exception as e:
                    print(f"获取日志消息时出错: {e}")
            
            # 添加直接记录的日志
            if self.direct_logs:
                messages_to_show.extend(self.direct_logs)
                self.direct_logs = []
            
            # 如果有消息需要显示，更新文本组件
            if messages_to_show:
                self.log_text.config(state=tk.NORMAL)
                for message in messages_to_show:
                    self.log_text.insert(tk.END, message + "\n")
                self.log_text.see(tk.END)  # 滚动到底部
                self.log_text.config(state=tk.DISABLED)
                print(f"已更新 {len(messages_to_show)} 条日志到GUI")
        except Exception as e:
            print(f"更新日志显示时出错: {e}")
            import traceback
            traceback.print_exc()
        
        # 安排下一次更新
        self.root.after(100, self.update_log_display)
    
    def log_message(self, message):
        """将消息添加到日志"""
        try:
            # 确保消息是字符串
            message = str(message)
            
            # 在控制台输出（调试用）
            print(f"GUI日志: {message}")
            
            # 创建日志记录
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            formatted_message = f"{timestamp} [INFO] {message}"
            
            # 直接添加到待显示列表，确保一定能显示
            self.direct_logs.append(formatted_message)
            
            # 同时也通过标准日志系统记录
            logging.info(message)
        except Exception as e:
            print(f"记录日志消息时出错: {e}")
    
    def on_closing(self):
        """窗口关闭时的处理"""
        try:
            if hasattr(self, 'client') and self.client:
                if messagebox.askokcancel("退出", "连接仍在运行，确定要退出吗？"):
                    # 先尝试正常停止连接
                    self.stop_connection()
                    
                    # 设置运行状态为False
                    self.running = False
                    
                    # 销毁GUI窗口
                    self.root.destroy()
            else:
                self.running = False
                self.root.destroy()
        except Exception as e:
            print(f"关闭窗口时出错: {e}")
            import traceback
            traceback.print_exc()
            # 无论如何尝试销毁窗口
            try:
                self.root.destroy()
            except:
                pass

def main():
    """主函数"""
    root = tk.Tk()
    
    # 创建应用
    app = MinecraftP2PGUI(root)
    
    # 立即添加一条测试日志，验证日志系统
    app.log_message("GUI界面初始化完成，准备就绪...")
    
    # 开始主循环
    root.mainloop()

#################################################
# MinecraftP2PClient类（从client.py导入）
#################################################

try:
    import aiortc
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration
    from aiortc.contrib.signaling import BYE
except ImportError:
    logging.warning("aiortc库未安装，将无法使用WebRTC功能")
    
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    logging.warning("python-dotenv库未安装，将使用默认配置")

# 从环境变量获取配置
TURN_SERVER = os.getenv("TURN_SERVER")
TURN_USERNAME = os.getenv("TURN_USERNAME")
TURN_PASSWORD = os.getenv("TURN_PASSWORD")
USE_SSL = os.getenv("USE_SSL", "false").lower() == "true"

class MinecraftP2PClient:
    def __init__(self, server_url: str, room_id: str, username: str, local_port: int, remote_port: int, role: str = "auto"):
        self.server_url = server_url
        self.room_id = room_id
        self.username = username
        self.local_port = local_port
        self.remote_port = remote_port
        self.role = role  # "server", "client", 或 "auto"
        
        self.websocket = None
        self.client_id = None
        
        # 单连接模式（向后兼容）
        self.peer_connection = None
        self.data_channel = None
        
        # 多连接模式支持 - 完全独立隧道
        self.peer_connections = {}  # client_id -> RTCPeerConnection
        self.data_channels = {}     # client_id -> RTCDataChannel
        self.client_connections = {}  # client_id -> {connection, channel, connected, username}
        self.client_tunnels = {}    # client_id -> {connection, channel, session_id, active}
        
        self.local_server = None
        
        # 多客户端支持 - 每个客户端连接单独管理
        self.minecraft_clients = {}  # client_id -> minecraft客户端连接
        self.minecraft_client = None  # 主客户端连接（向后兼容）
        
        # 为房主角色添加Minecraft服务器连接
        self.minecraft_server_readers = {}  # client_id -> reader
        self.minecraft_server_writers = {}  # client_id -> writer
        self.minecraft_server_reader = None
        self.minecraft_server_writer = None
        self.minecraft_server_connected = False
        self.is_connecting_to_server = False
        self.server_data_tasks = {}  # client_id -> task
        self.server_data_task = None
        
        # 消息队列系统
        self.message_queues = {}   # client_id -> Queue
        self.message_queue = asyncio.Queue()
        self.processor_tasks = {}  # client_id -> task
        self.processor_task = None
        self.is_processing = False
        
        # 添加请求队列，用于存储待处理的重要请求
        self.pending_requests = []
        self.client_pending_requests = {}  # client_id -> List[requests]
        self.login_request_received = False
        
        self.connected_to_peer = False
        self.shutdown_event = asyncio.Event()
        self.peers_in_room = 0
        
        # 新增的连接处理逻辑
        self.last_connection_attempt = 0
        self.connection_attempt_count = 0
        self.max_connection_retry_delay = 5  # 最大重试延迟（秒）
        self.is_session_login = False  # 标记当前会话是否为登录会话
        self.mc_server_host = '127.0.0.1'
        self.mc_server_port = remote_port
        
        # 会话状态跟踪
        self.current_session_id = None
        self.active_sessions = {}  # 会话ID -> 会话状态
        self.session_client_map = {}  # 会话ID -> 客户端ID
        self.client_session_map = {}  # 客户端ID -> 当前会话ID
        
        # 当前活跃的客户端ID
        self.current_peer_id = None
        
        # 每个客户端的本地服务器
        self.client_local_servers = {}  # client_id -> {server, port}
    
    async def create_peer_connection_for_client(self, peer_id):
        """为特定客户端创建独立的对等连接"""
        if peer_id in self.peer_connections:
            logging.info(f"已存在与客户端 {peer_id} 的连接")
            return self.peer_connections[peer_id]
            
        logging.info(f"为客户端 {peer_id} 创建新的WebRTC连接...")
        pc = RTCPeerConnection()
        
        @pc.on("datachannel")
        def on_datachannel(channel):
            logging.info(f"从客户端 {peer_id} 收到数据通道: {channel.label}")
            self.handle_data_channel_for_client(channel, peer_id)
        
        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logging.info(f"客户端 {peer_id} 连接状态变更: {pc.connectionState}")
            if pc.connectionState == "disconnected" or pc.connectionState == "failed":
                if not self.shutdown_event.is_set():
                    logging.info(f"检测到客户端 {peer_id} WebRTC连接已断开，清理资源...")
                    await self.cleanup_client_connection(peer_id)
        
        @pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            logging.info(f"客户端 {peer_id} ICE连接状态: {pc.iceConnectionState}")
            if pc.iceConnectionState == "failed":
                await pc.close()
                logging.error(f"客户端 {peer_id} ICE连接失败")
                # 清理这个客户端的连接
                await self.cleanup_client_connection(peer_id)
            elif pc.iceConnectionState == "connected":
                logging.info(f"与客户端 {peer_id} 的P2P连接已建立")
                # 设置当前活跃客户端
                self.current_peer_id = peer_id
                self.connected_to_peer = True
        
        @pc.on("icecandidate")
        async def on_icecandidate(event):
            if event.candidate:
                candidate_dict = {
                    "candidate": event.candidate.candidate,
                    "sdpMid": event.candidate.sdpMid,
                    "sdpMLineIndex": event.candidate.sdpMLineIndex,
                }
                
                # 准备ICE候选项消息
                ice_message = {
                    "type": "ice_candidate",
                    "candidate": candidate_dict,
                    "target": peer_id  # 指定目标客户端
                }
                
                await self.websocket.send(json.dumps(ice_message))
        
        # 保存连接
        self.peer_connections[peer_id] = pc
        return pc
    
    def handle_data_channel_for_client(self, channel, peer_id):
        """处理特定客户端的数据通道"""
        # 保存数据通道
        self.data_channels[peer_id] = channel
        
        @channel.on("open")
        def on_open():
            logging.info(f"客户端 {peer_id} 的数据通道已打开: {channel.label}")
            # 如果是服务器角色，连接到Minecraft服务器
            if self.role == "server" and not self.minecraft_server_writer:
                logging.info(f"数据通道已打开，作为房主开始连接到Minecraft服务器...")
                asyncio.create_task(self.connect_to_minecraft_server())
        
        @channel.on("close")
        def on_close():
            logging.info(f"客户端 {peer_id} 的数据通道已关闭: {channel.label}")
        
        @channel.on("message")
        async def message_handler(message):
            # 记录当前活跃的客户端ID
            self.current_peer_id = peer_id
            await self.on_data_channel_message(message)
    
    async def cleanup_client_connection(self, peer_id):
        """清理指定客户端的连接资源"""
        if peer_id in self.peer_connections:
            pc = self.peer_connections[peer_id]
            # 关闭连接
            await pc.close()
            # 从字典中移除
            del self.peer_connections[peer_id]
            logging.info(f"已清理客户端 {peer_id} 的WebRTC连接")
        
        if peer_id in self.data_channels:
            # 从字典中移除
            del self.data_channels[peer_id]
            logging.info(f"已清理客户端 {peer_id} 的数据通道")
        
        # 如果当前活跃的是这个客户端，则重置
        if self.current_peer_id == peer_id:
            self.current_peer_id = None
    
    async def connect_to_signaling_server(self):
        """连接到信令服务器"""
        try:
            self.websocket = await websockets.connect(self.server_url)
            
            # 发送加入房间请求
            await self.websocket.send(json.dumps({
                "type": "join",
                "room_id": self.room_id,
                "username": self.username
            }))
            
            # 接收确认消息
            response = await self.websocket.recv()
            data = json.loads(response)
            
            if data["type"] == "joined":
                self.client_id = data["client_id"]
                self.peers_in_room = data["peers"]
                logging.info(f"已连接到信令服务器，房间ID: {self.room_id}, 客户端ID: {self.client_id}")
                logging.info(f"房间内其他用户数: {self.peers_in_room}")
                
                return True
            else:
                logging.error(f"加入房间失败: {data}")
                return False
                
        except Exception as e:
            logging.error(f"连接信令服务器失败: {e}")
            return False
    
    async def setup_peer_connection(self):
        """设置WebRTC对等连接"""
        try:
            # 尝试手动创建RTCConfiguration
            logging.info("创建WebRTC连接...")
            
            # 定义STUN服务器
            stun_servers = [
                "stun:stun.l.google.com:19302",
                "stun:stun1.l.google.com:19302",
            ]
            
            # 创建RTCPeerConnection，使用最小配置
            self.peer_connection = RTCPeerConnection()
            
            @self.peer_connection.on("datachannel")
            def on_datachannel(channel):
                logging.info(f"收到数据通道: {channel.label}")
                if self.role == "server":
                    logging.info("作为服务器角色，准备在接收到数据通道后连接到Minecraft服务器")
                self.handle_data_channel(channel)
            
            @self.peer_connection.on("connectionstatechange")
            async def on_connectionstatechange():
                logging.info(f"连接状态变更: {self.peer_connection.connectionState}")
                if self.peer_connection.connectionState == "disconnected" or self.peer_connection.connectionState == "failed":
                    if not self.shutdown_event.is_set():
                        logging.info("检测到WebRTC连接已断开，清理资源...")
                        asyncio.create_task(self.cleanup_after_client_disconnect())
            
            @self.peer_connection.on("iceconnectionstatechange")
            async def on_iceconnectionstatechange():
                logging.info(f"ICE连接状态: {self.peer_connection.iceConnectionState}")
                if self.peer_connection.iceConnectionState == "failed":
                    await self.peer_connection.close()
                    logging.error("ICE连接失败")
                elif self.peer_connection.iceConnectionState == "connected":
                    logging.info("P2P连接已建立")
                    self.connected_to_peer = True
                    # 如果是服务器角色，连接到Minecraft服务器
                    if self.role == "server" and not self.minecraft_server_writer:
                        logging.info("ICE连接已建立，作为房主将连接到Minecraft服务器...")
                        await self.connect_to_minecraft_server()
            
            @self.peer_connection.on("icecandidate")
            async def on_icecandidate(event):
                if event.candidate:
                    candidate_dict = {
                        "candidate": event.candidate.candidate,
                        "sdpMid": event.candidate.sdpMid,
                        "sdpMLineIndex": event.candidate.sdpMLineIndex,
                    }
                    
                    # 准备ICE候选项消息
                    ice_message = {
                        "type": "ice_candidate",
                        "candidate": candidate_dict
                    }
                    
                    # 如果已知对等方ID，指定目标
                    if hasattr(self, 'peer_id') and self.peer_id:
                        ice_message["target"] = self.peer_id
                    
                    await self.websocket.send(json.dumps(ice_message))
            
            logging.info("WebRTC连接设置完成")
            
        except Exception as e:
            logging.error(f"设置WebRTC连接失败: {str(e)}")
            logging.error(f"错误详情: {type(e).__name__}")
            import traceback
            logging.error(traceback.format_exc())
            self.shutdown_event.set()  # 触发关闭
    
    def handle_data_channel(self, channel):
        """处理接收到的数据通道"""
        self.data_channel = channel
        
        @channel.on("open")
        def on_open():
            logging.info(f"数据通道已打开: {channel.label}")
            # 如果是服务器角色，连接到Minecraft服务器
            if self.role == "server" and not self.minecraft_server_writer:
                logging.info("数据通道已打开，作为房主开始连接到Minecraft服务器...")
                asyncio.create_task(self.connect_to_minecraft_server())
        
        @channel.on("close")
        def on_close():
            logging.info(f"数据通道已关闭: {channel.label}")
            if self.role == "server" and not self.shutdown_event.is_set():
                logging.info("检测到客户端已断开，清理资源...")
                asyncio.create_task(self.cleanup_after_client_disconnect())
        
        @channel.on("message")
        async def message_handler(message):
            await self.on_data_channel_message(message)
    
    async def connect_to_minecraft_server(self):
        """连接到本地Minecraft服务器"""
        # 实现指数退避重连
        now = time.time()
        if now - self.last_connection_attempt < 0.5:  # 防止过于频繁的重连
            self.connection_attempt_count += 1
            delay = min(2 ** (self.connection_attempt_count - 1), self.max_connection_retry_delay)
            logging.info(f"连接尝试过于频繁，等待{delay}秒后重试...")
            await asyncio.sleep(delay)
        else:
            self.connection_attempt_count = 0
            
        self.last_connection_attempt = time.time()
        
        # 如果已经有连接，先关闭它
        if self.minecraft_server_writer and not self.minecraft_server_writer.is_closing():
            try:
                self.minecraft_server_writer.close()
                await self.minecraft_server_writer.wait_closed()
            except Exception as e:
                logging.error(f"关闭旧连接时出错: {e}")
                
        # 如果有正在运行的数据处理任务，取消它
        if self.server_data_task and not self.server_data_task.done():
            logging.info("取消服务器数据处理任务...")
            self.server_data_task.cancel()
            try:
                await self.server_data_task
            except asyncio.CancelledError:
                pass
            self.server_data_task = None
        
        self.minecraft_server_connected = False
        self.minecraft_server_reader = None
        self.minecraft_server_writer = None
        
        try:
            logging.info(f"尝试连接到本地Minecraft服务器({self.mc_server_host}:{self.mc_server_port})...")
            
            # 创建TCP连接
            reader, writer = await asyncio.open_connection(self.mc_server_host, self.mc_server_port)
            
            self.minecraft_server_reader = reader
            self.minecraft_server_writer = writer
            self.minecraft_server_connected = True
            
            logging.info(f"已成功连接到本地Minecraft服务器({self.mc_server_host}:{self.mc_server_port})")
            
            # 记录连接的本地和远程端口信息
            local_addr = writer.get_extra_info('sockname')
            remote_addr = writer.get_extra_info('peername')
            if local_addr and remote_addr:
                logging.info(f"本地端口: {local_addr[1]} -> 服务器端口: {remote_addr[1]}")
            
            # 创建任务读取来自Minecraft服务器的数据
            self.server_data_task = asyncio.create_task(self.process_minecraft_server_data())
            logging.info("已创建Minecraft服务器数据处理任务")
            
            # 处理积压的请求 - 在消息处理器中统一处理
            if self.pending_requests:
                logging.info(f"连接成功后转移积压的请求到消息队列，共{len(self.pending_requests)}个")
                # 将积压请求转移到消息队列
                for req in self.pending_requests[:]:
                    await self.message_queue.put((req, self.current_session_id))
                    self.pending_requests.remove(req)
            
            return True
        except ConnectionRefusedError:
            logging.error(f"连接到Minecraft服务器被拒绝: {self.mc_server_host}:{self.mc_server_port}")
            logging.error("请确保您的Minecraft服务器正在运行，并且端口配置正确")
            return False
        except Exception as e:
            logging.error(f"连接Minecraft服务器失败: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return False

    async def process_minecraft_server_data(self):
        """处理从Minecraft服务器接收的数据 - 多客户端版本"""
        try:
            packet_count = 0
            last_activity = time.time()
            
            if not self.minecraft_server_reader:
                logging.critical("严重错误: 数据处理协程启动时，服务器读取器不存在")
                return
                
            logging.info("开始处理来自Minecraft服务器的数据")
            
            while True:
                if self.shutdown_event.is_set():
                    logging.info("关闭事件已触发，停止服务器数据处理")
                    break
                    
                try:
                    # 读取Minecraft服务器数据
                    data = await asyncio.wait_for(
                        self.minecraft_server_reader.read(4096),
                        timeout=10.0
                    )
                    
                    if not data:
                        logging.warning("服务器连接已关闭（EOF）")
                        break
                    
                    packet_count += 1
                    last_activity = time.time()
                    
                    # 基本分析数据包类型（仅用于日志）
                    packet_type, is_login_related = self.analyze_minecraft_packet(data)
                    is_important = is_login_related or packet_type in ["断开连接", "登录成功", "加密握手"]
                    
                    # 记录重要数据包
                    if is_important:
                        logging.info(f"收到服务器重要数据包: 类型={packet_type}, 长度={len(data)}字节")
                    elif packet_count % 1000 == 0:
                        logging.info(f"已处理 {packet_count} 个服务器数据包")
                    
                    # 获取数据包关联的会话ID和客户端ID
                    target_client_id = None
                    current_session = self.current_session_id
                    
                    # 如果有会话ID，尝试找到对应的客户端
                    if current_session and current_session in self.session_client_map:
                        target_client_id = self.session_client_map[current_session]
                        if is_important:
                            logging.info(f"数据包关联到会话 {current_session[:8]} 和客户端 {target_client_id}")
                    
                    # 如果找不到特定客户端，使用当前活跃客户端
                    if not target_client_id:
                        target_client_id = self.current_peer_id
                    
                    # 多客户端数据分发逻辑
                    if target_client_id:
                        # 1. 如果确定了目标客户端，优先发送给它
                        if target_client_id in self.data_channels:
                            channel = self.data_channels[target_client_id]
                            if channel and channel.readyState == "open":
                                try:
                                    channel.send(bytes(data))
                                    if is_important:
                                        logging.info(f"已将服务器数据包发送给目标客户端 {target_client_id}")
                                    continue  # 发送成功，处理下一个数据包
                                except Exception as e:
                                    logging.error(f"向目标客户端 {target_client_id} 发送数据失败: {e}")
                    
                    # 2. 如果有单一主数据通道（兼容旧客户端），尝试使用它
                    if self.data_channel and self.data_channel.readyState == "open":
                        try:
                            self.data_channel.send(bytes(data))
                            if is_important:
                                logging.info("通过主数据通道发送服务器数据")
                            continue  # 发送成功，处理下一个数据包
                        except Exception as e:
                            logging.error(f"通过主数据通道发送数据失败: {e}")
                    
                    # 3. 如果是广播类型的数据包，或者无法确定目标客户端，尝试发送给所有连接的客户端
                    should_broadcast = packet_type in ["keep_alive", "时间更新", "区块数据", "世界边界"]
                    if should_broadcast or not target_client_id:
                        sent_to_any = False
                        for client_id, channel in self.data_channels.items():
                            if channel and channel.readyState == "open":
                                try:
                                    channel.send(bytes(data))
                                    sent_to_any = True
                                    if is_important:
                                        logging.info(f"已将服务器广播数据包发送给客户端 {client_id}")
                                except Exception as e:
                                    logging.error(f"向客户端 {client_id} 广播数据失败: {e}")
                        
                        if not sent_to_any and is_important:
                            logging.warning("没有可用的数据通道发送服务器数据")
                
                except asyncio.TimeoutError:
                    # 超时只是表示没有读取到数据，不需要特别处理
                    continue
                except asyncio.CancelledError:
                    logging.info("服务器数据处理任务被取消")
                    break
                except ConnectionResetError:
                    logging.error("服务器连接被重置")
                    break
                except Exception as e:
                    logging.error(f"处理服务器数据时出错: {e}")
                    # 如果是严重错误，如连接断开，中断处理
                    if isinstance(e, OSError):
                        logging.error("连接错误，中断服务器数据处理")
                        break
                    # 其他错误，短暂等待后继续
                    await asyncio.sleep(1)
        
        except asyncio.CancelledError:
            logging.info("服务器数据处理协程被取消")
        except Exception as e:
            logging.error(f"服务器数据处理出错: {e}")
            import traceback
            logging.error(traceback.format_exc())
        finally:
            logging.info("服务器数据处理结束")
            # 如果不是因为关闭事件导致的退出，尝试重新连接
            if not self.shutdown_event.is_set():
                logging.info("尝试重新连接Minecraft服务器...")
                asyncio.create_task(self.connect_to_minecraft_server())
    
    async def create_data_channel(self):
        """创建数据通道"""
        try:
            # 确保WebRTC连接已初始化
            if not self.peer_connection:
                logging.error("无法创建数据通道: WebRTC连接未初始化")
                return False
            
            logging.info("创建数据通道...")
            channel_options = {}  # 可以在此设置通道选项
            
            # 使用try/except包装数据通道创建
            try:
                self.data_channel = self.peer_connection.createDataChannel(
                    "minecraft", 
                    ordered=True,  # 有序传输
                    protocol="mc-p2p"  # 自定义协议标识
                )
                
                logging.info(f"数据通道已创建: {self.data_channel.label}")
                
                # 设置事件处理程序
                @self.data_channel.on("open")
                def on_open():
                    logging.info(f"数据通道已打开: {self.data_channel.label}")
                    # 如果是服务器角色，连接到Minecraft服务器
                    if self.role == "server" and not self.minecraft_server_writer:
                        asyncio.create_task(self.connect_to_minecraft_server())
                
                @self.data_channel.on("close")
                def on_close():
                    logging.info(f"数据通道已关闭: {self.data_channel.label}")
                
                @self.data_channel.on("error")
                def on_error(error):
                    logging.error(f"数据通道错误: {error}")
                
                @self.data_channel.on("bufferedamountlow")
                def on_bufferedamountlow():
                    logging.info("数据通道缓冲区低")
                
                @self.data_channel.on("message")
                async def message_handler(message):
                    await self.on_data_channel_message(message)
                
                return True
                
            except Exception as channel_error:
                logging.error(f"创建数据通道时出错: {channel_error}")
                import traceback
                logging.error(traceback.format_exc())
                return False
            
        except Exception as e:
            logging.error(f"创建数据通道步骤失败: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return False

    async def create_offer(self):
        """创建连接提议"""
        try:
            logging.info("开始创建连接提议...")
            
            # 创建数据通道
            channel_created = await self.create_data_channel()
            if not channel_created:
                logging.error("无法创建连接提议: 数据通道创建失败")
                return False
            
            # 等待一小段时间，确保数据通道已正确创建
            await asyncio.sleep(0.5)
            
            # 创建提议
            try:
                offer = await self.peer_connection.createOffer()
                logging.info("已生成提议")
            except Exception as offer_error:
                logging.error(f"创建提议失败: {offer_error}")
                return False
            
            # 设置本地描述
            try:
                await self.peer_connection.setLocalDescription(offer)
                logging.info("已设置本地描述")
            except Exception as local_desc_error:
                logging.error(f"设置本地描述失败: {local_desc_error}")
                return False
            
            # 发送提议到信令服务器
            try:
                # 如果对等方ID已知，则添加到提议中
                offer_message = {
                    "type": "offer",
                    "sdp": self.peer_connection.localDescription.sdp
                }
                
                # 如果已知对等方ID，指定目标
                if hasattr(self, 'peer_id') and self.peer_id:
                    offer_message["target"] = self.peer_id
                
                await self.websocket.send(json.dumps(offer_message))
                logging.info("已发送连接提议")
                return True
            except Exception as send_error:
                logging.error(f"发送提议失败: {send_error}")
                return False
            
        except Exception as e:
            logging.error(f"创建连接提议失败: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return False
    
    async def handle_signaling(self):
        """处理信令消息 - 多客户端版本"""
        try:
            # 存储与当前客户端通信的对等方ID
            self.peer_id = None
            
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    msg_type = data.get("type", "unknown")
                    logging.info(f"收到信令消息: {msg_type}")
                    
                    # 获取发送者和目标ID
                    sender_id = data.get("sender_id")
                    target_id = data.get("target")
                    
                    # 如果消息有目标ID且不是发给自己的，则忽略
                    if target_id and target_id != self.client_id:
                        logging.info(f"忽略发送给其他客户端的消息: target={target_id}")
                        continue
                    
                    # 处理offer消息（客户端接收服务端发来的offer）
                    if msg_type == "offer":
                        try:
                            # 记录发送offer的对等方ID
                            if sender_id:
                                self.peer_id = sender_id
                                logging.info(f"记录对等方ID: {sender_id}")
                            
                            # 设置远程描述
                            offer = RTCSessionDescription(sdp=data["sdp"], type="offer")
                            
                            # 如果是客户端模式，使用单一连接
                            if self.role == "client":
                                await self.peer_connection.setRemoteDescription(offer)
                                
                                # 创建应答
                                answer = await self.peer_connection.createAnswer()
                                await self.peer_connection.setLocalDescription(answer)
                                
                                # 发送应答，指定目标ID（即offer的发送者）
                                response = {
                                    "type": "answer",
                                    "sdp": self.peer_connection.localDescription.sdp
                                }
                                
                                if sender_id:
                                    response["target"] = sender_id
                                
                                await self.websocket.send(json.dumps(response))
                                logging.info(f"已向{sender_id or '服务端'}发送应答")
                            
                        except Exception as e:
                            logging.error(f"处理offer失败: {e}")
                            import traceback
                            logging.error(traceback.format_exc())
                            
                    # 处理answer消息（服务端接收客户端发来的answer）
                    elif msg_type == "answer":
                        try:
                            if not sender_id:
                                logging.error("收到answer但没有sender_id，无法处理")
                                continue
                                
                            # 创建answer对象
                            answer = RTCSessionDescription(sdp=data["sdp"], type="answer")
                            
                            # 在多连接模式下查找正确的连接
                            if sender_id in self.peer_connections:
                                pc = self.peer_connections[sender_id]
                                
                                if pc and pc.signalingState == "have-local-offer":
                                    await pc.setRemoteDescription(answer)
                                    logging.info(f"已为客户端 {sender_id} 设置远程描述")
                                else:
                                    logging.error(f"无法设置远程描述，当前状态: {pc.signalingState if pc else 'None'}")
                            
                            # 兼容旧模式
                            elif self.peer_connection and self.peer_connection.signalingState == "have-local-offer":
                                await self.peer_connection.setRemoteDescription(answer)
                                logging.info("使用主连接设置远程描述")
                            else:
                                logging.error("找不到对应的连接来处理answer")
                                
                        except Exception as e:
                            logging.error(f"处理answer失败: {e}")
                            import traceback
                            logging.error(traceback.format_exc())
                            
                    # 处理ICE候选项
                    elif msg_type == "ice_candidate":
                        try:
                            if not data.get("candidate"):
                                continue
                                
                            # 创建候选项对象
                            candidate = RTCIceCandidate(
                                candidate=data["candidate"]["candidate"],
                                sdpMid=data["candidate"]["sdpMid"],
                                sdpMLineIndex=data["candidate"]["sdpMLineIndex"]
                            )
                            
                            # 在多连接模式下查找正确的连接
                            if sender_id and sender_id in self.peer_connections:
                                pc = self.peer_connections[sender_id]
                                await pc.addIceCandidate(candidate)
                                logging.info(f"已为客户端 {sender_id} 添加ICE候选项")
                            
                            # 兼容旧模式
                            elif self.peer_connection:
                                await self.peer_connection.addIceCandidate(candidate)
                                logging.info("使用主连接添加ICE候选项")
                            else:
                                logging.error("找不到对应的连接来添加ICE候选项")
                                
                        except Exception as e:
                            logging.error(f"处理ICE候选项失败: {e}")
                            import traceback
                            logging.error(traceback.format_exc())
                            
                    # 处理用户加入
                    elif msg_type == "user_joined":
                        client_id = data.get('client_id', '未知')
                        username = data.get('username', '未知用户')
                        logging.info(f"用户 {username} (ID: {client_id}) 加入房间")
                        self.peers_in_room += 1
                        
                        # 如果是服务器角色，为新用户创建连接
                        if client_id != '未知' and client_id != self.client_id and self.role == "server":
                            asyncio.create_task(self.handle_new_peer(client_id, username))
                    
                    # 处理用户离开
                    elif msg_type == "user_left":
                        client_id = data.get('client_id', '未知')
                        logging.info(f"用户 {data.get('username', '未知')} (ID: {client_id}) 离开房间")
                        self.peers_in_room -= 1
                        
                        # 清理客户端连接
                        if client_id in self.client_connections:
                            try:
                                client_info = self.client_connections[client_id]
                                if client_info["connection"]:
                                    await client_info["connection"].close()
                                del self.client_connections[client_id]
                                
                                if client_id in self.peer_connections:
                                    del self.peer_connections[client_id]
                                    
                                if client_id in self.data_channels:
                                    del self.data_channels[client_id]
                                
                                # 清理Minecraft客户端连接
                                if client_id in self.minecraft_clients:
                                    mc_client = self.minecraft_clients[client_id]
                                    if not mc_client.is_closing():
                                        mc_client.close()
                                    del self.minecraft_clients[client_id]
                                
                                logging.info(f"已清理客户端 {client_id} 的连接")
                                
                                # 清理会话映射
                                for session_id, mapped_client in list(self.session_client_map.items()):
                                    if mapped_client == client_id:
                                        del self.session_client_map[session_id]
                                        logging.info(f"已清理会话 {session_id[:8]} 的客户端关联")
                                
                                # 清理客户端-会话映射
                                if client_id in self.client_session_map:
                                    del self.client_session_map[client_id]
                                
                            except Exception as e:
                                logging.error(f"清理客户端 {client_id} 连接时出错: {e}")
                    
                except json.JSONDecodeError:
                    logging.error(f"无法解析信令消息: {message}")
                except Exception as e:
                    logging.error(f"处理信令消息出错: {e}")
                    import traceback
                    logging.error(traceback.format_exc())
                
        except websockets.exceptions.ConnectionClosed:
            logging.info("信令服务器连接已关闭")
        except Exception as e:
            logging.error(f"信令处理循环出错: {e}")
            import traceback
            logging.error(traceback.format_exc())
    
    async def start_local_server(self):
        """启动本地服务器，接收Minecraft客户端连接"""
        try:
            # 尝试不同的端口，如果默认端口被占用
            port_to_try = self.local_port
            max_attempts = 5
            attempt = 0
            
            while attempt < max_attempts:
                try:
                    server = await asyncio.start_server(
                        self.handle_minecraft_client,
                        "127.0.0.1",
                        port_to_try
                    )
                    
                    self.local_server = server
                    self.local_port = port_to_try  # 更新实际使用的端口
                    logging.info(f"本地服务器已启动在 127.0.0.1:{port_to_try}")
                    
                    # 作为客户端，提示用户如何连接
                    if self.role == "client":
                        logging.info(f"Minecraft客户端可以通过连接 127.0.0.1:{port_to_try} 加入游戏")
                    
                    async with server:
                        await server.serve_forever()
                    break
                except OSError as e:
                    if e.errno == 10013 or e.errno == 10048:  # 权限不足或端口已被占用
                        attempt += 1
                        port_to_try = self.local_port + attempt
                        logging.warning(f"端口 {self.local_port + attempt - 1} 不可用，尝试端口 {port_to_try}")
                    else:
                        raise  # 如果是其他错误，则抛出
            
            if attempt >= max_attempts:
                logging.error(f"无法绑定本地端口，已尝试 {max_attempts} 次")
                raise OSError(f"无法绑定本地端口，已尝试 {max_attempts} 次")
        except Exception as e:
            logging.error(f"启动本地服务器失败: {e}")
            self.shutdown_event.set()  # 触发关闭

    async def handle_minecraft_client(self, reader, writer):
        """处理Minecraft客户端连接 - 多客户端版本"""
        client_address = writer.get_extra_info('peername')
        logging.info(f"Minecraft客户端已连接: {client_address}")
        
        # 为此连接生成一个唯一标识符
        connection_id = str(uuid.uuid4())[:8]
        logging.info(f"为Minecraft客户端分配连接ID: {connection_id}")
        
        # 存储客户端连接 - 优先与当前活跃的WebRTC客户端关联
        if self.current_peer_id:
            self.minecraft_clients[self.current_peer_id] = writer
            logging.info(f"存储为客户端 {self.current_peer_id} 的Minecraft客户端连接")
        else:
            # 如果没有当前活跃的对等点，使用主客户端变量（向后兼容）
            self.minecraft_client = writer
            logging.info(f"存储为主Minecraft客户端连接")
        
        # 记录此连接使用的客户端ID，便于追踪
        connection_client_id = self.current_peer_id
        
        # 从Minecraft客户端读取数据并通过WebRTC发送
        try:
            packet_count = 0
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                
                packet_count += 1
                
                # 分析数据包类型
                packet_type, is_login_attempt = self.analyze_minecraft_packet(data)
                is_important = is_login_attempt or packet_type in ["握手包-状态查询", "断开连接", "登录开始"]
                
                # 打印客户端发送的数据
                try:
                    if is_important:
                        data_hex = data[:20].hex() if len(data) > 0 else "空数据"
                        peer_info = f"(客户端ID: {connection_client_id})" if connection_client_id else ""
                        logging.info(f"从Minecraft客户端{peer_info}接收重要数据包#{packet_count}: 长度={len(data)}字节, 类型={packet_type}")
                    else:
                        # 每1000个数据包打印一次
                        if packet_count % 1000 == 0:
                            logging.info(f"已接收{packet_count}个客户端数据包")
                except Exception as e:
                    logging.error(f"打印客户端数据包信息时出错: {e}")
                
                # 获取应该使用的客户端ID - 优先使用建立连接时的ID
                target_client_id = connection_client_id if connection_client_id else self.current_peer_id
                
                # 1. 如果有明确的目标客户端，先尝试发送给它
                if target_client_id and target_client_id in self.data_channels:
                    channel = self.data_channels[target_client_id]
                    if channel and channel.readyState == "open":
                        try:
                            channel.send(bytes(data))
                            if is_important:
                                logging.info(f"已将Minecraft客户端重要数据包(类型={packet_type})发送给WebRTC客户端 {target_client_id}")
                            continue  # 发送成功，跳过后续步骤
                        except Exception as e:
                            logging.error(f"向WebRTC客户端 {target_client_id} 发送数据时出错: {e}")
                
                # 2. 如果没有特定客户端或发送失败，尝试使用主数据通道
                if self.data_channel and self.data_channel.readyState == "open":
                    try:
                        self.data_channel.send(bytes(data))
                        if is_important:
                            logging.info(f"已将Minecraft客户端重要数据包(类型={packet_type})通过主数据通道发送")
                        continue  # 发送成功，跳过后续步骤
                    except Exception as e:
                        logging.error(f"通过主数据通道发送数据时出错: {e}")
                
                # 3. 如果前两步都失败，尝试广播给所有连接的客户端
                sent = False
                for client_id, channel in self.data_channels.items():
                    if channel and channel.readyState == "open":
                        try:
                            channel.send(bytes(data))
                            sent = True
                            if is_important:
                                logging.info(f"已将Minecraft客户端重要数据包(类型={packet_type})广播给WebRTC客户端 {client_id}")
                            break  # 只发送给一个客户端即可
                        except Exception as e:
                            logging.error(f"向WebRTC客户端 {client_id} 广播数据时出错: {e}")
                
                # 如果没有任何数据通道可用，记录警告
                if not sent and is_important:
                    logging.warning("没有可用的WebRTC数据通道发送Minecraft客户端数据")
        except Exception as e:
            logging.error(f"处理Minecraft客户端数据时出错: {e}")
            import traceback
            logging.error(traceback.format_exc())
        finally:
            writer.close()
            logging.info(f"Minecraft客户端已断开连接: {client_address}")
            
            # 清理连接记录
            if self.minecraft_client == writer:
                self.minecraft_client = None
                logging.info("已清理主Minecraft客户端连接")
            
            # 查找并清理客户端连接记录
            for pid, mc_client in list(self.minecraft_clients.items()):
                if mc_client == writer:
                    del self.minecraft_clients[pid]
                    logging.info(f"已清理客户端 {pid} 的Minecraft客户端连接")

    async def wait_for_peer(self, timeout=60):
        """等待对等方加入房间"""
        start_time = time.time()
        while self.peers_in_room == 0:
            await asyncio.sleep(1)
            if time.time() - start_time > timeout:
                logging.warning(f"等待对等方超时（{timeout}秒）")
                return False
            if self.shutdown_event.is_set():
                return False
        logging.info("对等方已加入房间，准备建立连接")
        return True
        
    # 更精确地检测Minecraft协议包类型
    def analyze_minecraft_packet(self, data):
        """分析Minecraft协议包类型"""
        if not data or len(data) < 1:
            return "未知", False
            
        packet_type = "未知"
        is_login_related = False
        
        try:
            first_byte = data[0]
            
            # 握手包
            if first_byte == 0x10:
                if len(data) > 3:
                    next_state = data[3]
                    if next_state == 2:
                        packet_type = "握手包-登录意图"
                        is_login_related = True
                    elif next_state == 1:
                        packet_type = "握手包-状态查询"
                else:
                    packet_type = "握手包-未完整"
            
            # 登录开始包
            elif first_byte == 0x00 and len(data) > 1:
                packet_type = "登录开始"
                is_login_related = True
                
            # 服务器列表ping
            elif first_byte == 0x01:
                if len(data) > 8:  # ping包通常包含8字节时间戳
                    packet_type = "服务器ping"
                else:
                    # 较短的0x01可能是登录相关数据包
                    packet_type = "加密响应或登录确认"
                    is_login_related = True
                    
            # 加密响应
            elif first_byte == 0xAC or first_byte == 0xAD:
                packet_type = "加密握手"
                is_login_related = True
                
            # 服务器响应
            elif first_byte == 0x02:
                packet_type = "登录成功"
                is_login_related = True
                
            # 断开连接
            elif first_byte == 0xFF:
                packet_type = "断开连接"
                
        except Exception as e:
            logging.error(f"分析Minecraft数据包时出错: {e}")
            
        return packet_type, is_login_related
    
    async def on_data_channel_message(self, message):
        """处理从数据通道接收到的消息 - 多客户端版本"""
        try:
            if isinstance(message, str):
                message = message.encode('utf-8')
            
            # 使用客户端ID创建会话ID，确保每个客户端的会话独立
            current_client_id = self.current_peer_id
            
            # 查找客户端当前的会话ID
            session_id = None
            if current_client_id and current_client_id in self.client_session_map:
                session_id = self.client_session_map[current_client_id]
            
            # 如果没有找到或没有分配，则创建新会话
            if not session_id:
                session_id = str(uuid.uuid4())
                if current_client_id:
                    self.client_session_map[current_client_id] = session_id
                self.current_session_id = session_id
            
            # 分析数据包类型，仅用于日志记录
            packet_type, is_login_attempt = self.analyze_minecraft_packet(message)
            
            # 只记录重要的数据包
            is_important = is_login_attempt or packet_type in ["握手包-状态查询", "断开连接"]
            
            if is_important:
                data_hex = message[:20].hex() if len(message) > 0 else "空数据"
                logging.info(f"收到重要数据包: 类型={packet_type}, 会话ID={session_id[:8]}")
            
            # 创建新会话
            if packet_type == "握手包-登录意图" or packet_type == "登录请求":
                session_id = str(uuid.uuid4())
                self.current_session_id = session_id
                logging.info(f"创建新登录会话: {session_id[:8]}")
                # 记录关联的客户端ID
                if current_client_id:
                    self.session_client_map[session_id] = current_client_id
                    self.client_session_map[current_client_id] = session_id
                    logging.info(f"会话 {session_id[:8]} 关联到客户端 {current_client_id}")
            elif packet_type == "握手包-状态查询":
                session_id = str(uuid.uuid4())
                self.current_session_id = session_id
                logging.info(f"创建新状态查询会话: {session_id[:8]}")
                # 记录关联的客户端ID
                if current_client_id:
                    self.session_client_map[session_id] = current_client_id
                    self.client_session_map[current_client_id] = session_id
            
            # 将消息放入队列，以便按序处理
            await self.message_queue.put((message, session_id))
            
        except Exception as e:
            logging.error(f"处理数据通道消息时出错: {e}")
            import traceback
            logging.error(traceback.format_exc())
    
    async def cleanup_after_client_disconnect(self):
        """在客户端断开连接后清理资源"""
        logging.info("客户端断开连接，开始清理资源...")
        
        # 如果服务器角色，可以选择保持Minecraft服务器连接
        # 或者关闭它，这取决于你的需求
        if self.role == "server" and self.minecraft_server_writer and not self.minecraft_server_writer.is_closing():
            # 这里选择关闭到Minecraft服务器的连接
            logging.info("关闭到Minecraft服务器的连接...")
            try:
                self.minecraft_server_writer.close()
                await self.minecraft_server_writer.wait_closed()
                self.minecraft_server_writer = None
                self.minecraft_server_reader = None
                self.minecraft_server_connected = False
            except Exception as e:
                logging.error(f"关闭Minecraft服务器连接时出错: {e}")
        
        # 取消服务器数据处理任务
        if self.server_data_task and not self.server_data_task.done():
            logging.info("取消服务器数据处理任务...")
            self.server_data_task.cancel()
            try:
                await self.server_data_task
            except asyncio.CancelledError:
                pass
            self.server_data_task = None
        
        # 重置状态
        self.current_session_id = None
        self.active_sessions = {}
        self.is_session_login = False
        self.login_request_received = False
        self.pending_requests = []
        
        logging.info("客户端断开连接后资源清理完成")

    async def process_message_queue(self):
        """处理消息队列，确保消息按顺序处理"""
        try:
            logging.info("消息队列处理器已启动")
            
            while self.is_processing:
                try:
                    # 获取队列中的下一个消息
                    message, session_id = await self.message_queue.get()
                    
                    # 处理消息
                    await self.process_minecraft_message(message, session_id)
                    
                    # 标记任务完成
                    self.message_queue.task_done()
                except asyncio.CancelledError:
                    logging.info("消息队列处理器被取消")
                    break
                except Exception as e:
                    logging.error(f"处理消息队列时出错: {e}")
                    import traceback
                    logging.error(traceback.format_exc())
                    # 继续处理下一个消息
                    await asyncio.sleep(0.1)
                    
        except asyncio.CancelledError:
            logging.info("消息队列处理器被取消")
            raise
        except Exception as e:
            logging.error(f"消息队列处理器崩溃: {e}")
            import traceback
            logging.error(traceback.format_exc())
        finally:
            logging.info("消息队列处理器已停止")

    async def process_minecraft_message(self, message, session_id):
        """处理单个Minecraft消息 - 多客户端版本"""
        try:
            if not message:
                return
                
            # 分析数据包类型（仅用于日志）
            packet_type, is_login_attempt = self.analyze_minecraft_packet(message)
            is_important = is_login_attempt or packet_type in ["握手包-状态查询", "断开连接", "登录开始"]
            
            if is_important:
                logging.info(f"处理重要客户端消息: 类型={packet_type}, 长度={len(message)}字节")
            
            # 获取该会话关联的客户端ID
            target_client_id = None
            if session_id in self.session_client_map:
                target_client_id = self.session_client_map[session_id]
                
            # 如果未找到关联客户端，使用当前活跃客户端
            if not target_client_id:
                target_client_id = self.current_peer_id
            
            # 记录关联的客户端ID到会话
            if session_id and target_client_id:
                self.session_client_map[session_id] = target_client_id
                self.client_session_map[target_client_id] = session_id
                
                if is_important:
                    logging.info(f"关联会话 {session_id[:8]} 到客户端 {target_client_id}")
                
            # 不同角色的处理逻辑
            if self.role == "server":
                # 服务端将消息转发给Minecraft服务器
                
                # 确保与Minecraft服务器连接
                if not self.minecraft_server_writer or self.minecraft_server_writer.is_closing():
                    logging.info("Minecraft服务器连接未建立，尝试连接...")
                    if not await self.connect_to_minecraft_server():
                        logging.error("连接到Minecraft服务器失败，无法处理消息")
                        return
                
                try:
                    # 发送到Minecraft服务器
                    self.minecraft_server_writer.write(message)
                    await self.minecraft_server_writer.drain()
                    
                    if is_important:
                        logging.info(f"已将重要客户端消息(类型={packet_type})发送到Minecraft服务器")
                        
                except ConnectionError as e:
                    logging.error(f"发送数据到Minecraft服务器时连接错误: {e}")
                    # 尝试重连
                    if await self.connect_to_minecraft_server():
                        try:
                            self.minecraft_server_writer.write(message)
                            await self.minecraft_server_writer.drain()
                            if is_important:
                                logging.info(f"重连后成功发送重要消息")
                        except Exception as retry_error:
                            logging.error(f"重连后发送失败: {retry_error}")
                except Exception as e:
                    logging.error(f"发送数据到Minecraft服务器时错误: {e}")
            
            elif self.role == "client":
                # 客户端将消息转发给Minecraft客户端
                # 查找关联的客户端连接
                minecraft_client_writer = None
                
                # 1. 首先查看该会话ID关联的客户端是否有连接
                if target_client_id and target_client_id in self.minecraft_clients:
                    minecraft_client_writer = self.minecraft_clients[target_client_id]
                
                # 2. 如果没有找到，检查主连接
                if not minecraft_client_writer and self.minecraft_client:
                    minecraft_client_writer = self.minecraft_client
                
                # 发送数据
                if minecraft_client_writer and not minecraft_client_writer.is_closing():
                    try:
                        minecraft_client_writer.write(message)
                        if is_important:
                            logging.info(f"已将重要服务器消息(类型={packet_type})发送到Minecraft客户端")
                    except Exception as e:
                        logging.error(f"发送数据到Minecraft客户端时错误: {e}")
                else:
                    logging.error("Minecraft客户端连接不可用")
                    # 对每种情况提供更详细信息以便调试
                    if not minecraft_client_writer:
                        if target_client_id:
                            logging.error(f"客户端ID {target_client_id} 没有关联的Minecraft客户端连接")
                        else:
                            logging.error("没有活跃的客户端ID")
                    else:
                        logging.error("Minecraft客户端连接已关闭")
        
        except Exception as e:
            logging.error(f"处理Minecraft消息时出错: {e}")
            import traceback
            logging.error(traceback.format_exc())
    
    async def run(self):
        """运行客户端 - 多客户端支持版本"""
        # 连接到信令服务器
        if not await self.connect_to_signaling_server():
            return
        
        # 设置WebRTC连接
        await self.setup_peer_connection()
        
        # 处理信令消息任务
        signaling_task = asyncio.create_task(self.handle_signaling())
        
        # 启动消息处理器
        self.is_processing = True
        self.processor_task = asyncio.create_task(self.process_message_queue())
        
        # 如果是服务器角色，尝试提前连接到Minecraft服务器（仅用于向后兼容）
        if self.role == "server":
            logging.info("作为房主角色，为旧版客户端准备Minecraft服务器连接...")
            try:
                await self.connect_to_minecraft_server()
            except Exception as e:
                logging.warning(f"提前连接Minecraft服务器失败: {e}，将在连接建立后重试")
        
        # 只有客户端角色才需要启动本地服务器作为代理
        local_server_task = None
        if self.role != "server":
            # 客户端角色启动本地服务器作为代理
            logging.info("作为客户端角色，启动本地代理服务器")
            local_server_task = asyncio.create_task(self.start_local_server())
        else:
            logging.info("作为服务器角色，不需要启动本地代理服务器")
            logging.info("请确保您的Minecraft服务器已经在运行，端口为: " + str(self.remote_port))
        
        try:
            # 根据角色决定是否创建提议
            if self.role == "server":
                logging.info("作为服务器角色等待客户端连接...")
                # 服务器角色不主动发起连接，等待客户端连接
                await self.shutdown_event.wait()
            elif self.role == "client":
                logging.info("作为客户端角色等待服务端发起连接...")
                # 客户端角色等待服务端的offer
                await self.shutdown_event.wait()
            else:  # auto
                logging.info("自动模式：等待确定角色...")
                # 如果房间为空，成为第一个加入的人，默认为服务端角色
                if self.peers_in_room == 0:
                    logging.info("房间内没有其他用户，等待其他用户加入...")
                    self.role = "server"
                    logging.info("自动切换为服务器角色")
                    await self.shutdown_event.wait()
                else:
                    logging.info("房间内已有其他用户，默认作为客户端等待连接...")
                    self.role = "client"
                    logging.info("自动切换为客户端角色")
                    await self.shutdown_event.wait()
            
        except Exception as e:
            logging.error(f"运行时出错: {e}")
            import traceback
            logging.error(traceback.format_exc())
        finally:
            # 清理资源
            await self.cleanup_resources()
            
            # 取消信令任务
            if signaling_task:
                signaling_task.cancel()
                try:
                    await signaling_task
                except asyncio.CancelledError:
                    pass
            
            # 取消本地服务器任务
            if local_server_task:
                local_server_task.cancel()
                try:
                    await local_server_task
                except asyncio.CancelledError:
                    pass

    async def cleanup_resources(self):
        """清理所有资源"""
        logging.info("开始清理所有资源...")
        
        # 停止消息处理
        self.is_processing = False
        
        # 取消主消息处理任务
        if self.processor_task and not self.processor_task.done():
            self.processor_task.cancel()
            try:
                await self.processor_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logging.error(f"取消主消息处理任务时出错: {e}")
        
        # 取消所有客户端消息处理任务
        for client_id, task in list(self.processor_tasks.items()):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logging.error(f"取消客户端 {client_id} 的消息处理任务时出错: {e}")
        
        # 取消主服务器数据任务
        if self.server_data_task and not self.server_data_task.done():
            self.server_data_task.cancel()
            try:
                await self.server_data_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logging.error(f"取消主服务器数据任务时出错: {e}")
        
        # 取消所有客户端服务器数据任务
        for client_id, task in list(self.server_data_tasks.items()):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logging.error(f"取消客户端 {client_id} 的服务器数据任务时出错: {e}")
        
        # 关闭主数据通道（旧模式）
        if self.data_channel:
            if self.data_channel.readyState == "open":
                try:
                    self.data_channel.close()
                    logging.info("已关闭主数据通道")
                except Exception as e:
                    logging.error(f"关闭主数据通道时出错: {e}")
            self.data_channel = None
        
        # 关闭所有客户端数据通道
        for client_id, channel in list(self.data_channels.items()):
            if channel and channel.readyState == "open":
                try:
                    channel.close()
                    logging.info(f"已关闭客户端 {client_id} 的数据通道")
                except Exception as e:
                    logging.error(f"关闭客户端 {client_id} 的数据通道时出错: {e}")
        self.data_channels.clear()
        
        # 关闭主WebRTC连接（旧模式）
        if self.peer_connection:
            try:
                await self.peer_connection.close()
                logging.info("已关闭主WebRTC连接")
            except Exception as e:
                logging.error(f"关闭主WebRTC连接时出错: {e}")
            self.peer_connection = None
        
        # 关闭所有客户端WebRTC连接
        for client_id, pc in list(self.peer_connections.items()):
            if pc:
                try:
                    await pc.close()
                    logging.info(f"已关闭客户端 {client_id} 的WebRTC连接")
                except Exception as e:
                    logging.error(f"关闭客户端 {client_id} 的WebRTC连接时出错: {e}")
        self.peer_connections.clear()
        
        # 关闭主Minecraft服务器连接（旧模式）
        if self.minecraft_server_writer and not self.minecraft_server_writer.is_closing():
            try:
                self.minecraft_server_writer.close()
                await self.minecraft_server_writer.wait_closed()
                logging.info("已关闭主Minecraft服务器连接")
            except Exception as e:
                logging.error(f"关闭主Minecraft服务器连接时出错: {e}")
            self.minecraft_server_writer = None
            self.minecraft_server_reader = None
        
        # 关闭所有客户端Minecraft服务器连接
        for client_id, writer in list(self.minecraft_server_writers.items()):
            if writer and not writer.is_closing():
                try:
                    writer.close()
                    await writer.wait_closed()
                    logging.info(f"已关闭客户端 {client_id} 的Minecraft服务器连接")
                except Exception as e:
                    logging.error(f"关闭客户端 {client_id} 的Minecraft服务器连接时出错: {e}")
        self.minecraft_server_writers.clear()
        self.minecraft_server_readers.clear()
        
        # 关闭主Minecraft客户端连接（旧模式）
        if self.minecraft_client:
            if not self.minecraft_client.is_closing():
                try:
                    self.minecraft_client.close()
                    logging.info("已关闭主Minecraft客户端连接")
                except Exception as e:
                    logging.error(f"关闭主Minecraft客户端连接时出错: {e}")
            self.minecraft_client = None
        
        # 关闭所有客户端Minecraft客户端连接
        for client_id, writer in list(self.minecraft_clients.items()):
            if writer and not writer.is_closing():
                try:
                    writer.close()
                    logging.info(f"已关闭客户端 {client_id} 的Minecraft客户端连接")
                except Exception as e:
                    logging.error(f"关闭客户端 {client_id} 的Minecraft客户端连接时出错: {e}")
        self.minecraft_clients.clear()
        
        # 关闭本地服务器
        if self.local_server:
            self.local_server.close()
            try:
                await self.local_server.wait_closed()
                logging.info("已关闭本地服务器")
            except Exception as e:
                logging.error(f"关闭本地服务器时出错: {e}")
            self.local_server = None
        
        # 关闭WebSocket连接
        if self.websocket:
            try:
                await self.websocket.close()
                logging.info("已关闭WebSocket连接")
            except Exception as e:
                logging.error(f"关闭WebSocket连接时出错: {e}")
            self.websocket = None
        
        # 清理数据结构
        self.client_connections.clear()
        self.client_tunnels.clear()
        self.session_client_map.clear()
        self.client_session_map.clear()
        self.message_queues.clear()
        self.client_pending_requests.clear()
        self.processor_tasks.clear()
        self.server_data_tasks.clear()
        
        # 重置状态变量
        self.current_session_id = None
        self.current_peer_id = None
        self.active_sessions.clear()
        self.is_session_login = False
        self.login_request_received = False
        self.pending_requests.clear()
        self.minecraft_server_connected = False
        self.is_connecting_to_server = False
        
        logging.info("所有资源清理完成")

    async def shutdown(self):
        """关闭客户端 - 多客户端版本"""
        logging.info("正在关闭客户端...")
        # 设置关闭标志
        self.shutdown_event.set()
        # 清理所有资源
        await self.cleanup_resources()
        logging.info("客户端关闭完成")

    async def handle_new_peer(self, peer_id, username):
        """处理新的对等方连接 - 完全独立隧道版本"""
        # 只有服务器角色需要主动创建连接
        if self.role != "server":
            logging.info(f"客户端角色：接收到用户 {username} (ID: {peer_id}) 加入通知")
            return
            
        logging.info(f"服务器角色：检测到新客户端 {username} (ID: {peer_id}) 加入，创建新连接")
        
        # 检查是否已有此客户端的连接
        if peer_id in self.client_connections:
            logging.info(f"已存在与客户端 {peer_id} 的连接，跳过创建")
            return
            
        try:
            # 为此客户端创建新的连接对象和独立隧道
            client_info = {
                "id": peer_id,
                "username": username,
                "connection": None,
                "channel": None,
                "connected": False,
                "connection_time": time.time(),
                "message_queue": asyncio.Queue(),
                "session_id": str(uuid.uuid4())
            }
            
            # 创建独立的消息队列
            self.message_queues[peer_id] = asyncio.Queue()
            
            # 创建独立的请求队列
            self.client_pending_requests[peer_id] = []
            
            # 创建RTCPeerConnection
            pc = RTCPeerConnection()
            client_info["connection"] = pc
            self.peer_connections[peer_id] = pc
            
            # 连接状态变化处理
            @pc.on("connectionstatechange")
            async def on_connectionstatechange():
                state = pc.connectionState
                logging.info(f"客户端 {peer_id} 连接状态变更: {state}")
                
                if state == "connected":
                    client_info["connected"] = True
                    # 更新活跃客户端，但不独占
                    self.current_peer_id = peer_id
                    logging.info(f"与客户端 {peer_id} 建立连接成功")
                    
                    # 为此客户端创建独立的Minecraft服务器连接
                    asyncio.create_task(self.connect_to_minecraft_server_for_client(peer_id))
                    
                    # 启动独立的消息处理任务
                    self.processor_tasks[peer_id] = asyncio.create_task(
                        self.process_message_queue_for_client(peer_id)
                    )
                    
                elif state == "disconnected" or state == "failed":
                    client_info["connected"] = False
                    if peer_id in self.client_connections:
                        logging.info(f"客户端 {peer_id} 连接已断开，清理资源")
                        # 清理独立隧道资源
                        await self.cleanup_client_resources(peer_id)
                        
                        # 如果当前活跃客户端是这个，则重置
                        if self.current_peer_id == peer_id:
                            self.current_peer_id = None
                            # 尝试将活跃客户端设为任何其他连接的客户端
                            if self.client_connections:
                                self.current_peer_id = next(iter(self.client_connections))
            
            # ICE连接状态变化处理
            @pc.on("iceconnectionstatechange")
            async def on_iceconnectionstatechange():
                logging.info(f"客户端 {peer_id} ICE连接状态: {pc.iceConnectionState}")
                if pc.iceConnectionState == "connected" or pc.iceConnectionState == "completed":
                    logging.info(f"客户端 {peer_id} 的ICE连接已建立")
            
            # ICE候选处理
            @pc.on("icecandidate")
            async def on_icecandidate(event):
                if event.candidate:
                    candidate_dict = {
                        "candidate": event.candidate.candidate,
                        "sdpMid": event.candidate.sdpMid,
                        "sdpMLineIndex": event.candidate.sdpMLineIndex,
                    }
                    
                    ice_message = {
                        "type": "ice_candidate",
                        "candidate": candidate_dict,
                        "target": peer_id  # 指定目标客户端
                    }
                    
                    await self.websocket.send(json.dumps(ice_message))
            
            # 创建数据通道
            channel = pc.createDataChannel("minecraft", ordered=True)
            client_info["channel"] = channel
            self.data_channels[peer_id] = channel
            
            # 数据通道事件处理
            @channel.on("open")
            def on_open():
                logging.info(f"客户端 {peer_id} 的数据通道已打开")
                client_info["connected"] = True
                
                # 创建独立的会话ID
                session_id = str(uuid.uuid4())
                self.client_session_map[peer_id] = session_id
                self.session_client_map[session_id] = peer_id
                logging.info(f"为客户端 {peer_id} 创建独立会话: {session_id[:8]}")
            
            @channel.on("close")
            def on_close():
                logging.info(f"客户端 {peer_id} 的数据通道已关闭")
                client_info["connected"] = False
            
            @channel.on("message")
            async def on_message(message):
                # 记录当前客户端ID
                old_peer_id = self.current_peer_id
                self.current_peer_id = peer_id
                
                # 处理来自客户端的消息，使用独立的处理逻辑
                try:
                    await self.handle_client_message(peer_id, message)
                finally:
                    # 恢复之前的客户端ID
                    self.current_peer_id = old_peer_id
            
            # 创建offer
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            
            # 发送offer给目标客户端
            offer_message = {
                "type": "offer",
                "sdp": pc.localDescription.sdp,
                "target": peer_id
            }
            
            await self.websocket.send(json.dumps(offer_message))
            logging.info(f"已向客户端 {peer_id} 发送连接请求")
            
            # 保存客户端连接信息
            self.client_connections[peer_id] = client_info
            
            # 创建独立隧道记录
            self.client_tunnels[peer_id] = {
                "connection": pc,
                "channel": channel,
                "session_id": client_info["session_id"],
                "active": True
            }
            
        except Exception as e:
            logging.error(f"创建与客户端 {peer_id} 的连接时出错: {e}")
            import traceback
            logging.error(traceback.format_exc())

    async def cleanup_client_resources(self, client_id):
        """清理指定客户端的所有资源"""
        logging.info(f"开始清理客户端 {client_id} 的所有资源")
        
        # 1. 清理WebRTC连接
        if client_id in self.peer_connections:
            try:
                await self.peer_connections[client_id].close()
                del self.peer_connections[client_id]
                logging.info(f"已关闭客户端 {client_id} 的WebRTC连接")
            except Exception as e:
                logging.error(f"关闭客户端 {client_id} 的WebRTC连接时出错: {e}")
        
        # 2. 清理数据通道
        if client_id in self.data_channels:
            del self.data_channels[client_id]
            logging.info(f"已清理客户端 {client_id} 的数据通道")
        
        # 3. 清理客户端连接记录
        if client_id in self.client_connections:
            del self.client_connections[client_id]
            logging.info(f"已清理客户端 {client_id} 的连接记录")
        
        # 4. 清理独立隧道记录
        if client_id in self.client_tunnels:
            del self.client_tunnels[client_id]
            logging.info(f"已清理客户端 {client_id} 的隧道记录")
        
        # 5. 清理Minecraft客户端连接
        if client_id in self.minecraft_clients:
            try:
                writer = self.minecraft_clients[client_id]
                if not writer.is_closing():
                    writer.close()
                del self.minecraft_clients[client_id]
                logging.info(f"已清理客户端 {client_id} 的Minecraft客户端连接")
            except Exception as e:
                logging.error(f"关闭客户端 {client_id} 的Minecraft客户端连接时出错: {e}")
        
        # 6. 清理Minecraft服务器连接
        if client_id in self.minecraft_server_writers:
            try:
                writer = self.minecraft_server_writers[client_id]
                if not writer.is_closing():
                    writer.close()
                    await writer.wait_closed()
                del self.minecraft_server_writers[client_id]
                if client_id in self.minecraft_server_readers:
                    del self.minecraft_server_readers[client_id]
                logging.info(f"已清理客户端 {client_id} 的Minecraft服务器连接")
            except Exception as e:
                logging.error(f"关闭客户端 {client_id} 的Minecraft服务器连接时出错: {e}")
        
        # 7. 取消数据处理任务
        if client_id in self.server_data_tasks:
            task = self.server_data_tasks[client_id]
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logging.error(f"取消客户端 {client_id} 的数据处理任务时出错: {e}")
            del self.server_data_tasks[client_id]
            logging.info(f"已取消客户端 {client_id} 的数据处理任务")
        
        # 8. 取消消息处理任务
        if client_id in self.processor_tasks:
            task = self.processor_tasks[client_id]
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logging.error(f"取消客户端 {client_id} 的消息处理任务时出错: {e}")
            del self.processor_tasks[client_id]
            logging.info(f"已取消客户端 {client_id} 的消息处理任务")
        
        # 9. 清理消息队列
        if client_id in self.message_queues:
            del self.message_queues[client_id]
            logging.info(f"已清理客户端 {client_id} 的消息队列")
        
        # 10. 清理待处理请求
        if client_id in self.client_pending_requests:
            del self.client_pending_requests[client_id]
            logging.info(f"已清理客户端 {client_id} 的待处理请求")
        
        # 11. 清理会话映射
        session_to_remove = []
        for session_id, mapped_client in self.session_client_map.items():
            if mapped_client == client_id:
                session_to_remove.append(session_id)
        
        for session_id in session_to_remove:
            del self.session_client_map[session_id]
            logging.info(f"已清理会话 {session_id[:8]} 的客户端关联")
        
        # 12. 清理客户端会话映射
        if client_id in self.client_session_map:
            del self.client_session_map[client_id]
            logging.info(f"已清理客户端 {client_id} 的会话映射")
        
        logging.info(f"客户端 {client_id} 的所有资源清理完成")

    async def connect_to_minecraft_server_for_client(self, client_id):
        """为特定客户端创建独立的Minecraft服务器连接"""
        # 如果已经有连接，先关闭它
        if client_id in self.minecraft_server_writers and self.minecraft_server_writers[client_id]:
            if not self.minecraft_server_writers[client_id].is_closing():
                try:
                    self.minecraft_server_writers[client_id].close()
                    await self.minecraft_server_writers[client_id].wait_closed()
                except Exception as e:
                    logging.error(f"为客户端 {client_id} 关闭旧的Minecraft服务器连接时出错: {e}")

        # 如果有正在运行的数据处理任务，取消它
        if client_id in self.server_data_tasks and self.server_data_tasks[client_id]:
            if not self.server_data_tasks[client_id].done():
                self.server_data_tasks[client_id].cancel()
                try:
                    await self.server_data_tasks[client_id]
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logging.error(f"为客户端 {client_id} 取消数据处理任务时出错: {e}")

        try:
            logging.info(f"尝试为客户端 {client_id} 连接到Minecraft服务器({self.mc_server_host}:{self.mc_server_port})...")
            
            # 创建TCP连接
            reader, writer = await asyncio.open_connection(self.mc_server_host, self.mc_server_port)
            
            # 存储连接
            self.minecraft_server_readers[client_id] = reader
            self.minecraft_server_writers[client_id] = writer
            
            logging.info(f"已成功为客户端 {client_id} 连接到Minecraft服务器({self.mc_server_host}:{self.mc_server_port})")
            
            # 记录连接的本地和远程端口信息
            local_addr = writer.get_extra_info('sockname')
            remote_addr = writer.get_extra_info('peername')
            if local_addr and remote_addr:
                logging.info(f"客户端 {client_id} 的连接: 本地端口: {local_addr[1]} -> 服务器端口: {remote_addr[1]}")
            
            # 创建任务读取来自Minecraft服务器的数据
            self.server_data_tasks[client_id] = asyncio.create_task(
                self.process_minecraft_server_data_for_client(client_id)
            )
            logging.info(f"已为客户端 {client_id} 创建Minecraft服务器数据处理任务")
            
            # 处理积压的请求
            if client_id in self.client_pending_requests and self.client_pending_requests[client_id]:
                logging.info(f"处理客户端 {client_id} 的积压请求，共{len(self.client_pending_requests[client_id])}个")
                # 获取客户端的会话ID
                session_id = self.client_session_map.get(client_id)
                if session_id:
                    # 将积压请求转移到消息队列
                    for req in self.client_pending_requests[client_id][:]:
                        await self.message_queues[client_id].put((req, session_id))
                        self.client_pending_requests[client_id].remove(req)
                else:
                    logging.warning(f"客户端 {client_id} 没有关联的会话ID，无法处理积压请求")
            
            return True
        except ConnectionRefusedError:
            logging.error(f"客户端 {client_id} 连接到Minecraft服务器被拒绝: {self.mc_server_host}:{self.mc_server_port}")
            logging.error("请确保您的Minecraft服务器正在运行，并且端口配置正确")
            return False
        except Exception as e:
            logging.error(f"为客户端 {client_id} 连接Minecraft服务器失败: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return False

    async def process_minecraft_server_data_for_client(self, client_id):
        """为特定客户端处理Minecraft服务器数据流"""
        try:
            packet_count = 0
            reader = self.minecraft_server_readers.get(client_id)
            
            if not reader:
                logging.error(f"客户端 {client_id} 的Minecraft服务器读取器不存在")
                return
                
            logging.info(f"开始为客户端 {client_id} 处理来自Minecraft服务器的数据")
            
            while True:
                if self.shutdown_event.is_set():
                    logging.info(f"关闭事件已触发，停止客户端 {client_id} 的服务器数据处理")
                    break
                
                try:
                    # 读取Minecraft服务器数据
                    data = await asyncio.wait_for(reader.read(4096), timeout=10.0)
                    
                    if not data:
                        logging.warning(f"客户端 {client_id} 的服务器连接已关闭（EOF）")
                        break
                    
                    packet_count += 1
                    
                    # 分析数据包类型（仅用于日志）
                    packet_type, is_login_related = self.analyze_minecraft_packet(data)
                    is_important = is_login_related or packet_type in ["断开连接", "登录成功", "加密握手"]
                    
                    # 记录重要数据包
                    if is_important:
                        logging.info(f"为客户端 {client_id} 接收到服务器重要数据包: 类型={packet_type}, 长度={len(data)}字节")
                    elif packet_count % 1000 == 0:
                        logging.info(f"客户端 {client_id} 已处理 {packet_count} 个服务器数据包")
                    
                    # 发送数据到指定客户端的数据通道
                    if client_id in self.data_channels:
                        channel = self.data_channels[client_id]
                        if channel and channel.readyState == "open":
                            try:
                                channel.send(bytes(data))
                                if is_important:
                                    logging.info(f"已将服务器重要数据包发送给客户端 {client_id}")
                            except Exception as e:
                                logging.error(f"向客户端 {client_id} 发送数据失败: {e}")
                        else:
                            logging.warning(f"客户端 {client_id} 的数据通道已关闭或无效")
                    else:
                        logging.warning(f"找不到客户端 {client_id} 的数据通道")
                
                except asyncio.TimeoutError:
                    # 超时只是表示没有读取到数据，继续等待
                    continue
                except asyncio.CancelledError:
                    logging.info(f"客户端 {client_id} 的服务器数据处理任务被取消")
                    break
                except ConnectionResetError:
                    logging.error(f"客户端 {client_id} 的服务器连接被重置")
                    break
                except Exception as e:
                    logging.error(f"处理客户端 {client_id} 的服务器数据时出错: {e}")
                    # 如果是严重错误，中断处理
                    if isinstance(e, OSError):
                        logging.error(f"客户端 {client_id} 连接错误，中断服务器数据处理")
                        break
                    # 其他错误，短暂等待后继续
                    await asyncio.sleep(1)
        
        except asyncio.CancelledError:
            logging.info(f"客户端 {client_id} 的服务器数据处理协程被取消")
        except Exception as e:
            logging.error(f"客户端 {client_id} 的服务器数据处理出错: {e}")
            import traceback
            logging.error(traceback.format_exc())
        finally:
            logging.info(f"客户端 {client_id} 的服务器数据处理结束")
            # 如果不是因为关闭事件导致的退出，尝试重新连接
            if not self.shutdown_event.is_set():
                logging.info(f"尝试为客户端 {client_id} 重新连接Minecraft服务器...")
                asyncio.create_task(self.connect_to_minecraft_server_for_client(client_id))

    async def handle_client_message(self, client_id, message):
        """处理来自特定客户端的消息"""
        try:
            if isinstance(message, str):
                message = message.encode('utf-8')
            
            # 获取客户端的会话ID
            session_id = None
            if client_id in self.client_session_map:
                session_id = self.client_session_map[client_id]
            
            # 如果没有会话ID，创建一个新的
            if not session_id:
                session_id = str(uuid.uuid4())
                self.client_session_map[client_id] = session_id
                self.session_client_map[session_id] = client_id
                logging.info(f"为客户端 {client_id} 创建新会话: {session_id[:8]}")
            
            # 分析数据包类型
            packet_type, is_login_attempt = self.analyze_minecraft_packet(message)
            
            # 记录重要数据包
            is_important = is_login_attempt or packet_type in ["握手包-状态查询", "断开连接"]
            
            if is_important:
                logging.info(f"收到客户端 {client_id} 的重要数据包: 类型={packet_type}, 会话ID={session_id[:8]}")
            
            # 创建新的登录会话
            if packet_type == "握手包-登录意图" or packet_type == "登录请求":
                new_session_id = str(uuid.uuid4())
                self.client_session_map[client_id] = new_session_id
                self.session_client_map[new_session_id] = client_id
                session_id = new_session_id
                logging.info(f"为客户端 {client_id} 创建新登录会话: {session_id[:8]}")
            
            # 将消息添加到客户端的消息队列
            if client_id in self.message_queues:
                await self.message_queues[client_id].put((message, session_id))
            else:
                # 如果没有为此客户端创建消息队列，创建一个
                self.message_queues[client_id] = asyncio.Queue()
                await self.message_queues[client_id].put((message, session_id))
                
                # 启动消息处理任务
                if client_id not in self.processor_tasks or self.processor_tasks[client_id].done():
                    self.processor_tasks[client_id] = asyncio.create_task(
                        self.process_message_queue_for_client(client_id)
                    )
        
        except Exception as e:
            logging.error(f"处理客户端 {client_id} 的消息时出错: {e}")
            import traceback
            logging.error(traceback.format_exc())

    async def process_message_queue_for_client(self, client_id):
        """为特定客户端处理消息队列"""
        try:
            logging.info(f"客户端 {client_id} 的消息队列处理器已启动")
            
            # 确保有消息队列
            if client_id not in self.message_queues:
                self.message_queues[client_id] = asyncio.Queue()
            
            message_queue = self.message_queues[client_id]
            
            while not self.shutdown_event.is_set():
                try:
                    # 获取队列中的下一个消息
                    message, session_id = await message_queue.get()
                    
                    # 处理消息
                    await self.process_minecraft_message_for_client(client_id, message, session_id)
                    
                    # 标记任务完成
                    message_queue.task_done()
                except asyncio.CancelledError:
                    logging.info(f"客户端 {client_id} 的消息队列处理器被取消")
                    break
                except Exception as e:
                    logging.error(f"处理客户端 {client_id} 的消息队列时出错: {e}")
                    # 继续处理下一个消息
                    await asyncio.sleep(0.1)
        
        except asyncio.CancelledError:
            logging.info(f"客户端 {client_id} 的消息队列处理器被取消")
        except Exception as e:
            logging.error(f"客户端 {client_id} 的消息队列处理器崩溃: {e}")
            import traceback
            logging.error(traceback.format_exc())
        finally:
            logging.info(f"客户端 {client_id} 的消息队列处理器已停止")

    async def process_minecraft_message_for_client(self, client_id, message, session_id):
        """处理特定客户端的Minecraft消息"""
        try:
            if not message:
                return
                
            # 分析数据包类型（仅用于日志）
            packet_type, is_login_attempt = self.analyze_minecraft_packet(message)
            is_important = is_login_attempt or packet_type in ["握手包-状态查询", "断开连接", "登录开始"]
            
            if is_important:
                logging.info(f"处理客户端 {client_id} 的重要消息: 类型={packet_type}, 长度={len(message)}字节")
                
            # 对服务端角色，将消息转发给Minecraft服务器
            if self.role == "server":
                # 确保有此客户端的Minecraft服务器连接
                if client_id not in self.minecraft_server_writers or not self.minecraft_server_writers[client_id]:
                    logging.info(f"客户端 {client_id} 的Minecraft服务器连接未建立，尝试连接...")
                    
                    # 如果没有连接，将消息添加到待处理队列
                    if client_id not in self.client_pending_requests:
                        self.client_pending_requests[client_id] = []
                    self.client_pending_requests[client_id].append(message)
                    
                    # 开始连接
                    connected = await self.connect_to_minecraft_server_for_client(client_id)
                    
                    if not connected:
                        logging.error(f"为客户端 {client_id} 连接到Minecraft服务器失败，消息将在连接建立后处理")
                        return
                
                # 确保连接仍然有效
                writer = self.minecraft_server_writers.get(client_id)
                if not writer or writer.is_closing():
                    logging.info(f"客户端 {client_id} 的Minecraft服务器连接已关闭，尝试重新连接...")
                    
                    # 将消息添加到待处理队列
                    if client_id not in self.client_pending_requests:
                        self.client_pending_requests[client_id] = []
                    self.client_pending_requests[client_id].append(message)
                    
                    # 重新连接
                    connected = await self.connect_to_minecraft_server_for_client(client_id)
                    
                    if not connected:
                        logging.error(f"为客户端 {client_id} 重新连接到Minecraft服务器失败，消息将在连接建立后处理")
                        return
                    
                    # 更新writer引用
                    writer = self.minecraft_server_writers.get(client_id)
                
                # 发送消息到Minecraft服务器
                try:
                    writer.write(message)
                    await writer.drain()
                    
                    if is_important:
                        logging.info(f"已将客户端 {client_id} 的重要消息(类型={packet_type})发送到Minecraft服务器")
                except Exception as e:
                    logging.error(f"为客户端 {client_id} 发送数据到Minecraft服务器时出错: {e}")
                    
                    # 如果是连接错误，尝试重新连接
                    if isinstance(e, ConnectionError) or isinstance(e, OSError):
                        logging.info(f"客户端 {client_id} 的Minecraft服务器连接错误，尝试重新连接...")
                        
                        # 将消息添加到待处理队列
                        if client_id not in self.client_pending_requests:
                            self.client_pending_requests[client_id] = []
                        self.client_pending_requests[client_id].append(message)
                        
                        # 重新连接
                        await self.connect_to_minecraft_server_for_client(client_id)
            
            # 对客户端角色，将消息转发给Minecraft客户端
            elif self.role == "client":
                # 查找为此客户端分配的Minecraft客户端连接
                minecraft_client_writer = None
                
                # 首先查看该客户端是否有专用连接
                if client_id in self.minecraft_clients:
                    minecraft_client_writer = self.minecraft_clients[client_id]
                
                # 如果没有找到，使用主连接（向后兼容）
                if not minecraft_client_writer and self.minecraft_client:
                    minecraft_client_writer = self.minecraft_client
                
                # 发送数据
                if minecraft_client_writer and not minecraft_client_writer.is_closing():
                    try:
                        minecraft_client_writer.write(message)
                        if is_important:
                            logging.info(f"已将服务器重要消息(类型={packet_type})发送到客户端 {client_id} 的Minecraft客户端")
                    except Exception as e:
                        logging.error(f"发送数据到客户端 {client_id} 的Minecraft客户端时错误: {e}")
                else:
                    logging.error(f"客户端 {client_id} 的Minecraft客户端连接不可用")
        
        except Exception as e:
            logging.error(f"处理客户端 {client_id} 的Minecraft消息时出错: {e}")
            import traceback
            logging.error(traceback.format_exc())

if __name__ == "__main__":
    main() 
