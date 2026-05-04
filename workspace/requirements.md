# 需求文档：为 rosiwit_simulator 机器人添加 3D 激光雷达

> **项目**: rosiwit_simulator / rosiwit_slam  
> **角色**: 产品经理  
> **日期**: 2026-05-04  
> **状态**: 待评审

---

## 1. 需求概述

在 rosiwit_simulator 仿真环境中，为机器人添加一个 3D 激光雷达传感器（Gazebo GPU Ray 插件模拟），发布 `sensor_msgs/PointCloud2` 格式的 3D 点云数据，使其能够与 rosiwit_slam（FAST-LIO2）无缝对接，实现 3D SLAM 建图功能测试。

---

## 2. 现状分析

### 2.1 rosiwit_simulator 现状

| 维度 | 现状 |
|------|------|
| **ROS 版本** | ROS1 (catkin 构建系统，使用 `$(find simulator)` 语法) |
| **机器人模型** | mbot 圆形底盘，差速驱动，双轮+万向轮 |
| **已有传感器** | 2D 激光雷达 (rplidar，发布 `/scan` LaserScan)、IMU (发布 `/imu` Imu) |
| **模型组织方式** | xacro 宏模块化：`sensors/` 目录放传感器物理描述，`gazebo/` 目录放组合模型 |
| **顶层模型** | `mbot_with_laser_gazebo.xacro` — 引用 base + rplidar + imu |
| **2D 雷达参数** | 360采样点、水平±3rad、10Hz、0.1~30m、topic=`/scan` |
| **仿真环境** | Gazebo，默认加载 house.world |

### 2.2 rosiwit_slam (FAST-LIO2) 需求

| 维度 | 要求 |
|------|------|
| **算法** | FAST-LIO2 IEKF，LiDAR+IMU 紧耦合 |
| **点云输入** | `sensor_msgs/PointCloud2` 类型 |
| **默认 topic** | `/ouster/points` (launch 参数可配置) |
| **IMU 输入** | `sensor_msgs/Imu` 类型，默认 `/ouster/imu` |
| **坐标系** | lidar_frame=`os_sensor`，imu_frame=`os_imu`（均可配置） |
| **支持线数** | 6~64 线 |
| **已提供配置** | velodyne_vlp16.yaml、ouster_os1_16.yaml 等 |

### 2.3 差距

当前 simulator 只有 2D 雷达（发布 LaserScan），rosiwit_slam 需要 3D 点云（PointCloud2）。需要在仿真端新增 3D 雷达传感器。

---

## 3. 用户故事

### US-1: 3D 点云数据发布
> 作为 SLAM 开发者，我希望仿真机器人能发布 3D 点云数据（PointCloud2），以便我能测试 FAST-LIO2 算法的建图功能。

### US-2: 与 rosiwit_slam 无缝对接
> 作为 SLAM 开发者，我希望启动仿真后，只需一条命令就能启动 FAST-LIO2 进行建图，不需要额外的 topic 重映射或数据转换。

### US-3: 保留 2D 雷达功能
> 作为导航开发者，我希望 3D 雷达添加后，原有的 2D 雷达仍可独立使用（通过切换模型文件），不影响现有导航功能。

### US-4: 合理的仿真性能
> 作为仿真使用者，我希望 3D 雷达的仿真不会让 Gazebo 过于卡顿，在普通开发机上能流畅运行。

### US-5: 正确的 TF 树
> 作为系统集成者，我希望 3D 雷达的坐标系变换（base_link → lidar3d_link）正确发布，与 SLAM 节点期望的 TF 树一致。

---

## 4. 功能优先级

| 优先级 | 功能 | 说明 |
|--------|------|------|
| **P0** | 新增 3D LiDAR xacro 传感器文件 | `lidar3d.xacro` 定义物理外观和 link/joint |
| **P0** | 新增 3D LiDAR Gazebo 插件配置 | `lidar3d_gazebo.xacro` 使用 `libgazebo_ros_velodyne_gpu_laser.so` 或 `gpu_ray` 插件发布 PointCloud2 |
| **P0** | 新增组合模型 xacro | `mbot_with_lidar3d_gazebo.xacro` 引用 base + 3D lidar + imu |
| **P0** | 新增/更新 launch 文件 | ROS1 launch 文件加载 3D 雷达模型到 Gazebo |
| **P0** | Topic 命名与 SLAM 对齐 | 确保 3D 雷达发布的 topic 名称与 rosiwit_slam 默认配置一致 |
| **P1** | 新增 rosiwit_slam 仿真专用配置 | 为 Gazebo 仿真创建专用的 SLAM 参数配置文件 |
| **P1** | 新增 SLAM 仿真专用 launch 文件 | 一条命令同时启动仿真 + FAST-LIO2 |
| **P2** | RViz 可视化配置更新 | 更新 rviz 配置文件，展示 3D 点云和建图效果 |

---

## 5. 质疑检查清单

### Q1: 用户真正的痛点是什么？

