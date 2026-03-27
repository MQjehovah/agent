---
name: device-remote-operations
description: Use when receiving device fault alerts (positioning lost, collision, abnormal state, unresponsive) and need to remotely diagnose and recover devices via platform API
---
# Device Remote Operations

## Overview

Automated device maintenance skill that diagnoses faults via REST API and executes recovery procedures to restore device operation.

## When to Use

Use when:

- Device positioning lost (locate=false in device detail)
- Device collision detected
- Device state abnormal (hasFault=true)
- Device unresponsive (connect=false or timeout)
- Receiving fault alerts from monitoring system

## Fault Types and Recovery Flow

```
┌─────────────────────────────────────────────────────────────┐
│                    FAULT RECEIVED                            │
│                   (sn + fault info)                          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│              STEP 1: Get Device Detail                       │
│         POST /xz_sc50/fae/detail {sn}                        │
│    Check: connect, locate, hasFault, faultDTOList           │
└─────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              │               │               │
              ▼               ▼               ▼
        ┌──────────┐   ┌──────────┐   ┌──────────┐
        │connect=  │   │locate=   │   │hasFault= │
        │  false   │   │  false   │   │  true    │
        └──────────┘   └──────────┘   └──────────┘
              │               │               │
              ▼               ▼               ▼
        ┌──────────┐   ┌──────────┐   ┌──────────┐
        │ OFFLINE  │   │POSITIONING│   │  FAULT   │
        │ HANDLER  │   │  LOST     │   │ HANDLER  │
        └──────────┘   └──────────┘   └──────────┘
              │               │               │
              │               ▼               ▼
              │         ┌──────────┐   ┌──────────┐
              │         │ relocate │   │ diagnose │
              │         │   API    │   │   API    │
              │         └──────────┘   └──────────┘
              │               │               │
              │               │               ▼
              │               │         ┌──────────┐
              │               │         │ Recovery │
              │               │         │  Action  │
              │               │         └──────────┘
              │               │               │
              └───────────────┴───────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │ Verify Recovery │
                    │ Get detail again│
                    └─────────────────┘
```

## Quick Reference

| Fault Type           | Detection                       | API Endpoint                            | Recovery Action        |
| -------------------- | ------------------------------- | --------------------------------------- | ---------------------- |
| Positioning Lost     | locate=false                    | `/fae/relocate`                       | Relocate with position |
| Device Collision     | faultDTOList contains collision | `/fae/move` mode=0 then backward      | Stop + reverse         |
| State Abnormal       | hasFault=true                   | `/fae/fault_diagnose` → specific fix | Diagnose first         |
| Unresponsive         | connect=false or timeout        | `/fae/soft_restart`                   | Soft restart           |
| Parameters Corrupted | factory settings issue          | `/fae/factory_reset`                  | Reset parameters       |

## Implementation

### Base Configuration

```typescript
const API_BASE = '/xz_sc50/fae';
const HEADERS = { 'Content-Type': 'application/json', 'token': '<YOUR_TOKEN>' };
```

### Get Token

GET /xz_robot_common/user/login

| 参数       | 说明     | 必须 | 类型   |
| ---------- | -------- | ---- | ------ |
| username   | 账号名称 | true | string |
| password   | 密码     | true | string |
| clientType | 客户端   | true | string |

账号：admin

密码：123456

clientType：WEB

### Core API Calls

```typescript
async function getDeviceDetail(sn: string): Promise<DeviceDetail> {
  const response = await fetch(`${API_BASE}/detail`, {
    method: 'POST',
    headers: HEADERS,
    body: JSON.stringify({ sn })
  });
  return response.json();
}

async function relocate(sn: string, position: number[]): Promise<void> {
  await fetch(`${API_BASE}/relocate`, {
    method: 'POST',
    headers: HEADERS,
    body: JSON.stringify({ sn, position })
  });
}

async function softRestart(sn: string): Promise<void> {
  await fetch(`${API_BASE}/soft_restart`, {
    method: 'POST',
    headers: HEADERS,
    body: JSON.stringify({ sn })
  });
}

async function faultDiagnose(sn: string): Promise<void> {
  await fetch(`${API_BASE}/fault_diagnose`, {
    method: 'POST',
    headers: HEADERS,
    body: JSON.stringify({ sn })
  });
}

async function moveRobot(sn: string, mode: MoveMode): Promise<void> {
  await fetch(`${API_BASE}/move`, {
    method: 'POST',
    headers: HEADERS,
    body: JSON.stringify({ sn, mode })
  });
}

async function backward(sn: string): Promise<void> {
  await fetch(`${API_BASE}/backward`, {
    method: 'POST',
    headers: HEADERS,
    body: JSON.stringify({ sn })
  });
}

async function factoryReset(sn: string): Promise<void> {
  await fetch(`${API_BASE}/factory_reset`, {
    method: 'POST',
    headers: HEADERS,
    body: JSON.stringify({ sn })
  });
}
```

### Move Modes

