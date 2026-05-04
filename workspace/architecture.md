# 架构文档：为 rosiwit_simulator 机器人添加 3D 激光雷达

> **项目**: rosiwit_simulator / rosiwit_slam
> **角色**: 软件架构师
> **日期**: 2026-05-05
> **版本**: v1.0

---

## 1. 架构总览

### 1.1 目标

在 rosiwit_simulator（ROS1/Gazebo）中为 mbot 机器人新增 3D 激光雷达传感器，模拟 Velodyne VLP-16 规格，发布 `sensor_msgs/PointCloud2` 点云数据，使其通过 `ros1_bridge` 与 rosiwit_slam（ROS2/FAST-LIO2）无缝对接。

### 1.2 设计原则

| 原则 | 说明 |
|------|------|
| **最小侵入** | 不修改任何已有文件，仅新增文件 |
| **模块复用** | 复用已有 IMU 配置，新 3D 雷达作为独立 xacro 宏 |
| **模式切换** | 2D 雷达与 3D 雷达通过不同 launch 文件切换，互不影响 |
| **兼容对齐** | Topic、Frame、数据类型与 rosiwit_slam 的 `velodyne_vlp16.yaml` 对齐 |

---

## 2. 架构图

### 2.1 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    Gazebo 仿真环境                        │
│                                                         │
│  ┌──────────┐   ┌──────────────┐   ┌──────────────┐     │
│  │ mbot_base │   │  IMU 传感器   │   │  3D LiDAR    │     │
│  │ 差速驱动  │   │ libgazebo_   │   │ gpu_ray +    │     │
│  │ diff_drive│   │ ros_imu_     │   │ libgazebo_   │     │
│  │           │   │ sensor.so    │   │ ros_ray_     │     │
│  │           │   │              │   │ sensor.so    │     │
│  └─────┬─────┘   └──────┬───────┘   └──────┬───────┘     │
│        │                │                  │             │
│   /odom /cmd_vel    /imu            /velodyne_points     │
│        │                │                  │             │
└────────┼────────────────┼──────────────────┼─────────────┘
         │                │                  │
    ┌────▼────────────────▼──────────────────▼────┐
    │              ROS1 Master                     │
    │    (rosmaster + roscore)                     │
    └────┬────────────────┬──────────────────┬─────┘
         │                │                  │
    ┌────▼────────────────▼──────────────────▼─────┐
    │           ros1_bridge (ROS1 ↔ ROS2)           │
    │    自动桥接 sensor_msgs/Imu                    │
    │    自动桥接 sensor_msgs/PointCloud2            │
    │    自动桥接 tf2_msgs/TFMessage                 │
    └────┬────────────────┬──────────────────┬─────┘
         │                │                  │
    ┌────▼────────────────▼──────────────────▼─────┐
    │              ROS2 (Humble)                    │
    │                                               │
    │  ┌──────────────────────────────────────┐     │
    │  │         FAST-LIO2 SLAM Node          │     │
    │  │                                      │     │
    │  │  订阅:                               │     │
    │  │    /velodyne_points  [PointCloud2]   │     │
    │  │    /imu              [Imu]           │     │
    │  │                                      │     │
    │  │  发布:                               │     │
    │  │    /odom_estimated   [Odometry]      │     │
    │  │    /path_estimated   [Path]          │     │
    │  │    /cloud_map        [PointCloud2]   │     │
    │  │    TF: map→odom→base_link            │     │
    │  └──────────────────────────────────────┘     │
    └───────────────────────────────────────────────┘
```

### 2.2 TF 树

```
新增3D雷达后的完整TF树:

map (FAST-LIO2 发布)
 └─ odom (diff_drive 发布 / FAST-LIO2 覆盖)
     └─ base_footprint (diff_drive 发布)
         └─ base_link (robot_state_publisher 发布)
             ├─ left_wheel_link  [已有]
             ├─ right_wheel_link [已有]
             ├─ front_caster_link  [已有]
             ├─ back_caster_link   [已有]
             ├─ imu_link        [已有] ← IMU 传感器
             └─ velodyne_link   [新增] ← 3D 激光雷达
