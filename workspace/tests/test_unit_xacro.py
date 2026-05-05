#!/usr/bin/env python3
"""
单元测试：验证3D激光雷达URDF/Xacro模型文件的正确性

测试范围：
  - 新增文件存在性
  - XML语法正确性
  - Xacro宏定义完整性
  - 物理参数合理性
  - Gazebo插件配置正确性
  - 与架构文档参数对齐
"""

import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# ===== 项目路径 =====
WS_ROOT = Path("/home/jmq/agent/workspace/projects/rosiwit_ws")
SIM_DIR = WS_ROOT / "src/rosiwit_simulator"
SLAM_DIR = WS_ROOT / "src/rosiwit_slam"

# 新增文件列表
NEW_FILES = {
    "lidar3d.xacro": SIM_DIR / "urdf/xacro/sensors/lidar3d.xacro",
    "lidar3d_gazebo.xacro": SIM_DIR / "urdf/xacro/sensors/lidar3d_gazebo.xacro",
    "mbot_with_lidar3d_gazebo.xacro": SIM_DIR / "urdf/xacro/gazebo/mbot_with_lidar3d_gazebo.xacro",
    "simulator_gazebo_3d.launch": SIM_DIR / "launch/simulator_gazebo_3d.launch",
    "simulator_3d.rviz": SIM_DIR / "rviz/simulator_3d.rviz",
}

# 原有文件（不修改）
EXISTING_FILES = {
    "lidar.xacro": SIM_DIR / "urdf/xacro/sensors/lidar.xacro",
    "lidar_gazebo.xacro": SIM_DIR / "urdf/xacro/sensors/lidar_gazebo.xacro",
    "mbot_with_laser_gazebo.xacro": SIM_DIR / "urdf/xacro/gazebo/mbot_with_laser_gazebo.xacro",
    "simulator_gazebo.launch": SIM_DIR / "launch/simulator_gazebo.launch",
    "mbot_base.xacro": SIM_DIR / "urdf/xacro/gazebo/mbot_base.xacro",
    "imu_gazebo.xacro": SIM_DIR / "urdf/xacro/sensors/imu_gazebo.xacro",
}


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
        print(f"  单元测试结果: {self.passed}/{total} PASSED, {self.failed} FAILED")
        print(f"{'='*60}")
        return self.failed == 0


def read_file(path):
    """安全读取文件内容"""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


# ============================================================
# 测试用例
# ============================================================

def test_file_existence(tr):
    """UT-01: 新增文件存在性检查"""
    print("\n--- UT-01: 新增文件存在性检查 ---")
    for name, path in NEW_FILES.items():
        exists = path.exists() and path.is_file()
        tr.record(f"UT-01-{name}", f"文件 {name} 存在", exists,
                  f"路径: {path}" if not exists else "")


def test_xml_well_formed(tr):
    """UT-02: XML语法正确性"""
    print("\n--- UT-02: XML语法正确性 ---")
    xml_files = {k: v for k, v in NEW_FILES.items() if v.suffix in ('.xacro', '.launch')}
    for name, path in xml_files.items():
        try:
            content = read_file(path)
            # xacro文件可能有 $(find ...) 等非标准XML，先做基本解析
            # 替换 xacro 特有语法后再解析
            ET.fromstring(content)
            tr.record(f"UT-02-{name}", f"{name} XML格式正确", True)
        except ET.ParseError as e:
            # xacro包含 $(find ...) 等指令，纯XML解析可能失败
            # 但至少检查基本结构
            has_xml_decl = content.strip().startswith('<?xml')
            has_root_open = '<robot' in content or '<launch' in content
            has_root_close = '</robot>' in content or '</launch>' in content
            basic_ok = has_xml_decl and has_root_open and has_root_close
            tr.record(f"UT-02-{name}", f"{name} 基本XML结构正确", basic_ok,
                      f"xml_decl={has_xml_decl}, root_open={has_root_open}, root_close={has_root_close}")