**痛点**: SLAM 开发者需要真实/仿真的 3D 激光雷达数据来开发和调试 FAST-LIO2 算法。当前仿真器只有 2D 雷达，无法提供 3D 点云，导致无法在仿真中验证 3D SLAM 功能。

**不是**: 用户并不是要一个物理机器人上的传感器，而是仿真环境中可用的 3D 点云数据源。

### Q2: 不做这个会怎样？有没有更简单的替代方案？

- **不做**: 只能用 rosbag 数据集测试，无法实时交互式测试，无法验证算法在动态环境下的表现。
- **替代方案**:
  - 直接播放预录制的 rosbag → 无法实时交互，不能测试闭环控制
  - 用 depth camera 模拟点云 → 点云密度和特性与真实 3D 雷达差异大
  - 使用 velodyne_gazebo_plugin → 这正是推荐方案，成熟稳定

### Q3: 成功标准是什么？如何衡量？

| 验收标准 | 验证方法 |
|----------|----------|
| 3D 雷达发布 PointCloud2 数据 | `rostopic echo /ouster/points --noarr` 能看到消息 |
| 点云包含合理的 3D 点 | `rostopic echo /ouster/points/header` 显示正确 frame_id 和时间戳 |
| TF 树包含 base_link→lidar3d_link 变换 | `rosrun tf tf_echo base_link os_sensor` 正常 |
| FAST-LIO2 可以订阅并建图 | 启动 rosiwit_slam 后输出位姿估计和地图 |
| 不影响原有 2D 雷达模型 | 切换回 `mbot_with_laser_gazebo.xacro` 后 2D 雷达正常 |

### Q4: 技术上最大的风险点在哪？

1. **ROS 版本差异**: rosiwit_simulator 使用 ROS1 (catkin)，rosiwit_slam 使用 ROS2。需要在 launch 文件中提供 ROS1 版本启动仿真，同时提供一份 ROS2 版本的 launch 说明（供后续迁移使用）。
2. **Gazebo GPU 插件兼容性**: `libgazebo_ros_velodyne_gpu_laser.so` 需要显卡支持。备选方案是使用 `gpu_ray` + `libgazebo_ros_ray_sensor.so` 组合。
3. **仿真性能**: 3D 雷达点数过多可能导致 Gazebo 卡顿。需要合理控制采样参数（16线 × 1800水平点 = ~28800点/帧）。
4. **坐标系命名对齐**: SLAM 配置默认使用 `os_sensor` 作为 lidar_frame，仿真中需要保持一致或提供 remap。

### Q5: 如果要 1 天内交付，会砍掉什么？

砍掉的：
- ~~RViz 配置文件更新~~ (P2，用户可自行配置)
- ~~ROS2 版本 launch 文件~~ (当前仿真器基于 ROS1，后续再迁移)
- ~~SLAM 仿真专用配置文件~~ (P1，用户可直接修改现有配置的 topic 参数)

保留的：
- 3D 雷达 xacro 传感器文件 + Gazebo 插件
- 组合模型 xacro
- 更新后的 launch 文件

### Q6: 有没有竞品已经做过？我们比他们好在哪里？

- **velodyne_gazebo_plugin**: 开源 Velodyne Gazebo 插件，成熟但需要额外安装。
- **gazebo_ros_ray_sensor**: Gazebo 自带的 ray sensor（支持 `gpu_ray` 类型），可配置 vertical 扫描实现 3D 效果，无需额外安装。
- **我们的方案**: 直接使用 Gazebo 内置 `gpu_ray` sensor + `libgazebo_ros_ray_sensor.so`，零外部依赖，配置简单。同时参照 rosiwit_slam 已有的 `velodyne_vlp16.yaml` 和 `ouster_os1_16.yaml` 配置，确保参数对齐。

---

## 6. 技术方案要点

### 6.1 推荐雷达型号：Velodyne VLP-16

选择理由：
1. rosiwit_slam 已有现成的 `velodyne_vlp16.yaml` 配置文件
2. 16 线，点数适中，仿真性能可控
3. 工业界最常用的 3D 雷达之一，参考数据丰富

### 6.2 关键参数设计

| 参数 | 值 | 说明 |
|------|------|------|
| 线数 | 16 | 匹配 VLP-16 |
| 水平采样点 | 1800 | 0.2° 分辨率 |
| 水平扫描范围 | ±π (360°) | 全方位扫描 |
| 垂直扫描范围 | ±15° (±0.2618 rad) | VLP-16 标准 |
| 扫描频率 | 10 Hz | 匹配 SLAM scan_period=0.1 |
| 测距范围 | 0.5~100m | 匹配 SLAM max_range |
| 点云 topic | `/ouster/points` | 匹配 SLAM 默认 lidar_topic |
| IMU topic | `/imu` (保持不变) | SLAM 可通过参数配置 |
| 坐标系 frame_id | `os_sensor` | 匹配 SLAM lidar_frame |
| 安装位置 | base_link 上方 z=0.20m | 不与现有传感器冲突 |

### 6.3 文件变更清单