```

---

## 3. 模块列表

### 3.1 模块总览

| # | 模块 | 类型 | 职责 | 优先级 |
|---|------|------|------|--------|
| M1 | `lidar3d.xacro` | 传感器物理描述 | 定义 3D 雷达的 link（视觉/碰撞/惯性）和 joint（固定在 base_link 上方） | P0 |
| M2 | `lidar3d_gazebo.xacro` | Gazebo 插件配置 | 配置 `gpu_ray` 传感器 + `libgazebo_ros_ray_sensor.so` 插件发布 PointCloud2 | P0 |
| M3 | `mbot_with_lidar3d_gazebo.xacro` | 组合模型 | 组合 mbot_base + lidar3d + imu 的顶层 xacro | P0 |
| M4 | `simulator_gazebo_3d.launch` | Launch 文件 | 启动 Gazebo + 加载 3D 雷达模型 + robot_state_publisher | P0 |
| M5 | `simulator_3d.rviz` | RViz 配置 | 预配置 PointCloud2 显示，方便验证 | P1 |

### 3.2 模块详细接口

#### M1: `lidar3d.xacro` — 3D 雷达物理描述

```
文件: urdf/xacro/sensors/lidar3d.xacro
宏名: lidar3d
参数: prefix:=velodyne

接口:
  - 创建 link: ${prefix}_link  (velodyne_link)
    - visual:   cylinder, radius=0.0516m, length=0.0717m (模拟VLP-16外形)
    - collision: cylinder, radius=0.0516m, length=0.0717m
    - inertial:  mass=0.83kg (VLP-16实际重量), 惯性矩阵按圆柱体计算
    - Gazebo 材质: Gazebo/Grey

  - 创建 joint: ${prefix}_joint  (velodyne_joint)
    - type: fixed
    - parent: base_link
    - child:  velodyne_link
    - origin: xyz="0 0 0.1955" rpy="0 0 0"
      (base_link顶面z=0.08 + VLP-16半高0.03585 + 间隙 ≈ 0.1955)
```

#### M2: `lidar3d_gazebo.xacro` — Gazebo GPU Ray 插件

```
文件: urdf/xacro/sensors/lidar3d_gazebo.xacro
宏名: lidar3d_gazebo
参数: prefix:=velodyne

接口:
  - 继承 M1 的全部物理描述
  - 新增 Gazebo sensor 配置:
    sensor type="gpu_ray" name="velodyne_vlp16"
      ├─ update_rate: 10 Hz
      ├─ ray:
      │   ├─ scan:
      │   │   ├─ horizontal:
      │   │   │   ├─ samples: 1800      (360° / 0.2° 分辨率)
      │   │   │   ├─ resolution: 1
      │   │   │   ├─ min_angle: -3.14159 (-180°)
      │   │   │   └─ max_angle:  3.14159 (+180°)
      │   │   └─ vertical:
      │   │       ├─ samples: 16        (VLP-16 = 16线)
      │   │       ├─ resolution: 1
      │   │       ├─ min_angle: -0.2618  (-15°)
      │   │       └─ max_angle:  0.2618  (+15°)
      │   ├─ range:
      │   │   ├─ min: 0.5
      │   │   ├─ max: 100.0
      │   │   └─ resolution: 0.01
      │   └─ noise:
      │       ├─ type: gaussian
      │       ├─ mean: 0.0
      │       └─ stddev: 0.01
      └─ plugin:
          filename: libgazebo_ros_ray_sensor.so
          name: velodyne_plugin
          ├─ topicName: /velodyne_points
          ├─ frameName: velodyne_link
          └─ outputType: sensor_msgs/PointCloud2