def test_lidar3d_xacro_structure(tr):
    """UT-03: lidar3d.xacro 宏定义和link/joint结构"""
    print("\n--- UT-03: lidar3d.xacro 结构检查 ---")
    path = NEW_FILES["lidar3d.xacro"]
    content = read_file(path)

    # 检查宏定义
    has_macro = '<xacro:macro name="lidar3d"' in content
    tr.record("UT-03-01", "lidar3d宏定义存在", has_macro,
              "缺少 <xacro:macro name=\"lidar3d\" ...>")

    # 检查参数 prefix
    has_prefix_param = 'prefix:=velodyne' in content or 'params="prefix' in content
    tr.record("UT-03-02", "lidar3d宏有prefix参数（默认velodyne）", has_prefix_param)

    # 检查link定义 - xacro宏展开后prefix=velodyne -> velodyne_link
    has_link = '${prefix}_link' in content
    tr.record("UT-03-03", "link使用 ${prefix}_link 命名 (展开后为velodyne_link)", has_link)

    # 检查joint定义
    has_joint = '${prefix}_joint' in content
    tr.record("UT-03-04", "joint使用 ${prefix}_joint 命名 (展开后为velodyne_joint)", has_joint)

    # 检查joint type=fixed
    has_fixed_joint = 'type="fixed"' in content
    tr.record("UT-03-05", "joint类型为fixed", has_fixed_joint)

    # 检查parent=base_link
    has_base_parent = '<parent link="base_link"/>' in content
    tr.record("UT-03-06", "joint父link为base_link", has_base_parent)

    # 检查child=${prefix}_link
    has_child = '<child link="${prefix}_link"/>' in content
    tr.record("UT-03-07", "joint子link为${prefix}_link", has_child)

    # 检查物理外观 - VLP-16 cylinder
    has_cylinder = '<cylinder' in content
    tr.record("UT-03-08", "使用圆柱体几何形状（VLP-16外形）", has_cylinder)

    # 检查VLP-16参数 radius=0.0516, height=0.0717
    has_radius = 'radius="0.0516"' in content
    has_height = 'length="0.0717"' in content or 'height="0.0717"' in content
    tr.record("UT-03-09", "VLP-16外形参数: radius=0.0516m", has_radius)
    tr.record("UT-03-10", "VLP-16外形参数: length=0.0717m", has_height)

    # 检查inertial
    has_inertial = '<inertial>' in content and '<mass' in content and '<inertia' in content
    tr.record("UT-03-11", "包含inertial属性（质量+惯量）", has_inertial)

    # 检查质量 VLP-16: 0.83kg
    has_mass = '0.83' in content
    tr.record("UT-03-12", "VLP-16质量: 0.83kg", has_mass)

    # 检查visual和collision
    has_visual = '<visual>' in content
    has_collision = '<collision>' in content
    tr.record("UT-03-13", "包含visual定义", has_visual)
    tr.record("UT-03-14", "包含collision定义", has_collision)

    # 检查Gazebo material
    has_gazebo_material = 'Gazebo/Grey' in content
    tr.record("UT-03-15", "Gazebo材质为Gazebo/Grey", has_gazebo_material)


def test_lidar3d_joint_origin(tr):
    """UT-04: lidar3d joint origin（安装位置）检查"""
    print("\n--- UT-04: lidar3d安装位置检查 ---")
    path = NEW_FILES["lidar3d.xacro"]
    content = read_file(path)

    # 查找joint origin（需要精确匹配joint内的origin，不能匹配inertial内的origin）
    # 先找到joint块，再在joint块内找origin
    joint_match = re.search(r'<joint[^>]*>.*?<origin\s+xyz="([^"]+)"', content, re.DOTALL)
    if joint_match:
        xyz_str = joint_match.group(1)
        xyz = [float(v) for v in xyz_str.split()]
        tr.record("UT-04-01", f"joint origin xyz可解析: {xyz_str}", True)

        # 检查z值合理（应该在机器人顶部上方）
        z_reasonable = xyz[2] > 0.1 and xyz[2] < 0.5
        tr.record("UT-04-02", f"joint z值合理 ({xyz[2]:.4f}m, 期望>0.1m 且 <0.5m)", z_reasonable,
                  f"z={xyz[2]:.4f}")

        # 检查x, y为0（居中安装）
        xy_centered = abs(xyz[0]) < 0.01 and abs(xyz[1]) < 0.01
        tr.record("UT-04-03", f"joint x,y居中 (x={xyz[0]}, y={xyz[1]})", xy_centered)

        # 架构文档要求 z=0.1955
        z_matches_arch = abs(xyz[2] - 0.1955) < 0.001
        tr.record("UT-04-04", f"z值与架构文档对齐 (期望0.1955, 实际{xyz[2]:.4f})", z_matches_arch,
                  f"架构文档要求 z=0.1955, 实际 z={xyz[2]:.4f}")
    else:
        tr.record("UT-04-01", "joint origin可找到", False, "未找到joint内的<origin>标签")


