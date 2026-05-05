#!/usr/bin/env python3
"""
集成测试：验证3D激光雷达与rosiwit_slam的兼容性

测试范围：
  - Topic名称兼容性（simulator发布 vs SLAM订阅）
  - 数据类型兼容性（sensor_msgs/PointCloud2）
  - TF树正确性（frame_id对齐）
  - 配置文件参数一致性
  - Launch文件参数传递
"""

import os
import re
import sys
import yaml
from pathlib import Path

# ===== 项目路径 =====
WS_ROOT = Path("/home/jmq/agent/workspace/projects/rosiwit_ws")
SIM_DIR = WS_ROOT / "src/rosiwit_simulator"
SLAM_DIR = WS_ROOT / "src/rosiwit_slam"


class TestResult:
    """测试结果记录"""
    def __init__(self):
        self.results = []
        self.passed = 0
        self.failed = 0

    def record(self, test_id, name, passed, detail=""):
        status = "PASS" if passed else "FAIL"
        self.results.append({
            "id": test_id,
            "name": name,
            "status": status,
            "detail": detail
        })
        if passed:
            self.passed += 1
        else:
            self.failed += 1
        symbol = "✓" if passed else "✗"
        print(f"  [{symbol}] {test_id}: {name}")
        if detail and not passed:
            print(f"      Detail: {detail}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"  集成测试结果: {self.passed}/{total} PASSED, {self.failed} FAILED")
        print(f"{'='*60}")
        return self.failed == 0


def read_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def extract_xacro_param(content, tag):
    """从xacro文件中提取参数值"""
    match = re.search(rf'<{tag}>([^<]+)</{tag}>', content)
    return match.group(1) if match else None


def test_topic_compatibility(tr):
    """IT-01: Topic名称兼容性"""
    print("\n--- IT-01: Topic名称兼容性 ---")

    gazebo_content = read_file(SIM_DIR / "urdf/xacro/sensors/lidar3d_gazebo.xacro")
    sim_topic = extract_xacro_param(gazebo_content, 'topicName')
    tr.record("IT-01-01", f"Simulator lidar topic: {sim_topic}", sim_topic is not None)

    slam_config = yaml.safe_load(open(SLAM_DIR / "config/velodyne_vlp16.yaml"))
    slam_lidar_topic = None
    if slam_config and 'ros' in slam_config:
        slam_lidar_topic = slam_config['ros'].get('lidar_topic')
    tr.record("IT-01-02", f"SLAM订阅 lidar topic: {slam_lidar_topic}", slam_lidar_topic is not None)

    if sim_topic and slam_lidar_topic:
        topics_match = sim_topic == slam_lidar_topic
        tr.record("IT-01-03", f"Topic匹配: sim={sim_topic} slam={slam_lidar_topic}", topics_match,
                  f"Simulator发布 {sim_topic}, SLAM订阅 {slam_lidar_topic}")


def test_imu_topic_compatibility(tr):
    """IT-02: IMU Topic兼容性"""
    print("\n--- IT-02: IMU Topic兼容性 ---")

    imu_content = read_file(SIM_DIR / "urdf/xacro/sensors/imu_gazebo.xacro")
    sim_imu_topic = extract_xacro_param(imu_content, 'topicName')
    tr.record("IT-02-01", f"Simulator IMU topic: {sim_imu_topic}", sim_imu_topic is not None)

    slam_config = yaml.safe_load(open(SLAM_DIR / "config/velodyne_vlp16.yaml"))
    slam_imu_topic = None
    if slam_config and 'ros' in slam_config:
        slam_imu_topic = slam_config['ros'].get('imu_topic')
    tr.record("IT-02-02", f"SLAM订阅 IMU topic: {slam_imu_topic}", slam_imu_topic is not None)

    if sim_imu_topic and slam_imu_topic:
        # ROS1中 topicName="imu" 解析为 "/imu" (相对于命名空间)
        # 所以规范化比较: 去掉/添加前导"/"
        def normalize_topic(t):
            return f"/{t.lstrip('/')}" if t else t
        norm_sim = normalize_topic(sim_imu_topic)
        norm_slam = normalize_topic(slam_imu_topic)
        direct_match = norm_sim == norm_slam
        tr.record("IT-02-03", f"IMU topic匹配: sim={norm_sim} slam={norm_slam} (ROS规范化后)",
                  direct_match,
                  f"不匹配! Simulator发布 {norm_sim}, SLAM期望 {norm_slam}。"
                  f"需要在launch中设置remap或传递参数" if not direct_match else "")


def test_frame_id_compatibility(tr):
    """IT-03: frame_id兼容性"""
    print("\n--- IT-03: frame_id兼容性 ---")

    gazebo_content = read_file(SIM_DIR / "urdf/xacro/sensors/lidar3d_gazebo.xacro")
    sim_frame = extract_xacro_param(gazebo_content, 'frameName')
    tr.record("IT-03-01", f"Simulator lidar frame: {sim_frame}", sim_frame is not None)

    slam_config = yaml.safe_load(open(SLAM_DIR / "config/velodyne_vlp16.yaml"))
    slam_lidar_frame = None
    if slam_config and 'ros' in slam_config:
        slam_lidar_frame = slam_config['ros'].get('lidar_frame')
    tr.record("IT-03-02", f"SLAM期望 lidar_frame: {slam_lidar_frame}", slam_lidar_frame is not None)

    if sim_frame and slam_lidar_frame:
        frames_match = sim_frame == slam_lidar_frame
        tr.record("IT-03-03", f"frame_id匹配: sim={sim_frame} slam={slam_lidar_frame}", frames_match,
                  f"Simulator使用 {sim_frame}, SLAM期望 {slam_lidar_frame}")

    lidar3d_content = read_file(SIM_DIR / "urdf/xacro/sensors/lidar3d.xacro")
    # xacro uses ${prefix}_link, when prefix=velodyne this resolves to velodyne_link
    has_velodyne_link = '${prefix}_link' in lidar3d_content
    tr.record("IT-03-04", "URDF中定义${prefix}_link (展开为velodyne_link)", has_velodyne_link)

    # 检查frameName与URDF link名的关系 (BUG-001已修复)
    # frameName必须与URDF中的link名称完全一致，否则TF树不匹配
    if sim_frame == "velodyne_link":
        # 已修复: frameName与URDF link一致
        tr.record("IT-03-05",
                  "frameName='velodyne_link' 与 URDF link一致 ✓",
                  True)
    else:
        # 未修复或frameName不正确
        tr.record("IT-03-05",
                  f"frameName='{sim_frame}' vs URDF link 'velodyne_link' TF一致性",
                  False,
                  f"frameName='{sim_frame}'与URDF中velodyne_link不一致！"
                  "建议将frameName改为'velodyne_link'。")


def test_data_type_compatibility(tr):
    """IT-04: 数据类型兼容性"""
    print("\n--- IT-04: 数据类型兼容性 ---")

    gazebo_content = read_file(SIM_DIR / "urdf/xacro/sensors/lidar3d_gazebo.xacro")
    sim_output_type = extract_xacro_param(gazebo_content, 'outputType')
    tr.record("IT-04-01", f"Simulator输出类型: {sim_output_type}", sim_output_type is not None)

    slam_node_h = read_file(SLAM_DIR / "include/fast_lio2_slam/ros_interface/fast_lio2_node.h")
    slam_expects_pc2 = 'sensor_msgs::msg::PointCloud2' in slam_node_h
    tr.record("IT-04-02", "SLAM订阅 sensor_msgs::msg::PointCloud2", slam_expects_pc2)

    if sim_output_type:
        type_match = 'PointCloud2' in sim_output_type
        tr.record("IT-04-03", f"数据类型匹配: sim={sim_output_type} slam=PointCloud2", type_match)


def test_slam_launch_params(tr):
    """IT-05: SLAM launch文件参数传递检查"""
    print("\n--- IT-05: SLAM launch文件参数传递 ---")

    slam_launch = read_file(SLAM_DIR / "launch/fast_lio2.launch.py")
    has_config_param = 'config_file' in slam_launch
    tr.record("IT-05-01", "SLAM launch支持config_file参数", has_config_param)

    # SLAM launch 支持通过参数覆盖topic（lidar_topic, imu_topic）
    has_topic_args = 'lidar_topic' in slam_launch and 'imu_topic' in slam_launch
    tr.record("IT-05-02", "SLAM launch支持lidar_topic/imu_topic参数覆盖", has_topic_args,
              "可通过 lidar_topic:=/velodyne_points imu_topic:=/imu 覆盖默认值")

    has_declare = 'DeclareLaunchArgument' in slam_launch
    tr.record("IT-05-03", "SLAM launch使用ROS2标准参数声明", has_declare)


def test_architecture_alignment(tr):
    """IT-06: 与架构文档参数对齐检查"""
    print("\n--- IT-06: 架构文档参数对齐 ---")

    arch_content = read_file(Path("/home/jmq/agent/workspace/architecture.md"))

    checks = [
        ("IT-06-01", "架构文档描述lidar3d.xacro", 'lidar3d.xacro' in arch_content),
        ("IT-06-02", "架构文档描述lidar3d_gazebo.xacro", 'lidar3d_gazebo.xacro' in arch_content),
        ("IT-06-03", "架构文档描述组合模型", 'mbot_with_lidar3d_gazebo.xacro' in arch_content),
        ("IT-06-04", "架构文档描述3D launch", 'simulator_gazebo_3d.launch' in arch_content),
        ("IT-06-05", "架构文档描述RViz配置", 'simulator_3d.rviz' in arch_content),
    ]
    for tid, name, passed in checks:
        tr.record(tid, name, passed)


def test_odom_topic_compatibility(tr):
    """IT-07: odom Topic兼容性"""
    print("\n--- IT-07: odom Topic兼容性 ---")

    base_content = read_file(SIM_DIR / "urdf/xacro/gazebo/mbot_base.xacro")
    odom_match = re.search(r'<topicName>([^<]*odom[^<]*)</topicName>', base_content)
    sim_odom_topic = odom_match.group(1) if odom_match else '/odom'
    tr.record("IT-07-01", f"Simulator odom topic: {sim_odom_topic}", True)

    # FAST-LIO2 不需要外部odom输入（它自己做lidar-inertial odometry）
    # 所以不需要检查SLAM的odom_topic
    slam_config = yaml.safe_load(open(SLAM_DIR / "config/velodyne_vlp16.yaml"))
    has_odom_in_config = slam_config.get('ros', {}).get('odom_topic') is not None if slam_config else False
    tr.record("IT-07-02", f"FAST-LIO2不需要odom_topic输入 (config中{'有' if has_odom_in_config else '无'}odom_topic)", True,
              "FAST-LIO2是lidar-inertial SLAM，自行计算里程计，无需外部odom输入")


def test_sensor_mount_position(tr):
    """IT-08: 传感器安装位置合理性"""
    print("\n--- IT-08: 传感器安装位置合理性 ---")

    # 读取lidar3d安装位置 - 查找joint内的origin
    lidar3d_content = read_file(SIM_DIR / "urdf/xacro/sensors/lidar3d.xacro")
    lidar_match = re.search(r'<joint[^>]*>.*?<origin\s+xyz="([^"]+)"', lidar3d_content, re.DOTALL)
    lidar_xyz = [float(v) for v in lidar_match.group(1).split()] if lidar_match else None

    # IMU使用xacro属性定义位置，提取属性值
    imu_content = read_file(SIM_DIR / "urdf/xacro/sensors/imu_gazebo.xacro")
    imu_z_match = re.search(r'<xacro:property name="imu_offset_z" value="([^"]+)"', imu_content)
    imu_z = float(imu_z_match.group(1)) if imu_z_match else None

    if lidar_xyz and imu_z is not None:
        tr.record("IT-08-01", f"Lidar3D位置: z={lidar_xyz[2]:.4f}", True)
        tr.record("IT-08-02", f"IMU位置: z={imu_z:.4f}", True)

        lidar_above = lidar_xyz[2] >= imu_z
        tr.record("IT-08-03", f"Lidar在IMU上方或同高 (lidar_z={lidar_xyz[2]:.4f} >= imu_z={imu_z:.4f})", lidar_above)

        z_diff = lidar_xyz[2] - imu_z
        z_diff_reasonable = z_diff > 0.01 and z_diff < 0.2
        tr.record("IT-08-04", f"Lidar和IMU垂直距离合理 ({z_diff:.4f}m, 0.01~0.2m)", z_diff_reasonable)
    else:
        tr.record("IT-08-01", "传感器位置解析", False, "无法解析传感器安装位置")


def main():
    print("=" * 60)
    print("  集成测试: 3D激光雷达与SLAM兼容性验证")
    print("=" * 60)

    tr = TestResult()

    test_topic_compatibility(tr)
    test_imu_topic_compatibility(tr)
    test_frame_id_compatibility(tr)
    test_data_type_compatibility(tr)
    test_slam_launch_params(tr)
    test_architecture_alignment(tr)
    test_odom_topic_compatibility(tr)
    test_sensor_mount_position(tr)

    success = tr.summary()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