| 操作 | 文件路径 | 说明 |
|------|----------|------|
| **新增** | `urdf/xacro/sensors/lidar3d.xacro` | 3D 雷达物理外观定义 |
| **新增** | `urdf/xacro/sensors/lidar3d_gazebo.xacro` | 3D 雷达 Gazebo 插件配置 (gpu_ray + PointCloud2) |
| **新增** | `urdf/xacro/gazebo/mbot_with_lidar3d_gazebo.xacro` | 组合模型：base + 3D lidar + imu |
| **新增** | `launch/simulator_gazebo_3d.launch` | 加载 3D 雷达模型的 Gazebo 启动文件 |
| **新增** | `launch/simulator_slam_3d.launch` | 一键启动仿真 + FAST-LIO2（可选） |
| **保留** | 所有现有文件不变 | 2D 雷达模型完全不受影响 |

---

## 7. 范围边界

### ✅ 做什么

1. 在 rosiwit_simulator 中新增 3D 雷达传感器模块（xacro）
2. 配置 Gazebo 插件发布 PointCloud2 数据
3. 创建新的组合模型和 launch 文件
4. 确保 topic 命名和坐标系与 rosiwit_slam 默认配置对齐
5. 提供验证方法和使用说明

### ❌ 不做什么

1. **不修改** rosiwit_slam 的任何代码或配置
2. **不修改** 现有的 2D 雷达传感器文件（完全独立的新模块）
3. **不做** ROS2 迁移（当前仿真器是 ROS1）
4. **不做** RViz 配置更新（P2，用户自行配置）
5. **不做** 真实传感器驱动（仅仿真）
6. **不做** 性能基准测试或 SLAM 精度评估
7. **不做** velodyne_gazebo_plugin 外部插件安装（使用 Gazebo 内置能力）

---

## 8. 验收标准

### AC-1: 3D 点云数据发布 ✅

```
# 启动仿真
roslaunch simulator simulator_gazebo_3d.launch

# 验证 topic 存在
rostopic list | grep ouster
# 预期输出：
#   /ouster/points

# 验证消息类型
rostopic info /ouster/points
# 预期：Type: sensor_msgs/PointCloud2

# 验证数据内容
rostopic echo /ouster/points/header --noarr -n 1
# 预期：frame_id = "os_sensor", stamp 在更新
```

### AC-2: TF 树正确 ✅

```
rosrun tf view_frames
# 预期 TF 链: base_link → os_sensor (3D 雷达)
# 预期 TF 链: base_link → imu_link (IMU, 已有)
```

### AC-3: 与 FAST-LIO2 对接 ✅

```
# 终端1: 启动仿真
roslaunch simulator simulator_gazebo_3d.launch

# 终端2: 启动 SLAM (假设 ros1_bridge 或同版本)
# 配置 use_sim_time:=true, lidar_topic:=/ouster/points
```

### AC-4: 原有功能不受影响 ✅

```
# 启动原仿真（2D 雷达）
roslaunch simulator simulator_gazebo.launch

# 验证 2D 雷达正常
rostopic echo /scan --noarr -n 1
# 预期：Type: sensor_msgs/LaserScan, 正常发布
```

### AC-5: Gazebo 性能可接受 ✅

- Gazebo 仿真实时率 (Real Time Factor) > 0.5
- 点云发布频率稳定在 10Hz ± 2Hz

---

## 9. 与 rosiwit_slam 配合使用的注意事项

1. **ROS 版本差异**: rosiwit_simulator 基于 ROS1，rosiwit_slam 基于 ROS2。实际联调时需要使用 `ros1_bridge` 桥接，或者将 simulator 迁移到 ROS2。
2. **use_sim_time**: 启动 SLAM 时务必设置 `use_sim_time:=true`。
3. **IMU topic 映射**: 当前 IMU 发布到 `/imu`，SLAM 默认期望 `/ouster/imu`。需要通过 launch 参数 `imu_topic:=/imu` 指定。
4. **坐标系配置**: SLAM 配置中 `lidar_frame` 需设为 `os_sensor`（与仿真一致），或修改仿真端 frame_id 匹配 SLAM 配置。
5. **推荐使用 `velodyne_vlp16.yaml`**: 这是 SLAM 端最匹配的配置，16 线、10Hz、100m 范围完全对齐。

---

## 10. 传递给架构师的关键信息

| 维度 | 信息 |
|------|------|
| **推荐实现** | 使用 Gazebo 内置 `gpu_ray` sensor 类型 + `libgazebo_ros_ray_sensor.so` 插件 |
| **雷达型号** | 模拟 Velodyne VLP-16 (16线) |
| **参考文件** | 现有 `lidar_gazebo.xacro` (2D 雷达)，在其基础上扩展 vertical 扫描 |
| **SLAM 端配置** | 使用 `velodyne_vlp16.yaml`，topic 调整为 `/ouster/points` |
| **ROS 版本注意** | launch 文件使用 ROS1 XML 格式，但需要标注 ROS2 迁移方案 |