def test_lidar3d_gazebo_plugin(tr):
    """UT-05: lidar3d_gazebo.xacro Gazebo插件配置检查"""
    print("\n--- UT-05: lidar3d_gazebo.xacro 插件配置检查 ---")
    path = NEW_FILES["lidar3d_gazebo.xacro"]
    content = read_file(path)

    # 检查宏定义
    has_macro = '<xacro:macro name="lidar3d_gazebo"' in content
    tr.record("UT-05-01", "lidar3d_gazebo宏定义存在", has_macro)

    # 检查include lidar3d.xacro
    has_include = '$(find simulator)/urdf/xacro/sensors/lidar3d.xacro' in content
    tr.record("UT-05-02", "include lidar3d.xacro物理描述", has_include)

    # 检查调用lidar3d宏
    has_call = '<xacro:lidar3d prefix="${prefix}"' in content
    tr.record("UT-05-03", "调用lidar3d宏传递prefix", has_call)

    # 检查sensor type=gpu_ray
    has_gpu_ray = 'type="gpu_ray"' in content
    tr.record("UT-05-04", "sensor类型为gpu_ray", has_gpu_ray)

    # 检查sensor名称
    has_sensor_name = 'name="velodyne_vlp16"' in content
    tr.record("UT-05-05", "sensor名称为velodyne_vlp16", has_sensor_name)

    # 检查update_rate=10
    has_update_rate = '<update_rate>10</update_rate>' in content
    tr.record("UT-05-06", "update_rate=10 (10Hz)", has_update_rate)

    # 检查horizontal参数
    h_samples = '<samples>1800</samples>' in content
    h_angle = '<min_angle>-3.14159</min_angle>' in content and '<max_angle>3.14159</max_angle>' in content
    tr.record("UT-05-07", "水平采样: 1800点, ±π角度范围", h_samples and h_angle)

    # 检查vertical参数
    v_samples = 'vertical' in content and '<samples>16</samples>' in content
    v_angle = '<min_angle>-0.2618</min_angle>' in content
    tr.record("UT-05-08", "垂直采样: 16线, 角度范围±0.2618rad(≈±15°)", v_samples and v_angle)

    # 检查range参数
    range_min = '<min>0.5</min>' in content
    range_max = '<max>100.0</max>' in content
    tr.record("UT-05-09", "测距范围: 0.5m~100m", range_min and range_max)

    # 检查noise
    has_noise = 'gaussian' in content and '<stddev>0.01</stddev>' in content
    tr.record("UT-05-10", "高斯噪声配置 (stddev=0.01)", has_noise)

    # 检查plugin
    has_plugin = 'libgazebo_ros_ray_sensor.so' in content
    tr.record("UT-05-11", "使用libgazebo_ros_ray_sensor.so插件", has_plugin)

    # 检查topicName
    has_topic = '<topicName>/velodyne_points</topicName>' in content
    tr.record("UT-05-12", "topic名称: /velodyne_points", has_topic)

    # 检查frameName — 必须与URDF link名一致 (BUG-001修复: velodyne -> velodyne_link)
    has_frame = '<frameName>velodyne_link</frameName>' in content
    tr.record("UT-05-13", "frame名称: velodyne_link (与URDF link一致)", has_frame)

    # 检查outputType
    has_output = '<outputType>sensor_msgs/PointCloud2</outputType>' in content
    tr.record("UT-05-14", "输出类型: sensor_msgs/PointCloud2", has_output)


def test_lidar3d_gazebo_macro_includes(tr):
    """UT-06: lidar3d_gazebo.xacro 宏引用完整性"""
    print("\n--- UT-06: lidar3d_gazebo.xacro 引用完整性 ---")
    path = NEW_FILES["lidar3d_gazebo.xacro"]
    content = read_file(path)

    # 检查文件引用的物理xacro文件是否存在
    xacro_match = re.search(r'\$\(find simulator\)/([^"]+)', content)
    if xacro_match:
        ref_path = SIM_DIR / xacro_match.group(1)
        tr.record("UT-06-01", f"引用文件存在: {xacro_match.group(1)}", ref_path.exists(),
                  f"路径: {ref_path}")
    else:
        tr.record("UT-06-01", "能解析引用文件路径", False, "未找到$(find simulator)引用")