```

**3D 雷达参数设计说明**（对齐 Velodyne VLP-16 规格）：

| 参数 | 值 | 说明 |
|------|-----|------|
| 水平采样数 | 1800 | VLP-16 双回波模式下约 1800 点/圈 |
| 水平角度范围 | ±π rad (360°) | 全方位扫描 |
| 垂直采样数 | 16 | 16 线 |
| 垂直角度范围 | ±15° (±0.2618 rad) | VLP-16 规格 |
| 扫描频率 | 10 Hz | VLP-16 标准 |
| 测量范围 | 0.5 ~ 100.0 m | 对齐 velodyne_vlp16.yaml |
| Topic | `/velodyne_points` | 与 SLAM config 对齐 |
| Frame | `velodyne_link` | 与 SLAM config 的 `lidar_frame` 对齐 |

#### M3: `mbot_with_lidar3d_gazebo.xacro` — 组合模型

```
文件: urdf/xacro/gazebo/mbot_with_lidar3d_gazebo.xacro

结构:
  xacro:include ← mbot_base.xacro
  xacro:include ← sensors/lidar3d_gazebo.xacro
  xacro:include ← sensors/imu_gazebo.xacro

  实例化:
    xacro:mbot_base /          ← 底盘 + 差速驱动
    xacro:lidar3d_gazebo /     ← 3D 雷达 (含物理+Gazebo插件)
    xacro:imu prefix="imu"     ← IMU (复用已有)
```

#### M4: `simulator_gazebo_3d.launch` — Launch 文件

```
文件: launch/simulator_gazebo_3d.launch

结构 (基于 simulator_gazebo.launch 模板):
  1. 环境变量: GAZEBO_MODEL_PATH
  2. Gazebo 启动: empty_world.launch + house.world
  3. 模型加载:
     robot_description ← xacro(mbot_with_lidar3d_gazebo.xacro)
     spawn_model → Gazebo
  4. robot_state_publisher (50Hz)
  5. joint_state_publisher
```

#### M5: `simulator_3d.rviz` — RViz 配置（可选 P1）

```
文件: rviz/simulator_3d.rviz

显示项:
  - RobotModel (URDF 可视化)
  - PointCloud2 (/velodyne_points, Color=Z-axis, Size=0.01)
  - TF (坐标系树可视化)
  - Grid (参考网格)
  - Imu (/imu, 可选)
```

---

## 4. 数据流图

### 4.1 关键数据流

```
                    Gazebo 内部
┌──────────────────────────────────────────────────────┐
│                                                      │
│  ┌──────────┐                    ┌──────────────┐    │
│  │ gpu_ray  │ ──内部接口──────→ │ ray_sensor   │    │
│  │ sensor   │                    │ plugin       │    │
│  └──────────┘                    └──────┬───────┘    │
│                                         │            │
│  ┌──────────┐                    ┌──────┴───────┐    │
│  │ diff_    │                    │ imu_sensor   │    │
│  │ drive    │                    │ plugin       │    │
│  └────┬─────┘                    └──────┬───────┘    │
│       │                                 │            │
└───────┼─────────────────────────────────┼────────────┘
        │                                 │
   ROS1 Topics                       ROS1 Topics
   ──────────                        ──────────
   /odom (nav_msgs/Odometry)         /velodyne_points (sensor_msgs/PointCloud2)
   /tf (tf2_msgs/TFMessage)          /imu (sensor_msgs/Imu)
   /cmd_vel (geometry_msgs/Twist)          │
        │                                  │
        ▼                                  ▼
   ┌──────────────────────────────────────────┐
   │            ros1_bridge                   │
   │  自动桥接:                               │
   │    /velodyne_points (PointCloud2)        │
   │    /imu (Imu)                            │
   │    /tf (TFMessage)                       │
   │    /odom (Odometry)                      │
   └──────────────┬───────────────────────────┘
                  │
             ROS2 Topics
             ──────────
             /velodyne_points [sensor_msgs/PointCloud2]
             /imu             [sensor_msgs/Imu]
                  │
                  ▼
   ┌──────────────────────────────────────────┐
   │          FAST-LIO2 SLAM Node             │
   │                                          │
   │  Launch 参数配置:                         │
   │    config_file:=velodyne_vlp16.yaml      │
   │    lidar_topic:=/velodyne_points         │
   │    imu_topic:=/imu                       │
   │    use_sim_time:=true                    │
   │                                          │
   │  配置文件对齐 (velodyne_vlp16.yaml):      │
   │    lidar.scan_line: 16                   │
   │    lidar.scan_period: 0.1                │
   │    lidar.max_range: 100.0                │
   │    lidar.min_range: 0.5                  │
   │    ros.lidar_frame: "velodyne"           │
   │    ros.imu_frame: "imu"                  │
   └──────────────────────────────────────────┘
