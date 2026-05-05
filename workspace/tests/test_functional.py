#!/usr/bin/env python3
"""
功能测试：逐条验证需求文档验收标准

测试策略：
  对照 requirements.md 中的每条验收标准进行验证
  检查每个AC（Acceptance Criteria）的满足情况
"""

import os
import re
import sys
from pathlib import Path

# ===== 项目路径 =====
WS_ROOT = Path("/home/jmq/agent/workspace/projects/rosiwit_ws")
SIM_DIR = WS_ROOT / "src/rosiwit_simulator"
SLAM_DIR = WS_ROOT / "src/rosiwit_slam"

class TestResult:
    def __init__(self):
        self.results = []
        self.passed = 0
        self.failed = 0

    def record(self, test_id, name, passed, detail=""):
        status = "PASS" if passed else "FAIL"
        self.results.append({"id": test_id, "name": name, "status": status, "detail": detail})
        if passed:
            self.passed += 1
        else:
            self.failed += 1
        symbol = "✓" if passed else "✗"
        print(f"  [{symbol}] {test_id}: {name}")
        if detail:
            print(f"      {detail}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"  功能测试结果: {self.passed}/{total} PASSED, {self.failed} FAILED")
        print(f"{'='*60}")
        return self.failed == 0


def read_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def extract_param(content, tag):
    match = re.search(rf'<{tag}>([^<]+)</{tag}>', content)
    return match.group(1) if match else None


# ============================================================
# 验收标准测试
# ============================================================

def test_ac1_3d_lidar_model_added(tr):
    """AC-1: URDF/Xacro模型中添加3D激光雷达"""
    print("\n--- AC-1: 3D激光雷达模型已添加到URDF ---")

    # lidar3d.xacro 存在
    lidar3d_path = SIM_DIR / "urdf/xacro/sensors/lidar3d.xacro"
    tr.record("AC1-01", "lidar3d.xacro 文件存在", lidar3d_path.exists())

    if lidar3d_path.exists():
        content = read_file(lidar3d_path)
        # xacro uses ${prefix}_link which resolves to velodyne_link when prefix=velodyne
        has_link = '${prefix}_link' in content
        has_joint = '${prefix}_joint' in content
        has_visual = '<visual>' in content
        has_collision = '<collision>' in content
        has_inertial = '<inertial>' in content

        tr.record("AC1-02", "定义${prefix}_link (展开后velodyne_link)", has_link)
        tr.record("AC1-03", "定义${prefix}_joint (展开后velodyne_joint, fixed)", has_joint and 'type="fixed"' in content)
        tr.record("AC1-04", "包含visual定义", has_visual)
        tr.record("AC1-05", "包含collision定义", has_collision)
        tr.record("AC1-06", "包含inertial定义", has_inertial)

        # 检查外形为VLP-16
        has_cylinder = '<cylinder' in content
        tr.record("AC1-07", "使用圆柱体几何(VLP-16外形)", has_cylinder)


def test_ac2_gazebo_plugin_configured(tr):
    """AC-2: Gazebo插件配置发布PointCloud2数据"""
    print("\n--- AC-2: Gazebo插件配置正确 ---")

    path = SIM_DIR / "urdf/xacro/sensors/lidar3d_gazebo.xacro"
    tr.record("AC2-01", "lidar3d_gazebo.xacro 文件存在", path.exists())

    if path.exists():
        content = read_file(path)

        # 插件类型
        has_plugin = 'libgazebo_ros_ray_sensor.so' in content
        tr.record("AC2-02", "使用libgazebo_ros_ray_sensor.so插件", has_plugin)

        # 输出类型
        has_pc2 = 'sensor_msgs/PointCloud2' in content
        tr.record("AC2-03", "输出类型为sensor_msgs/PointCloud2", has_pc2)

        # topic名称
        topic = extract_param(content, 'topicName')
        tr.record("AC2-04", f"topic名称已设置: {topic}", topic is not None)

        # 传感器类型
        has_gpu_ray = 'type="gpu_ray"' in content
        tr.record("AC2-05", "sensor类型为gpu_ray", has_gpu_ray)

        # frameName
        frame = extract_param(content, 'frameName')
        tr.record("AC2-06", f"frameName已设置: {frame}", frame is not None)


def test_ac3_sensor_parameters(tr):
    """AC-3: 传感器参数合理"""
    print("\n--- AC-3: 传感器参数合理性 ---")

    path = SIM_DIR / "urdf/xacro/sensors/lidar3d_gazebo.xacro"
    content = read_file(path)

    # VLP-16参数验证
    checks = [
        ("AC3-01", "扫描频率10Hz", '<update_rate>10</update_rate>' in content),
        ("AC3-02", "水平线数1800", '<samples>1800</samples>' in content),
        ("AC3-03", "垂直线数16", '<samples>16</samples>' in content),
        ("AC3-04", "水平角度360度(±π)", '-3.14159' in content and '3.14159' in content),
        ("AC3-05", "垂直角度±15°(±0.2618rad)", '-0.2618' in content and '0.2618' in content),
        ("AC3-06", "最小测距0.5m", '<min>0.5</min>' in content),
        ("AC3-07", "最大测距100m", '<max>100.0</max>' in content),
        ("AC3-08", "噪声配置(stddev=0.01)", '<stddev>0.01</stddev>' in content),
    ]
    for tid, name, passed in checks:
        tr.record(tid, name, passed)


def test_ac4_tf_tree_correct(tr):
    """AC-4: TF树正确"""
    print("\n--- AC-4: TF树正确性 ---")

    # 检查URDF中joint定义确保TF链完整
    lidar3d_content = read_file(SIM_DIR / "urdf/xacro/sensors/lidar3d.xacro")

    # base_link -> velodyne_link 的 TF 通过 joint 发布
    parent_match = re.search(r'<parent link="([^"]+)"', lidar3d_content)
    child_match = re.search(r'<child link="([^"]+)"', lidar3d_content)

    if parent_match and child_match:
        parent = parent_match.group(1)
        child = child_match.group(1)
        tr.record("AC4-01", f"TF: {parent} -> {child}", True)

        # parent应该是base_link
        is_base = parent == 'base_link'
        tr.record("AC4-02", f"父帧为base_link", is_base)
    else:
        tr.record("AC4-01", "TF链解析", False, "未找到joint定义")

    # 检查robot_state_publisher配置
    launch_content = read_file(SIM_DIR / "launch/simulator_gazebo_3d.launch")
    has_rsp = 'robot_state_publisher' in launch_content
    tr.record("AC4-03", "launch包含robot_state_publisher", has_rsp)

    has_sim_time = 'use_sim_time' in launch_content
    tr.record("AC4-04", "使用sim_time同步TF", has_sim_time)


def test_ac5_topic_compatibility_with_slam(tr):
    """AC-5: Topic与SLAM兼容"""
    print("\n--- AC-5: Topic与SLAM兼容 ---")

    import yaml

    gazebo_content = read_file(SIM_DIR / "urdf/xacro/sensors/lidar3d_gazebo.xacro")
    sim_topic = extract_param(gazebo_content, 'topicName')

    slam_config = yaml.safe_load(open(SLAM_DIR / "config/velodyne_vlp16.yaml"))
    slam_topic = slam_config.get('ros', {}).get('lidar_topic')

    tr.record("AC5-01", f"Simulator发布: {sim_topic}", sim_topic is not None)
    tr.record("AC5-02", f"SLAM订阅: {slam_topic}", slam_topic is not None)
    tr.record("AC5-03", f"Topic名称一致", sim_topic == slam_topic,
              f"sim={sim_topic}, slam={slam_topic}" if sim_topic != slam_topic else "")

    # 检查数据类型
    sim_type = extract_param(gazebo_content, 'outputType')
    tr.record("AC5-04", f"数据类型: {sim_type}", sim_type is not None and 'PointCloud2' in sim_type)


def test_ac6_launch_file_functional(tr):
    """AC-6: Launch文件可正常使用"""
    print("\n--- AC-6: Launch文件功能完整性 ---")

    launch_path = SIM_DIR / "launch/simulator_gazebo_3d.launch"
    tr.record("AC6-01", "simulator_gazebo_3d.launch 存在", launch_path.exists())

    if launch_path.exists():
        content = read_file(launch_path)

        has_gazebo = 'gazebo' in content.lower()
        tr.record("AC6-02", "启动Gazebo仿真器", has_gazebo)

        has_3d_model = 'mbot_with_lidar3d_gazebo' in content
        tr.record("AC6-03", "加载3D激光雷达模型", has_3d_model)

        has_world = '.world' in content
        tr.record("AC6-04", "指定world文件", has_world)

        has_spawn = 'spawn_model' in content
        tr.record("AC6-05", "spawn机器人模型到Gazebo", has_spawn)

        has_rsp = 'robot_state_publisher' in content
        tr.record("AC6-06", "启动robot_state_publisher", has_rsp)

        has_jsp = 'joint_state_publisher' in content
        tr.record("AC6-07", "启动joint_state_publisher", has_jsp)

        has_rviz = 'rviz' in content.lower()
        tr.record("AC6-08", "启动RViz可视化", has_rviz)


def test_ac7_rviz_config(tr):
    """AC-7: RViz配置包含3D激光雷达显示"""
    print("\n--- AC-7: RViz配置检查 ---")

    rviz_path = SIM_DIR / "rviz/simulator_3d.rviz"
    tr.record("AC7-01", "simulator_3d.rviz 存在", rviz_path.exists())

    if rviz_path.exists():
        content = read_file(rviz_path)

        has_pc2 = 'PointCloud2' in content
        tr.record("AC7-02", "配置PointCloud2显示", has_pc2)

        has_velodyne = '/velodyne_points' in content
        tr.record("AC7-03", "PointCloud2话题为/velodyne_points", has_velodyne)

        has_robot_model = 'RobotModel' in content
        tr.record("AC7-04", "配置RobotModel显示", has_robot_model)

        has_tf = '/tf' in content or 'TF' in content
        tr.record("AC7-05", "配置TF显示", has_tf)


def test_ac8_2d_lidar_not_affected(tr):
    """AC-8: 原有2D激光雷达功能不受影响"""
    print("\n--- AC-8: 2D激光雷达回归检查 ---")

    # 原有文件未修改
    files_unchanged = [
        ("AC8-01", "2D lidar.xacro存在", SIM_DIR / "urdf/xacro/sensors/lidar.xacro"),
        ("AC8-02", "2D lidar_gazebo.xacro存在", SIM_DIR / "urdf/xacro/sensors/lidar_gazebo.xacro"),
        ("AC8-03", "mbot_with_laser_gazebo.xacro存在", SIM_DIR / "urdf/xacro/gazebo/mbot_with_laser_gazebo.xacro"),
        ("AC8-04", "simulator_gazebo.launch存在", SIM_DIR / "launch/simulator_gazebo.launch"),
    ]
    for tid, name, path in files_unchanged:
        tr.record(tid, name, path.exists())

    # 检查原有launch文件仍加载2D模型
    launch_2d = read_file(SIM_DIR / "launch/simulator_gazebo.launch")
    has_2d_model = 'mbot_with_laser_gazebo' in launch_2d
    tr.record("AC8-05", "2D launch仍加载2D模型", has_2d_model)

    # 检查3D launch不加载2D模型
    launch_3d = read_file(SIM_DIR / "launch/simulator_gazebo_3d.launch")
    no_2d_in_3d = 'mbot_with_laser_gazebo' not in launch_3d
    tr.record("AC8-06", "3D launch不加载2D模型", no_2d_in_3d)


def test_ac9_documentation(tr):
    """AC-9: 配置参数文档说明"""
    print("\n--- AC-9: 文档完整性检查 ---")

    # 检查README或文档是否存在
    readme_path = SIM_DIR / "README.md"
    tr.record("AC9-01", "README.md存在", readme_path.exists())

    # 检查launch文件中有足够的注释
    launch_content = read_file(SIM_DIR / "launch/simulator_gazebo_3d.launch")
    has_comments = '<!--' in launch_content or '#' in launch_content
    tr.record("AC9-02", "Launch文件包含注释", has_comments)

    # 检查xacro文件中有注释
    lidar3d_content = read_file(SIM_DIR / "urdf/xacro/sensors/lidar3d.xacro")
    has_xacro_comments = '<!--' in lidar3d_content
    tr.record("AC9-03", "lidar3d.xacro包含注释", has_xacro_comments)

    gazebo_content = read_file(SIM_DIR / "urdf/xacro/sensors/lidar3d_gazebo.xacro")
    has_gazebo_comments = '<!--' in gazebo_content
    tr.record("AC9-04", "lidar3d_gazebo.xacro包含注释", has_gazebo_comments)


def main():
    print("=" * 60)
    print("  功能测试: 逐条验证需求验收标准")
    print("=" * 60)

    tr = TestResult()

    test_ac1_3d_lidar_model_added(tr)
    test_ac2_gazebo_plugin_configured(tr)
    test_ac3_sensor_parameters(tr)
    test_ac4_tf_tree_correct(tr)
    test_ac5_topic_compatibility_with_slam(tr)
    test_ac6_launch_file_functional(tr)
    test_ac7_rviz_config(tr)
    test_ac8_2d_lidar_not_affected(tr)
    test_ac9_documentation(tr)

    success = tr.summary()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