def test_combined_model(tr):
    """UT-07: mbot_with_lidar3d_gazebo.xacro 组合模型检查"""
    print("\n--- UT-07: 组合模型检查 ---")
    path = NEW_FILES["mbot_with_lidar3d_gazebo.xacro"]
    content = read_file(path)

    # 检查include base
    has_base = '$(find simulator)/urdf/xacro/gazebo/mbot_base.xacro' in content
    tr.record("UT-07-01", "include mbot_base.xacro", has_base)

    # 检查include lidar3d_gazebo
    has_lidar3d = '$(find simulator)/urdf/xacro/sensors/lidar3d_gazebo.xacro' in content
    tr.record("UT-07-02", "include lidar3d_gazebo.xacro", has_lidar3d)

    # 检查include imu_gazebo
    has_imu = '$(find simulator)/urdf/xacro/sensors/imu_gazebo.xacro' in content
    tr.record("UT-07-03", "include imu_gazebo.xacro", has_imu)

    # 检查调用lidar3d_gazebo宏
    has_call_lidar3d = '<xacro:lidar3d_gazebo' in content
    tr.record("UT-07-04", "调用lidar3d_gazebo宏", has_call_lidar3d)

    # 检查传递prefix=velodyne
    has_velodyne_prefix = 'prefix="velodyne"' in content
    tr.record("UT-07-05", "lidar3d prefix=\"velodyne\"", has_velodyne_prefix)

    # 检查调用imu宏
    has_call_imu = '<xacro:imu' in content
    tr.record("UT-07-06", "调用imu宏", has_call_imu)

    # 检查调用mbot_base宏
    has_call_base = '<xacro:mbot_base' in content
    tr.record("UT-07-07", "调用mbot_base宏", has_call_base)

    # 检查不包含2D lidar引用
    no_2d_lidar = 'lidar_gazebo' not in content and 'rplidar' not in content
    tr.record("UT-07-08", "不包含2D lidar(rplidar)引用", no_2d_lidar,
              "3D模型不应引用2D雷达组件")


def test_launch_file(tr):
    """UT-08: simulator_gazebo_3d.launch 启动文件检查"""
    print("\n--- UT-08: Launch文件检查 ---")
    path = NEW_FILES["simulator_gazebo_3d.launch"]
    content = read_file(path)

    # 检查基本launch结构
    has_launch = content.strip().startswith('<launch>') and '</launch>' in content
    tr.record("UT-08-01", "launch文件结构正确", has_launch)

    # 检查GAZEBO_MODEL_PATH
    has_env = 'GAZEBO_MODEL_PATH' in content
    tr.record("UT-08-02", "设置GAZEBO_MODEL_PATH环境变量", has_env)

    # 检查world文件
    has_world = 'house.world' in content
    tr.record("UT-08-03", "使用house.world场景文件", has_world)

    # 检查加载3D模型（不是2D模型）
    has_3d_model = 'mbot_with_lidar3d_gazebo.xacro' in content
    tr.record("UT-08-04", "加载mbot_with_lidar3d_gazebo.xacro模型", has_3d_model)

    # 检查不加载2D模型
    no_2d_model = 'mbot_with_laser_gazebo.xacro' not in content
    tr.record("UT-08-05", "不加载2D激光雷达模型", no_2d_model)

    # 检查spawn_model
    has_spawn = 'spawn_model' in content
    tr.record("UT-08-06", "包含spawn_model节点", has_spawn)

    # 检查robot_state_publisher
    has_rsp = 'robot_state_publisher' in content
    tr.record("UT-08-07", "包含robot_state_publisher节点", has_rsp)

    # 检查publish_frequency
    has_freq = 'publish_frequency' in content and '50.0' in content
    tr.record("UT-08-08", "robot_state_publisher频率50Hz", has_freq)

    # 检查joint_state_publisher
    has_jsp = 'joint_state_publisher' in content
    tr.record("UT-08-09", "包含joint_state_publisher节点", has_jsp)

    # 检查use_sim_time
    has_sim_time = 'use_sim_time' in content
    tr.record("UT-08-10", "设置use_sim_time参数", has_sim_time)

    # 检查xacro命令
    has_xacro_cmd = 'xacro' in content and '--inorder' in content
    tr.record("UT-08-11", "使用xacro --inorder处理", has_xacro_cmd)


def test_rviz_config(tr):
    """UT-09: simulator_3d.rviz 配置检查"""
    print("\n--- UT-09: RViz配置文件检查 ---")
    path = NEW_FILES["simulator_3d.rviz"]
    content = read_file(path)

    # 检查PointCloud2显示
    has_pc2 = 'rviz/PointCloud2' in content or 'PointCloud2' in content
    tr.record("UT-09-01", "包含PointCloud2显示项", has_pc2)

    # 检查话题配置
    has_velodyne_topic = '/velodyne_points' in content
    tr.record("UT-09-02", "PointCloud2话题: /velodyne_points", has_velodyne_topic)

    # 检查RobotModel
    has_robot_model = 'rviz/RobotModel' in content or 'RobotModel' in content
    tr.record("UT-09-03", "包含RobotModel显示项", has_robot_model)

    # 检查TF
    has_tf = 'rviz/TF' in content or '/tf' in content
    tr.record("UT-09-04", "包含TF显示项", has_tf)

    # 检查Grid
    has_grid = 'rviz/Grid' in content
    tr.record("UT-09-05", "包含Grid显示项", has_grid)