```

### 4.2 Topic 兼容性映射表

| 仿真端 Topic | 类型 | Bridge | SLAM 端配置 | 匹配 |
|-------------|------|--------|------------|------|
| `/velodyne_points` | sensor_msgs/PointCloud2 | 自动桥接 | `lidar_topic: /velodyne_points` | ✅ |
| `/imu` | sensor_msgs/Imu | 自动桥接 | `imu_topic: /imu` | ✅ |
| `/odom` | nav_msgs/Odometry | 自动桥接 | `odom_topic: /odom` | ✅ |
| `/tf` | tf2_msgs/TFMessage | 自动桥接 | TF 内部订阅 | ✅ |

### 4.3 坐标系兼容性

| 仿真端 Frame | SLAM 配置项 | velodyne_vlp16.yaml 值 | 需要调整 |
|-------------|------------|----------------------|---------|
| `velodyne_link` | `ros.lidar_frame` | `"velodyne"` | ✅ 匹配 (xacro中frameName=velodyne_link, SLAM配置为"velodyne"，需统一) |
| `imu_link` | `ros.imu_frame` | `"imu"` | ✅ 匹配 |
| `base_link` | `ros.base_frame` | `"base_link"` | ✅ 匹配 |

> **注意**: `velodyne_vlp16.yaml` 中 `lidar_frame: "velodyne"`，而 Gazebo 插件中 `frameName: "velodyne_link"`。
> 这需要在 SLAM 启动时通过 launch 参数 `lidar_frame:=velodyne_link` 对齐，或修改 yaml 配置为 `"velodyne_link"`。
> **推荐方案**: 将 Gazebo 插件的 `frameName` 设为 `"velodyne"`（无 `_link` 后缀），与 yaml 配置完全一致。

---

## 5. 目录结构

### 5.1 新增文件（标记 🆕）

```
rosiwit_simulator/
├── urdf/
│   └── xacro/
│       ├── sensors/
│       │   ├── lidar.xacro              [已有] 2D雷达物理描述
│       │   ├── lidar_gazebo.xacro       [已有] 2D雷达Gazebo插件
│       │   ├── lidar3d.xacro            🆕 3D雷达物理描述 (M1)
│       │   ├── lidar3d_gazebo.xacro     🆕 3D雷达Gazebo插件 (M2)
│       │   ├── imu.xacro                [已有]
│       │   ├── imu_gazebo.xacro         [已有]
│       │   ├── camera.xacro             [已有]
│       │   ├── camera_gazebo.xacro      [已有]
│       │   ├── kinect.xacro             [已有]
│       │   └── kinect_gazebo.xacro      [已有]
│       └── gazebo/
│           ├── mbot_base.xacro                    [已有] 底盘+差速驱动
│           ├── mbot_gazebo.xacro                   [已有]
│           ├── mbot_with_laser_gazebo.xacro        [已有] 2D雷达组合模型
│           ├── mbot_with_camera_gazebo.xacro       [已有]
│           ├── mbot_with_kinect_gazebo.xacro       [已有]
│           └── mbot_with_lidar3d_gazebo.xacro      🆕 3D雷达组合模型 (M3)
├── launch/
│   ├── simulator_gazebo.launch                     [已有] 2D仿真启动
│   ├── simulator_gazebo_3d.launch                  🆕 3D仿真启动 (M4)
│   └── ...                                         [已有]
├── rviz/
│   ├── urdf.rviz                                   [已有]
│   └── simulator_3d.rviz                           🆕 3D仿真RViz配置 (M5, P1)
└── ...
```

### 5.2 不修改任何已有文件

所有 P0 功能通过新增文件实现，确保原有 2D 雷达仿真完全不受影响。

---

## 6. 核心文件原型

### 6.1 `lidar3d.xacro`

```xml
<?xml version="1.0"?>
<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="lidar3d">

    <xacro:macro name="lidar3d" params="prefix:=velodyne">
        <!-- 3D LiDAR 物理描述 (Velodyne VLP-16 外观) -->
        <link name="${prefix}_link">
            <inertial>
                <mass value="0.83" />
                <origin xyz="0 0 0" />
                <inertia ixx="0.001" ixy="0.0" ixz="0.0"
                         iyy="0.001" iyz="0.0"
                         izz="0.001" />
            </inertial>

            <visual>
                <origin xyz="0 0 0" rpy="0 0 0" />
                <geometry>
                    <cylinder length="0.0717" radius="0.0516"/>
                </geometry>
                <material name="grey">
                    <color rgba="0.5 0.5 0.5 1"/>
                </material>
            </visual>

            <collision>
                <origin xyz="0 0 0" rpy="0 0 0" />
                <geometry>
                    <cylinder length="0.0717" radius="0.0516"/>
                </geometry>
            </collision>
        </link>

        <!-- 3D LiDAR 固定关节 -->
        <joint name="${prefix}_joint" type="fixed">
            <origin xyz="0 0 0.1955" rpy="0 0 0" />
            <parent link="base_link"/>
            <child link="${prefix}_link"/>
        </joint>

        <gazebo reference="${prefix}_link">
            <material>Gazebo/Grey</material>
        </gazebo>
    </xacro:macro>

