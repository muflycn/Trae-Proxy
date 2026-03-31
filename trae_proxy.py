#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, request, Response, jsonify, stream_with_context, send_from_directory
import requests
import json
import ssl
import argparse
import logging
import os
import sys
import yaml
from datetime import datetime
from collections import deque
import threading
import time

# 默认配置
TARGET_API_BASE_URL = "https://api.openai.com"
CUSTOM_MODEL_ID = "gpt-4"
TARGET_MODEL_ID = "gpt-4"
STREAM_MODE = None  # None: 不修改，'true': 强制开启，'false': 强制关闭
DEBUG_MODE = False

# 证书文件路径
CERT_FILE = os.path.join("ca", "api.openai.com.crt")
KEY_FILE = os.path.join("ca", "api.openai.com.key")

# 多后端配置
MULTI_BACKEND_CONFIG = None

# 初始化 Flask 应用
app = Flask(__name__)

# ==================== 监控数据统计 ====================

# 线程锁
stats_lock = threading.Lock()

# 请求统计
stats = {
    'total_requests': 0,
    'success_count': 0,
    'error_count': 0,
    'response_times': [],  # 响应时间列表（毫秒）
    'requests_by_minute': [0] * 60,  # 最近 60 分钟的请求数
    'current_minute_index': 0,
    'last_minute_update': datetime.now().minute,
}

# 后端统计
backend_stats = {}

# 最近请求记录（最多保留 100 条）
recent_requests = deque(maxlen=100)

# 日志记录（最多保留 200 条）
log_buffer = deque(maxlen=200)

# 当前正在处理的请求数
processing_count = 0


class MonitorHandler(logging.Handler):
    """自定义日志处理器，将日志记录到缓冲区"""

    def emit(self, record):
        try:
            log_entry = {
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'level': record.levelname,
                'message': self.format(record)
            }
            with stats_lock:
                log_buffer.append(log_entry)
        except Exception:
            pass


# 添加自定义日志处理器
monitor_handler = MonitorHandler()
monitor_handler.setLevel(logging.INFO)
monitor_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(monitor_handler)


def update_minute_stats():
    """更新分钟统计"""
    current_minute = datetime.now().minute
    with stats_lock:
        if current_minute != stats['last_minute_update']:
            # 移动分钟索引
            stats['current_minute_index'] = (stats['current_minute_index'] + 1) % 60
            stats['requests_by_minute'][stats['current_minute_index']] = 0
            stats['last_minute_update'] = current_minute


def record_request(backend_name, model, success, response_time_ms):
    """记录请求统计"""
    global processing_count

    with stats_lock:
        # 更新分钟统计
        update_minute_stats()
        stats['requests_by_minute'][stats['current_minute_index']] += 1
        stats['total_requests'] += 1

        if success:
            stats['success_count'] += 1
        else:
            stats['error_count'] += 1

        # 记录响应时间
        stats['response_times'].append(response_time_ms)
        # 只保留最近 1000 个响应时间
        if len(stats['response_times']) > 1000:
            stats['response_times'] = stats['response_times'][-1000:]

        # 更新后端统计
        if backend_name:
            if backend_name not in backend_stats:
                backend_stats[backend_name] = {'request_count': 0, 'success_count': 0, 'error_count': 0}
            backend_stats[backend_name]['request_count'] += 1
            if success:
                backend_stats[backend_name]['success_count'] += 1
            else:
                backend_stats[backend_name]['error_count'] += 1

        # 记录最近请求
        recent_requests.append({
            'timestamp': datetime.now().isoformat(),
            'model': model,
            'backend': backend_name,
            'success': success,
            'response_time': response_time_ms
        })

        processing_count = max(0, processing_count - 1)


def increment_processing():
    """增加处理中计数"""
    global processing_count
    with stats_lock:
        processing_count += 1


# ==================== API 路由 ====================

@app.route('/dashboard')
def dashboard():
    """监控 Dashboard 页面"""
    return send_from_directory('.', 'dashboard.html')


@app.route('/api/monitor/stats')
def monitor_stats():
    """获取监控统计数据"""
    with stats_lock:
        # 计算响应时间统计
        response_times = stats['response_times']
        if response_times:
            avg_time = sum(response_times) / len(response_times)
            min_time = min(response_times)
            max_time = max(response_times)
        else:
            avg_time = min_time = max_time = None

        # 构建后端列表
        backends = []
        if MULTI_BACKEND_CONFIG:
            for api in MULTI_BACKEND_CONFIG.get('apis', []):
                name = api.get('name', '')
                backend_info = {
                    'name': name,
                    'endpoint': api.get('endpoint', ''),
                    'active': api.get('active', False),
                    'request_count': backend_stats.get(name, {}).get('request_count', 0)
                }
                backends.append(backend_info)

        # 构建最近请求列表（倒序，最新的在前）
        recent_list = list(recent_requests)[-20:]
        recent_list.reverse()

        # 获取日志（倒序，最新的在前）
        logs_list = list(log_buffer)[-50:]
        logs_list.reverse()

        return jsonify({
            'total_requests': stats['total_requests'],
            'success_count': stats['success_count'],
            'error_count': stats['error_count'],
            'avg_response_time': avg_time,
            'min_response_time': min_time,
            'max_response_time': max_time,
            'requests_by_minute': list(stats['requests_by_minute']),
            'backends': backends,
            'recent_requests': recent_list,
            'logs': logs_list,
            'processing_count': processing_count
        })