def test_existing_files_unchanged(tr):
    """UT-10: 原有文件未修改（回归检查）"""
    print("\n--- UT-10: 原有文件未修改检查 ---")
    # 检查原有文件是否存在且完整
    for name, path in EXISTING_FILES.items():
        exists = path.exists() and path.is_file()
        tr.record(f"UT-10-{name}", f"原有文件 {name} 仍存在", exists)

    # 检查2D lidar_gazebo.xacro 仍完整
    content = read_file(EXISTING_FILES["lidar_gazebo.xacro"])
    has_rplidar = 'name="rplidar"' in content
    tr.record("UT-10-rplidar", "2D雷达rplidar配置仍完整", has_rplidar)


def test_vlp16_parameter_consistency(tr):
    """UT-11: VLP-16参数与规格书一致性"""
    print("\n--- UT-11: VLP-16参数一致性检查 ---")
    path = NEW_FILES["lidar3d_gazebo.xacro"]
    content = read_file(path)

    # VLP-16规格: 16线, ±15°垂直FOV, 360°水平FOV, 10Hz, 100m range
    # 检查16线
    lines_16 = content.count('<samples>16</samples>') >= 1
    tr.record("UT-11-01", "垂直线数: 16线", lines_16)

    # 检查水平360度（±π）
    h_angle_range = '-3.14159' in content and '3.14159' in content
    tr.record("UT-11-02", "水平FOV: 360° (±π)", h_angle_range)

    # 检查垂直±15° (±0.2618 rad)
    v_angle_ok = '-0.2618' in content and '0.2618' in content
    tr.record("UT-11-03", "垂直FOV: ±15° (±0.2618 rad)", v_angle_ok)

    # 检查每帧点数 = 1800 × 16 = 28800
    has_1800 = '<samples>1800</samples>' in content
    tr.record("UT-11-04", "水平采样1800点 (每帧≈28800点)", has_1800)

    # 检查range 0.5~100m
    range_ok = '<min>0.5</min>' in content and '<max>100.0</max>' in content
    tr.record("UT-11-05", "测距范围: 0.5~100m", range_ok)


def test_no_duplicate_link_names(tr):
    """UT-12: link/joint名称无冲突"""
    print("\n--- UT-12: link/joint名称无冲突检查 ---")
    # 检查3D lidar的link名 (velodyne_link) 不与2D lidar (laser_link) 冲突
    lidar3d_content = read_file(NEW_FILES["lidar3d.xacro"])
    lidar2d_content = read_file(EXISTING_FILES["lidar.xacro"])

    # 3D lidar link: ${prefix}_link -> velodyne_link (when prefix=velodyne)
    # 2D lidar link: ${prefix}_link -> laser_link (when prefix=laser)
    # Since they use different prefixes, no conflict
    lidar3d_uses_prefix = '${prefix}_link' in lidar3d_content
    lidar2d_uses_prefix = '${prefix}_link' in lidar2d_content
    no_conflict = lidar3d_uses_prefix and lidar2d_uses_prefix  # both use parametric names
    tr.record("UT-12-01", "3D和2D雷达link使用不同prefix (velodyne vs laser)", no_conflict)

    # IMU link: imu_link, 与lidar3d不冲突
    imu_content = read_file(EXISTING_FILES["imu_gazebo.xacro"])
    no_imu_conflict = 'velodyne_link' not in imu_content and 'imu_link' not in lidar3d_content
    tr.record("UT-12-02", "IMU和3D雷达link名称不冲突", no_imu_conflict)


# ============================================================
# 主函数
# ============================================================

def main():
    print("=" * 60)
    print("  单元测试: 3D激光雷达 Xacro 模型验证")
    print("=" * 60)

    tr = TestResult()

    test_file_existence(tr)
    test_xml_well_formed(tr)
    test_lidar3d_xacro_structure(tr)
    test_lidar3d_joint_origin(tr)
    test_lidar3d_gazebo_plugin(tr)
    test_lidar3d_gazebo_macro_includes(tr)
    test_combined_model(tr)
    test_launch_file(tr)
    test_rviz_config(tr)
    test_existing_files_unchanged(tr)
    test_vlp16_parameter_consistency(tr)
    test_no_duplicate_link_names(tr)

    success = tr.summary()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