</robot>
```

### 6.2 `lidar3d_gazebo.xacro`

```xml
<?xml version="1.0"?>
<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="lidar3d">

    <xacro:macro name="lidar3d_gazebo" params="prefix:=velodyne">
        <xacro:include filename="$(find simulator)/urdf/xacro/sensors/lidar3d.xacro" />
        <xacro:lidar3d prefix="${prefix}"/>

        <!-- Gazebo GPU Ray Sensor for 3D LiDAR -->
        <gazebo reference="${prefix}_link">
            <sensor type="gpu_ray" name="velodyne_vlp16">
                <pose>0 0 0 0 0 0</pose>
                <visualize>false</visualize>
                <update_rate>10</update_rate>
                <ray>
                    <scan>
                        <horizontal>
                            <samples>1800</samples>
                            <resolution>1</resolution>
                            <min_angle>-3.14159</min_angle>
                            <max_angle>3.14159</max_angle>
                        </horizontal>
                        <vertical>
                            <samples>16</samples>
                            <resolution>1</resolution>
                            <min_angle>-0.2618</min_angle>
                            <max_angle>0.2618</max_angle>
                        </vertical>
                    </scan>
                    <range>
                        <min>0.5</min>
                        <max>100.0</max>
                        <resolution>0.01</resolution>
                    </range>
                    <noise>
                        <type>gaussian</type>
                        <mean>0.0</mean>
                        <stddev>0.01</stddev>
                    </noise>
                </ray>
                <plugin name="velodyne_plugin" filename="libgazebo_ros_ray_sensor.so">
                    <topicName>/velodyne_points</topicName>
                    <frameName>velodyne</frameName>
                    <outputType>sensor_msgs/PointCloud2</outputType>
                </plugin>
            </sensor>
        </gazebo>
    </xacro:macro>

</robot>
```

### 6.3 `mbot_with_lidar3d_gazebo.xacro`

```xml
<?xml version="1.0"?>
<robot name="arm" xmlns:xacro="http://www.ros.org/wiki/xacro">

    <xacro:include filename="$(find simulator)/urdf/xacro/gazebo/mbot_base.xacro" />
    <xacro:include filename="$(find simulator)/urdf/xacro/sensors/lidar3d_gazebo.xacro" />
    <xacro:include filename="$(find simulator)/urdf/xacro/sensors/imu_gazebo.xacro" />

    <xacro:lidar3d_gazebo prefix="velodyne"/>
    <xacro:imu prefix="imu"/>

    <xacro:mbot_base/>