@app.route('/', methods=['GET'])
def root():
    """处理根路径请求"""
    return jsonify({
        "message": "Welcome to the OpenAI API! Documentation is available at https://platform.openai.com/docs/api-reference",
        "dashboard": "/dashboard"
    })


@app.route('/v1', methods=['GET'])
def v1_root():
    """处理/v1 路径请求"""
    return jsonify({
        "message": "OpenAI API v1 endpoint",
        "endpoints": {
            "chat/completions": "/v1/chat/completions"
        }
    })


@app.route('/v1/models', methods=['GET'])
def list_models():
    """列出可用模型"""
    try:
        # 从配置中获取模型列表
        models = []
        if MULTI_BACKEND_CONFIG:
            apis = MULTI_BACKEND_CONFIG.get('apis', [])
            for api in apis:
                if api.get('active', False):
                    models.append({
                        "id": api.get('custom_model_id', ''),
                        "object": "model",
                        "created": 1,
                        "owned_by": "trae-proxy"
                    })
        else:
            models.append({
                "id": CUSTOM_MODEL_ID,
                "object": "model",
                "created": 1,
                "owned_by": "trae-proxy"
            })

        return jsonify({
            "object": "list",
            "data": models
        })
    except Exception as e:
        logger.error(f"列出模型时发生错误：{str(e)}")
        return jsonify({"error": f"内部服务器错误：{str(e)}"}), 500


def debug_log(message):
    """调试日志记录"""
    if DEBUG_MODE:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with open("debug_request.log", "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
        logger.debug(message)


def load_multi_backend_config():
    """加载多后端配置"""
    global MULTI_BACKEND_CONFIG
    try:
        config_file = "config.yaml"
        if os.path.exists(config_file):
            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
                MULTI_BACKEND_CONFIG = config
                logger.info(f"已加载多后端配置，共 {len(config.get('apis', []))} 个 API 配置")
                return True
        else:
            logger.warning("配置文件不存在，使用单后端模式")
            return False
    except Exception as e:
        logger.error(f"加载多后端配置失败：{str(e)}")
        return False


def select_backend_by_model(requested_model):
    """根据请求的模型选择后端 API"""
    if not MULTI_BACKEND_CONFIG:
        return None

    apis = MULTI_BACKEND_CONFIG.get('apis', [])

    # 首先尝试根据模型 ID 精确匹配
    for api in apis:
        if api.get('active', False) and api.get('custom_model_id') == requested_model:
            logger.info(f"根据模型 ID 匹配到后端：{api['name']} -> {api['endpoint']}")
            return api

    # 如果没有精确匹配，使用第一个激活的 API
    for api in apis:
        if api.get('active', False):
            logger.info(f"使用默认激活后端：{api['name']} -> {api['endpoint']}")
            return api

    # 如果都没有激活的，使用第一个
    if apis:
        logger.warning(f"没有激活的 API 配置，使用第一个：{apis[0]['name']}")
        return apis[0]

    return None


def generate_stream(response):
    """生成流式响应"""
    for chunk in response.iter_content(chunk_size=None):
        yield chunk


def simulate_stream(response_json):
    """将非流式响应模拟为流式响应"""
    # 提取完整响应中的内容
    try:
        content = response_json["choices"][0]["message"]["content"]

        # 模拟流式响应格式
        yield b'data: {"id":"chatcmpl-simulated","object":"chat.completion.chunk","created":1,"model":"' + CUSTOM_MODEL_ID.encode() + b'","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'

        # 将内容分成多个块
        for i in range(0, len(content), 4):
            chunk = content[i:i+4]
            yield f'data: {{"id":"chatcmpl-simulated","object":"chat.completion.chunk","created":1,"model":"{CUSTOM_MODEL_ID}","choices":[{{"index":0,"delta":{{"content":"{chunk}"}},"finish_reason":null}}]}}\n\n'.encode()

        # 发送完成标记
        yield b'data: {"id":"chatcmpl-simulated","object":"chat.completion.chunk","created":1,"model":"' + CUSTOM_MODEL_ID.encode() + b'","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b'data: [DONE]\n\n'
    except Exception as e:
        logger.error(f"模拟流式响应失败：{e}")
        yield f'data: {{"error": "模拟流式响应失败：{str(e)}"}}\n\n'.encode()