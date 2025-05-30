#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import os
import ssl
import uuid
from typing import Dict, List, Optional

import websockets
from dotenv import load_dotenv

load_dotenv()

# 设置日志级别，可通过环境变量控制
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logging.info(f"日志级别设置为: {LOG_LEVEL}")

HOST = os.getenv("SERVER_HOST", "0.0.0.0")
PORT = int(os.getenv("SERVER_PORT", "8080"))
SSL_CERT = os.getenv("SSL_CERT")
SSL_KEY = os.getenv("SSL_KEY")

class SignalingServer:
    def __init__(self):
        self.rooms = {}
        self.clients = {}
        
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
        
        # 获取房间内现有的其他客户端信息
        existing_peers = []
        for cid, info in self.clients.items():
            if info["room_id"] == room_id and cid != client_id:
                existing_peers.append({
                    "client_id": cid,
                    "username": info["username"]
                })
        
        # 通知房间内其他客户端有新用户加入
        if len(self.rooms[room_id]) > 1:
            for client in self.rooms[room_id]:
                if client != websocket:
                    await client.send(json.dumps({
                        "type": "user_joined",
                        "username": username,
                        "client_id": client_id
                    }))
        
        return client_id, existing_peers
    
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
    
    async def get_room_clients(self, room_id: str):
        """获取房间内所有客户端的信息"""
        if room_id not in self.rooms:
            return []
        
        room_clients = []
        for client_id, info in self.clients.items():
            if info["room_id"] == room_id:
                room_clients.append({
                    "client_id": client_id,
                    "username": info["username"]
                })
        
        return room_clients
    
    async def relay_message(self, websocket, message: dict):
        """转发消息到房间内其他客户端"""
        # 查找发送者所在房间
        room_id = None
        sender_id = None
        
        for client_id, info in self.clients.items():
            if info["websocket"] == websocket:
                room_id = info["room_id"]
                sender_id = client_id
                break
        
        if room_id:
            # 添加发送者ID
            message["sender_id"] = sender_id
            
            # 处理特殊消息类型
            if message["type"] == "get_room_clients":
                # 获取房间内所有客户端信息
                room_clients = await self.get_room_clients(room_id)
                # 发送回请求者
                await websocket.send(json.dumps({
                    "type": "room_clients",
                    "clients": room_clients
                }))
                logging.info(f"已发送房间 {room_id} 的客户端列表给 {sender_id}")
                return
            
            # 检查是否有指定的目标客户端ID
            target_id = message.get("target_id")
            
            if target_id and target_id in self.clients:
                # 如果有指定目标，只发送给目标客户端
                target_websocket = self.clients[target_id]["websocket"]
                await target_websocket.send(json.dumps(message))
                logging.debug(f"信令消息从 {sender_id} 发送到特定目标 {target_id}")
            else:
                # 否则，转发消息给房间内所有其他客户端
            for client in self.rooms[room_id]:
                if client != websocket:
                    await client.send(json.dumps(message))
                logging.debug(f"信令消息从 {sender_id} 广播到房间 {room_id} 的所有其他客户端")

signaling_server = SignalingServer()

async def handler(websocket):
    """处理WebSocket连接"""
    try:
        # 等待客户端发送加入房间消息
        message = await websocket.recv()
        data = json.loads(message)
        
        if data["type"] == "join":
            room_id = data["room_id"]
            username = data["username"]
            
            # 注册客户端
            client_id, existing_peers = await signaling_server.register(websocket, room_id, username)
            
            # 发送确认消息
            await websocket.send(json.dumps({
                "type": "joined",
                "room_id": room_id,
                "client_id": client_id,
                "peers": len(signaling_server.rooms[room_id]) - 1,
                "existing_peers": existing_peers
            }))
            
            logging.info(f"客户端 {username} (ID: {client_id}) 加入房间 {room_id}")
            logging.info(f"房间 {room_id} 现有 {len(signaling_server.rooms[room_id])} 个客户端")
            if existing_peers:
                logging.info(f"房间内现有客户端: {', '.join([p['username'] for p in existing_peers])}")
            else:
                logging.info("房间内没有其他客户端")
            
            # 处理客户端消息
            async for message in websocket:
                data = json.loads(message)
                
                # 转发WebRTC信令
                if data["type"] in ["offer", "answer", "ice_candidate"]:
                    # 记录信令类型和目标（如果有）
                    target_id = data.get("target_id")
                    if target_id:
                        logging.debug(f"收到 {data['type']} 信令，目标客户端: {target_id}")
                    else:
                        logging.debug(f"收到 {data['type']} 信令，广播到房间")
                    
                    await signaling_server.relay_message(websocket, data)
                
                # 处理获取房间客户端列表请求
                elif data["type"] == "get_room_clients":
                    logging.info(f"收到获取房间客户端列表请求")
                    await signaling_server.relay_message(websocket, data)
        
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        logging.error(f"错误: {e}")
        import traceback
        logging.error(traceback.format_exc())
    finally:
        await signaling_server.unregister(websocket)

async def process_request(path, headers):
    """处理HTTP请求，可用于CORS支持等"""
    return None

async def main():
    """启动WebSocket服务器"""
    if SSL_CERT and SSL_KEY:
        # 配置SSL
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(SSL_CERT, SSL_KEY)
    else:
        ssl_context = None
    
    async with websockets.serve(handler, HOST, PORT, ssl=ssl_context, process_request=process_request):
        logging.info(f"信令服务器运行在 {'wss' if ssl_context else 'ws'}://{HOST}:{PORT}")
        await asyncio.Future()  # 运行直到被中断

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("服务器已停止") 