</robot>
```

### 6.4 `simulator_gazebo_3d.launch`

```xml
<launch>
    <env name="GAZEBO_MODEL_PATH" value="$(find simulator)/models" />

    <!-- 设置launch文件的参数 -->
    <arg name="world_name" value="$(find simulator)/world/house.world" />
    <arg name="use_sim_time" default="true" />
    <arg name="gui" default="true" />
    <arg name="paused" default="false" />
    <arg name="headless" default="false" />
    <arg name="debug" default="false" />

    <!-- 运行gazebo仿真环境 -->
    <include file="$(find gazebo_ros)/launch/empty_world.launch">
        <arg name="world_name" value="$(arg world_name)" />
        <arg name="debug" value="$(arg debug)" />
        <arg name="gui" value="$(arg gui)" />
        <arg name="paused" value="$(arg paused)" />
        <arg name="use_sim_time" value="$(arg use_sim_time)" />
        <arg name="headless" value="$(arg headless)" />
    </include>

    <!-- 加载3D雷达机器人模型描述参数 -->
    <param name="robot_description"
           command="$(find xacro)/xacro --inorder '$(find simulator)/urdf/xacro/gazebo/mbot_with_lidar3d_gazebo.xacro'" />

    <!-- 在gazebo中加载机器人模型 -->
    <node name="urdf_spawner" pkg="gazebo_ros" type="spawn_model"
          respawn="false" output="screen"
          args="-urdf -model mrobot -param robot_description" />

    <!-- 运行joint_state_publisher节点 -->
    <node name="joint_state_publisher" pkg="joint_state_publisher" type="joint_state_publisher" />

    <!-- 运行robot_state_publisher节点，发布tf -->
    <node name="robot_state_publisher" pkg="robot_state_publisher" type="robot_state_publisher" output="screen">
        <param name="publish_frequency" type="double" value="50.0" />
    </node>

</launch>
```

---

## 7. 错误处理策略

### 7.1 错误场景与处理

| # | 错误场景 | 影响 | 处理策略 |
|---|---------|------|---------|
| E1 | GPU Ray 插件加载失败（无 GPU） | 无点云数据 | **降级方案**: 提供使用 `ray`（非 gpu）的备选 xacro，见 M2-alt |
| E2 | `libgazebo_ros_ray_sensor.so` 不存在 | 仿真无法启动 | 在 launch 中添加 prereq 检查，错误日志提示安装 `ros-$ROS_DISTRO-gazebo-ros` |
| E3 | Topic 无数据发布 | SLAM 无输入 | 验证脚本检查 topic 频率和数据类型 |
| E4 | TF 树断裂（velodyne_link 未连接） | SLAM 坐标变换失败 | 确保 joint type=fixed 且 robot_state_publisher 正常运行 |
| E5 | ros1_bridge 未启动 | ROS2 端收不到数据 | 文档明确 bridge 启动步骤 |
| E6 | use_sim_time 未同步 | 时间戳不一致 | launch 文件默认 use_sim_time=true，SLAM 端也须设置 |
| E7 | 垂直角度配置不对齐 | SLAM 算法异常 | 参数严格对齐 velodyne_vlp16.yaml 中的 max_angle=30°（±15°） |

### 7.2 降级方案 (M2-alt)

如果目标机器没有 GPU 或 GPU Ray 不可用，可提供 CPU 版本的 xacro：

```
文件: urdf/xacro/sensors/lidar3d_gazebo_cpu.xacro

区别: sensor type="ray" (非 "gpu_ray")
性能: CPU 模式下建议降低水平采样点至 360 ~ 900，保证 RTF > 0.5
```

---

## 8. 验证与测试矩阵

### 8.1 验证步骤

```bash
# ===== 步骤 1: 编译 =====
cd ~/rosiwit_ws
catkin_make
source devel/setup.bash

