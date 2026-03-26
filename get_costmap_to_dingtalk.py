#!/usr/bin/env python3
"""
获取设备cost_map并发送到钉钉
设备编号: 14003BU3
"""
import requests
import json
import base64
from PIL import Image
import numpy as np
import io
import sys
import os
from datetime import datetime

# ============ 配置区域 ============
API_BASE = "http://localhost:8080/xz_sc50/fae"
LOGIN_URL = "http://localhost:8080/xz_robot_common/user/login"
DEVICE_SN = "14003BU3"
OUTPUT_PATH = f"./cost_map_{DEVICE_SN}.png"

# ============ 核心函数 ============

def get_token():
    """获取登录token"""
    params = {
        "username": "admin",
        "password": "123456",
        "clientType": "WEB"
    }
    try:
        response = requests.get(LOGIN_URL, params=params, timeout=10)
        result = response.json()
        if result.get("code") == 200:
            token = result.get("data", {}).get("token")
            print(f"✓ 登录成功，token: {token[:20]}...")
            return token
        else:
            print(f"✗ 登录失败: {result.get('msg', result)}")
            return None
    except Exception as e:
        print(f"✗ 登录异常: {e}")
        return None

def get_cost_map(sn, token):
    """获取设备的cost_map数据"""
    headers = {
        'Content-Type': 'application/json',
        'token': token
    }
    url = f"{API_BASE}/cost_map"
    data = {"sn": sn}
    
    try:
        print(f"正在获取设备 {sn} 的cost_map...")
        response = requests.post(url, headers=headers, json=data, timeout=15)
        result = response.json()
        
        # 保存原始响应用于调试
        with open(f"./cost_map_raw_{sn}.json", "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        
        return result
    except Exception as e:
        print(f"✗ 获取cost_map异常: {e}")
        return None

def parse_cost_map_to_image(cost_map_data, sn):
    """将cost_map数据转换为图片"""
    if not cost_map_data:
        print("✗ 没有cost_map数据")
        return None
    
    if cost_map_data.get("code") != 200:
        print(f"✗ API返回错误: {cost_map_data.get('msg', 'Unknown error')}")
        return None
    
    data = cost_map_data.get("data", {})
    
    if not data:
        print("✗ data字段为空")
        return None
    
    print(f"解析cost_map数据，字段: {list(data.keys())}")
    
    # 方式1: 解析grid数组格式
    if "grid" in data:
        try:
            grid = data["grid"]
            width = data.get("width", 100)
            height = data.get("height", 100)
            
            grid_array = np.array(grid).reshape(height, width)
            
            # 归一化到0-255
            grid_normalized = ((grid_array - grid_array.min()) / 
                             (grid_array.max() - grid_array.min() + 1e-10) * 255).astype(np.uint8)
            
            img = Image.fromarray(grid_normalized, mode='L')
            print(f"✓ 使用grid数据生成图片，尺寸: {img.size}")
            return img
        except Exception as e:
            print(f"解析grid失败: {e}")
    
    # 方式2: 解析base64图片
    for key in ["image", "mapImage", "map_image", "img"]:
        if key in data and isinstance(data[key], str):
            try:
                img_bytes = base64.b64decode(data[key])
                img = Image.open(io.BytesIO(img_bytes))
                print(f"✓ 使用{key}字段生成图片，尺寸: {img.size}")
                return img
            except Exception as e:
                print(f"解析{key}失败: {e}")
    
    # 方式3: 解析二维数组map
    if "map" in data:
        try:
            map_data = data["map"]
            if isinstance(map_data, list):
                map_array = np.array(map_data)
                if map_array.max() > 255:
                    map_array = ((map_array - map_array.min()) / 
                               (map_array.max() - map_array.min() + 1e-10) * 255).astype(np.uint8)
                img = Image.fromarray(map_array.astype(np.uint8), mode='L')
                print(f"✓ 使用map数据生成图片，尺寸: {img.size}")
                return img
        except Exception as e:
            print(f"解析map失败: {e}")
    
    # 方式4: 尝试其他可能的数组字段
    for key in ["data", "array", "matrix", "gridMap"]:
        if key in data and isinstance(data[key], list):
            try:
                arr = np.array(data[key])
                if len(arr.shape) >= 2:
                    if arr.max() > 255:
                        arr = ((arr - arr.min()) / (arr.max() - arr.min() + 1e-10) * 255).astype(np.uint8)
                    img = Image.fromarray(arr.astype(np.uint8), mode='L')
                    print(f"✓ 使用{key}字段生成图片，尺寸: {img.size}")
                    return img
            except Exception as e:
                print(f"解析{key}失败: {e}")
    
    print("✗ 无法解析cost_map数据格式")
    print(f"可用的数据字段: {list(data.keys())}")
    return None

def save_image(img, path):
    """保存图片"""
    try:
        # 确保是RGB模式保存为PNG
        if img.mode == 'L':
            img = img.convert('RGB')
        img.save(path, 'PNG')
        print(f"✓ 图片已保存到: {path}")
        return True
    except Exception as e:
        print(f"✗ 保存图片失败: {e}")
        return False

def main():
    """主流程"""
    print("=" * 50)
    print(f"设备Cost Map获取工具 - {DEVICE_SN}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    
    # 步骤1: 登录获取token
    token = get_token()
    if not token:
        print("\n✗ 任务失败: 无法获取登录token")
        print("请检查:")
        print("  1. API服务是否正常运行")
        print("  2. 网络连接是否正常")
        print("  3. 登录账号密码是否正确")
        sys.exit(1)
    
    # 步骤2: 获取cost_map
    cost_map_result = get_cost_map(DEVICE_SN, token)
    if not cost_map_result:
        print("\n✗ 任务失败: 无法获取cost_map数据")
        sys.exit(1)
    
    # 步骤3: 转换为图片
    img = parse_cost_map_to_image(cost_map_result, DEVICE_SN)
    if not img:
        print("\n✗ 任务失败: 无法解析cost_map为图片")
        print(f"原始数据已保存到: ./cost_map_raw_{DEVICE_SN}.json")
        sys.exit(1)
    
    # 步骤4: 保存图片
    if save_image(img, OUTPUT_PATH):
        print("\n" + "=" * 50)
        print("✓ 任务完成！")
        print(f"✓ 图片路径: {os.path.abspath(OUTPUT_PATH)}")
        print("=" * 50)
        print("\n下一步: 请使用send_image_to_dingtalk工具发送图片到钉钉")
        return OUTPUT_PATH
    else:
        print("\n✗ 任务失败: 图片保存失败")
        sys.exit(1)

if __name__ == "__main__":
    main()