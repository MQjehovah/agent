---
name: device-pull-bag
description: 通过运维平台 API 拉取设备录包文件并上传云端返回下载链接；老设备不支持云端上传接口时，走设备端直传兜底
---
## 适用场景

- 设备故障后需要拉取录包进行回溯分析
- 用户要求下载设备的运行录包或任务录包
- 需要提取特定时间段的录包文件

## 前置条件

- 知道设备 `device_id`（设备序列号）与 `product_id`（产品型号，如 XZ-SC50）
- 知道时间点

## 拉包流程

```
查询录包列表 → 确认目标文件 → 上传云端 → 返回下载链接
```

上传有两条路径：
1. **首选**：云端接口 `upload_bag_file`（走设备功能指令，需设备版本支持）
2. **兜底**：设备端直传（老设备不支持云端上传接口时，经远程终端在设备上用 curl/wget 直传云端）

### 第一步：查询录包列表

调用 `list_device_bags(device_id, product_id, current=1, size=20)` 获取录包分页列表。

| 参数          | 说明                       |
| ------------- | -------------------------- |
| `device_id`   | 设备序列号                 |
| `product_id`  | 产品型号（如 XZ-SC50）     |
| `current`     | 页码，从 1 开始            |
| `size`        | 每页数量，建议 20          |

若已知故障时间，可用 `find_bags_near_time(...)` 按时间就近检索目标切片。

**录包文件特征：**

- 每 2 分钟一个切片
- 文件名包含该切片的**开始时间点**（`all_` 运行录包 / `task_` 任务录包）
- 一般按时间倒序（最新在前）

### 第二步：确认目标文件

从返回列表中找到需要拉取的录包文件路径（如 `/opt/xzrobot/bags/all_2026-06-25-11-22-15_11.bag`）。

- 用户指定时间 → 精准匹配该时间段
- 故障点接近切片边缘 → 可向前/向后多取一个切片

### 第三步：上传到云端（首选）

对目标文件调用 `upload_bag_file(device_id, product_id, file_path, timestamp)`：

| 参数          | 说明                                               |
| ------------- | -------------------------------------------------- |
| `device_id`   | 设备序列号                                         |
| `product_id`  | 产品型号                                           |
| `file_path`   | 录包文件路径（来自 `list_device_bags`）            |
| `timestamp`   | 时间戳（可选）                                     |

- 成功且 `attach_ready=true` 时，`url` 为云端下载地址。
- 若返回「设备不支持 / 该设备当前版本不支持此功能 / 失败 / 无 url」→ **转第四步兜底**。

**⚠️ 约束**：上传录包消耗流量与网盘空间，每次只上传**必要文件**，不要批量上传。

### 第四步：兜底——设备端直传（老设备不支持 `upload_bag_file` 时）

老设备不支持云端 `/remote/bag/upload` 指令，改用远程终端在**设备本地**把 bag 直传到云端文件上传接口。

**4.1 连接设备终端**：`connect_terminal(sn=device_id)`（默认用户名 `xzrobot`，SC50 密码 `xzyz2022!`，T810 密码 `titan@810`，以现场为准）。

**4.2 在设备上执行以下命令**（`send_command` 或 `interactive_session`），把 `<SN>` 与 `<BAG_PATH>` 替换为实际值：

```sh
# 1) 用 SN 换取设备 token（该接口免鉴权、已在云端白名单；token 有效期 7 天）
TOK=$(curl -s "https://bms-cn.xzrobot.com/rosiwit-cloud/device/v2/api/getDeviceToken?sn=<SN>" \
      | python3 -c "import sys,json;d=json.load(sys.stdin);d=d.get('data') or d;print(d.get('token',''))")

# 2) multipart 上传 bag，返回 JSON 的 data.url 即云端地址
curl -s -X POST -H "Authorization: Bearer $TOK" \
     -F "file=@<BAG_PATH>" \
     "https://bms-cn.xzrobot.com/rosiwit-cloud/device/v2/api/xz_robot_common/file/upload"
```

返回示例（`data.url` 即云端下载地址）：

```json
{"data":{"name":"all_....bag","sn":"<SN>","type":".bag",
         "url":"https://xz-server.oss-cn-shanghai.aliyuncs.com/<SN>/<uuid>/all_....bag"},
 "returnCode":200,"returnMsg":"操作成功","success":true}
```

**wget 备选**（设备无 curl 时；GNU wget 不支持 `-F`，需手工拼 multipart）：

```sh
B=----rosiwitboundary$RANDOM
{ printf -- "--$B\r\n";
  printf 'Content-Disposition: form-data; name="file"; filename="%s"\r\n' "$(basename <BAG_PATH>)";
  printf 'Content-Type: application/octet-stream\r\n\r\n';
  cat <BAG_PATH>;
  printf "\r\n--$B--\r\n"; } > /tmp/up_mp.bin
wget -q -O - --header="Authorization: Bearer $TOK" \
     --header="Content-Type: multipart/form-data; boundary=$B" \
     --post-file=/tmp/up_mp.bin \
     "https://bms-cn.xzrobot.com/rosiwit-cloud/device/v2/api/xz_robot_common/file/upload"
rm -f /tmp/up_mp.bin
```

**4.3 上传完成后** `disconnect_terminal(sn=device_id)`。

> 说明：设备端已实测具备 `curl`(7.58) 与 `wget`(1.19.4)。上传为 multipart 且**必须带 token**（云端全局过滤器要求，无免 token 上传口）；本兜底用设备自身 SN 换取设备 token，无需下发用户 token。大文件优先用 `curl -F`（流式，不额外占盘）；wget 方案会先生成一份临时 multipart 副本。

### 第五步：返回下载链接

拿到云端地址（首选路径的 `url`，或兜底的 `data.url`）后向用户返回：

```markdown
📋 设备: {device_id} ({product_id})
📦 拉取结果:
  - [{文件名}]({云端下载路径})
```

若最终仍未取得 url：**不要**声称已上传/已挂附件，应如实说明失败原因。

## 完整示例

用户说"设备 130030J0 昨天下午3点左右有故障，帮我拉一下录包"：

1. `list_device_bags(device_id="130030J0", product_id="XZ-SC50", current=1, size=20)`
2. 从返回列表找到覆盖 15:00 的切片文件（`/opt/xzrobot/bags/all_...bag`）
3. `upload_bag_file(device_id="130030J0", product_id="XZ-SC50", file_path="...")`
4. 若返回"设备不支持" → 兜底：`connect_terminal(sn="130030J0")` → 执行第四步命令 → 取 `data.url` → `disconnect_terminal`
5. 返回 Markdown 下载链接

## 注意事项

- `upload_bag_file` 会消耗设备流量和网盘空间，**每次只拉必要的文件**
- 文件名中的时间是切片**开始时间**，2 分钟后的内容在下一个切片
- 兜底路径的 `getDeviceToken` 只需合法 SN 即可换取设备 token（云端既有白名单设计），token 有效期 7 天
- 若 `list_device_bags` 返回空，可能是设备无录包或时间范围不匹配，直接告知用户录包不存在