# ===== 步骤 2: 启动 3D 仿真 =====
roslaunch simulator simulator_gazebo_3d.launch

# ===== 步骤 3: 验证 Topic =====
# 终端 2
rostopic list | grep velodyne
# 预期: /velodyne_points

rostopic info /velodyne_points
# 预期: Type: sensor_msgs/PointCloud2, Publishers: /gazebo

rostopic hz /velodyne_points
# 预期: ~10 Hz

# ===== 步骤 4: 验证 TF =====
rosrun tf view_frames
# 预期 TF 链: base_link → velodyne_link (或 velodyne, 取决于frameName配置)

# ===== 步骤 5: 验证 2D 雷达不受影响 =====
# 新终端
roslaunch simulator simulator_gazebo.launch
rostopic echo /scan --noarr -n 1
# 预期: Type: sensor_msgs/LaserScan, 正常发布

# ===== 步骤 6: RViz 可视化 =====
rviz -d $(rospack find simulator)/rviz/simulator_3d.rviz
# 添加 PointCloud2 display, topic=/velodyne_points
# 预期: 能看到 3D 点云可视化
```

### 8.2 测试矩阵

| 测试ID | 测试项 | 类型 | 输入 | 预期结果 | 优先级 |
|--------|--------|------|------|---------|--------|
| T01 | PointCloud2 发布 | 集成 | 启动 3D 仿真 | `/velodyne_points` 以 10±2Hz 发布 | P0 |
| T02 | PointCloud2 数据类型 | 集成 | `rostopic info` | Type: sensor_msgs/PointCloud2 | P0 |
| T03 | frame_id 正确性 | 集成 | `rostopic echo /velodyne_points/header` | frame_id = "velodyne" | P0 |
| T04 | TF 树正确性 | 集成 | `rosrun tf view_frames` | base_link → velodyne link/joint 存在 | P0 |
| T05 | 2D 雷达独立性 | 回归 | 启动原 launch | `/scan` 正常发布 LaserScan | P0 |
| T06 | Gazebo 实时率 | 性能 | 启动 3D 仿真 | Real Time Factor > 0.5 | P1 |
| T07 | 点云范围验证 | 功能 | `rostopic echo` | 点云距离范围 0.5~100m | P1 |
| T08 | 16 线验证 | 功能 | 检查 PointCloud2 点数 | 每帧约 28800 点 (1800×16) | P1 |
| T09 | IMU 数据正常 | 回归 | `rostopic hz /imu` | 100Hz, 数据类型 Imu | P0 |
| T10 | FAST-LIO2 对接 | 集成 | 启动 SLAM | SLAM 正常运行，输出里程计 | P0 |

### 8.3 与 rosiwit_slam 配合使用的注意事项

| # | 事项 | 说明 |
|---|------|------|
| 1 | **ROS 版本桥接** | rosiwit_simulator 是 ROS1，rosiwit_slam 是 ROS2，必须通过 `ros1_bridge` 桥接 |
| 2 | **Bridge 启动顺序** | 先启动 ROS1 master → 再启动 `ros1_bridge` → 最后启动 ROS2 SLAM |
| 3 | **use_sim_time** | SLAM 启动时必须设置 `use_sim_time:=true` |
| 4 | **Topic 映射** | 仿真端发布 `/velodyne_points`，SLAM 使用 `velodyne_vlp16.yaml` 配置，默认 `lidar_topic: /velodyne_points`，无需重映射 |
| 5 | **IMU Topic** | 仿真 IMU 发布到 `/imu`，SLAM 启动时需设置 `imu_topic:=/imu`（默认是 `/ouster/imu`） |
| 6 | **坐标系配置** | SLAM `velodyne_vlp16.yaml` 中 `lidar_frame` 为 `"velodyne"`，仿真端 `frameName` 须设为 `"velodyne"`（不含 `_link`） |
| 7 | **推荐配置文件** | 使用 `velodyne_vlp16.yaml`，参数完美对齐仿真配置 |

### 8.4 FAST-LIO2 完整启动流程

```bash
# 终端 1: ROS1 仿真
source /opt/ros/noetic/setup.bash
source ~/rosiwit_ws/devel/setup.bash
roslaunch simulator simulator_gazebo_3d.launch