| Mode | Action                |
| ---- | --------------------- |
| 0    | Stop                  |
| 1    | Forward               |
| 2    | Right turn + forward  |
| 3    | Right rotation        |
| 4    | Right turn + backward |
| 5    | Backward              |
| 6    | Left turn + backward  |
| 7    | Left rotation         |
| 8    | Left turn + forward   |

### Recovery Procedures

#### 1. Positioning Lost Recovery

```typescript
async function recoverPositioningLost(sn: string, position: number[]): Promise<boolean> {
  const before = await getDeviceDetail(sn);
  if (!before.locate) {
    await relocate(sn, position);
    await sleep(3000);
    const after = await getDeviceDetail(sn);
    return after.locate === true;
  }
  return true;
}
```

#### 2. Collision Recovery

```typescript
async function recoverCollision(sn: string): Promise<boolean> {
  await moveRobot(sn, 0);
  await sleep(1000);
  await backward(sn);
  await sleep(2000);
  await moveRobot(sn, 0);
  
  const detail = await getDeviceDetail(sn);
  return !detail.faultDTOList.some(f => f.code.includes('collision'));
}
```

#### 3. State Abnormal Recovery

```typescript
async function recoverAbnormalState(sn: string): Promise<boolean> {
  await faultDiagnose(sn);
  await sleep(5000);
  
  const detail = await getDeviceDetail(sn);
  if (detail.hasFault) {
    const faultCodes = detail.faultDTOList.map(f => f.code);
  
    if (faultCodes.some(c => c.includes('PARAM'))) {
      await factoryReset(sn);
    }
    if (faultCodes.some(c => c.includes('HANG'))) {
      await softRestart(sn);
    }
  }
  
  await sleep(3000);
  const after = await getDeviceDetail(sn);
  return !after.hasFault;
}
```

#### 4. Unresponsive Recovery

```typescript
async function recoverUnresponsive(sn: string): Promise<boolean> {
  try {
    const detail = await getDeviceDetail(sn);
    if (!detail.connect) {
      await softRestart(sn);
      await sleep(10000);
  
      const after = await getDeviceDetail(sn);
      return after.connect === true;
    }
    return true;
  } catch (error) {
    await softRestart(sn);
    await sleep(10000);
    return false;
  }
}
```

### Unified Recovery Entry

```typescript
async function handleFault(sn: string, faultType: FaultType, options?: { position?: number[] }): Promise<RecoveryResult> {
  const detail = await getDeviceDetail(sn);
  let success = false;
  let action = '';

  switch (faultType) {
    case 'POSITIONING_LOST':
      if (!detail.locate && options?.position) {
        success = await recoverPositioningLost(sn, options.position);
        action = 'relocate';
      }
      break;
  
    case 'COLLISION':
      success = await recoverCollision(sn);
      action = 'stop+backward';
      break;
  
    case 'STATE_ABNORMAL':
      if (detail.hasFault) {
        success = await recoverAbnormalState(sn);
        action = 'diagnose+recovery';
      }
      break;
  
    case 'UNRESPONSIVE':
      if (!detail.connect) {
        success = await recoverUnresponsive(sn);
        action = 'soft_restart';
      }
      break;
  }

  return { sn, faultType, success, action, timestamp: Date.now() };
}
```

## Common Mistakes

| Mistake                                 | Fix                                       |
| --------------------------------------- | ----------------------------------------- |
| Not checking device state before action | Always call getDeviceDetail first         |
| Missing delay after API call            | Add 3-5 second delays for device response |
| Not verifying recovery result           | Call getDeviceDetail again to confirm     |
| Using wrong position format             | Position is number array [x, y, theta]    |
| Ignoring fault level                    | Check faultDTOList[].level for severity   |

## API Endpoints Summary

| Endpoint                        | Purpose           | Body                 |
| ------------------------------- | ----------------- | -------------------- |
| `/fae/detail`                 | Get device status | `{sn}`             |
| `/fae/relocate`               | Relocate device   | `{sn, position[]}` |
| `/fae/soft_restart`           | Soft restart      | `{sn}`             |
| `/fae/fault_diagnose`         | Diagnose fault    | `{sn}`             |
| `/fae/move`                   | Control movement  | `{sn, mode}`       |
| `/fae/backward`               | Move backward     | `{sn}`             |
| `/fae/factory_reset`          | Reset parameters  | `{sn}`             |
| `/fae/device/real_time_state` | Real-time data    | `{sn}`             |
| `/fae/cost_map`               | Perception map    | `{sn}`             |
| `/fae/point_cloud`            | Point cloud data  | `{sn}`             |

## Device Detail Key Fields

| Field            | Type    | Description                                          |
| ---------------- | ------- | ---------------------------------------------------- |
| `connect`      | boolean | Connection status                                    |
| `locate`       | boolean | true=positioned, false=lost                          |
| `hasFault`     | boolean | Has fault                                            |
| `faultDTOList` | array   | Fault details [{code, level, module, name, content}] |
| `position`     | array   | Current position [x, y, theta]                       |
| `battery`      | int     | Battery percentage                                   |
| `runState`     | int     | Work state code                                      |
| `runStateName` | string  | Work state name                                      |