# 终端 2: ros1_bridge
source /opt/ros/noetic/setup.bash
source /opt/ros/humble/setup.bash
ros2 run ros1_bridge dynamic_bridge

# 终端 3: ROS2 FAST-LIO2
source /opt/ros/humble/setup.bash
source ~/rosiwit_ws/install/setup.bash
ros2 launch fast_lio2_slam fast_lio2.launch.py \
    config_file:=velodyne_vlp16.yaml \
    lidar_topic:=/velodyne_points \
    imu_topic:=/imu \
    use_sim_time:=true
```

---

## 9. 修改/新增文件清单

| # | 文件路径 | 操作 | 优先级 | 说明 |
|---|---------|------|--------|------|
| 1 | `urdf/xacro/sensors/lidar3d.xacro` | **新增** | P0 | 3D 雷达物理描述 (VLP-16 外形) |
| 2 | `urdf/xacro/sensors/lidar3d_gazebo.xacro` | **新增** | P0 | 3D 雷达 Gazebo GPU Ray 插件配置 |
| 3 | `urdf/xacro/gazebo/mbot_with_lidar3d_gazebo.xacro` | **新增** | P0 | 3D 雷达组合模型 (base+lidar3d+imu) |
| 4 | `launch/simulator_gazebo_3d.launch` | **新增** | P0 | 3D 仿真启动文件 |
| 5 | `rviz/simulator_3d.rviz` | **新增** | P1 | RViz 配置文件（方便验证） |

> **所有文件均为新增，不修改任何已有文件。** 这保证了原 2D 雷达仿真功能完全不受影响。

---

## 10. 风险评估

| # | 风险 | 概率 | 影响 | 缓解措施 |
|---|------|------|------|---------|
| R1 | GPU Ray 插件在某些环境不可用 | 中 | 高 | 提供 CPU `ray` 降级方案 (M2-alt) |
| R2 | 28800 点/帧 (1800×16) 性能压力大 | 低 | 中 | 可降低水平采样到 900 或 360，牺牲密度换性能 |
| R3 | ros1_bridge 延迟影响 SLAM 实时性 | 中 | 中 | bridge 在同机运行延迟 <10ms，可接受 |
| R4 | `frameName` 配置不一致导致 TF 断裂 | 低 | 高 | 文档明确说明，验证脚本检查 |
| R5 | `libgazebo_ros_ray_sensor.so` 版本不兼容 | 低 | 高 | 该插件在 Gazebo 9+ / ROS Noetic 标配 |

---

## 11. 传递给代码工程师的关键信息

| 维度 | 信息 |
|------|------|
| **实现策略** | 纯新增文件，零修改已有代码 |
| **参考模板** | `lidar_gazebo.xacro`（2D 雷达），在其基础上增加 vertical 扫描参数 |
| **Gazebo 插件** | 使用 `gpu_ray` sensor + `libgazebo_ros_ray_sensor.so`（注意不是 `libgazebo_ros_laser.so`） |
| **核心区别 2D→3D** | sensor type 从 `ray` 改为 `gpu_ray`；增加 `<vertical>` 扫描配置；插件改为 ray_sensor 并设置 `outputType: PointCloud2` |
| **坐标系约定** | `frameName` 设为 `"velodyne"`（无 `_link` 后缀），与 SLAM yaml 对齐 |
| **安装偏移** | z=0.1955m（base_link 顶面 + 传感器半高），与 2D 雷达的 z=0.105m 不冲突（3D 雷达替换位置更高） |
| **ROS 版本** | 当前为 ROS1 (catkin)，launch 用 XML 格式。文档中已标注 ROS2 迁移方案 |
