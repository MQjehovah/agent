"""
单元测试: rosiwit_slam 编译修复 & 集成 launch

测试范围:
- UT-01: CMakeLists.txt 目标名修复验证
- UT-02: CMakeLists.txt rosidl_generate_interfaces 保持 PROJECT_NAME
- UT-03: CMakeLists.txt NODE_NAME 变量使用一致性
- UT-04: fast_lio2.launch.py 包名和可执行名修复
- UT-05: livox_avia.launch.py 包名和可执行名修复
- UT-06: package.xml 包含 member_of_group
- UT-07: simulator_slam_demo.launch.py 语法正确性
- UT-08: simulator_slam_demo.launch.py 结构完整性
- UT-09: velodyne_vlp16.yaml 配置验证
- UT-10: simulator CMakeLists.txt 未被修改
"""

import os
import re
import sys
import ast
import pytest
import yaml

# ==================== 路径常量 ====================
WORKSPACE = '/home/jmq/agent/workspace/projects/rosiwit_ws'
SLAM_DIR = os.path.join(WORKSPACE, 'src', 'rosiwit_slam')
SIM_DIR = os.path.join(WORKSPACE, 'src', 'rosiwit_simulator')

SLAM_CMAKE = os.path.join(SLAM_DIR, 'CMakeLists.txt')
SLAM_PKG_XML = os.path.join(SLAM_DIR, 'package.xml')
FAST_LIO2_LAUNCH = os.path.join(SLAM_DIR, 'launch', 'fast_lio2.launch.py')
LIVOX_AVIA_LAUNCH = os.path.join(SLAM_DIR, 'launch', 'livox_avia.launch.py')
DEMO_LAUNCH = os.path.join(SIM_DIR, 'launch', 'simulator_slam_demo.launch.py')
SIM_CMAKE = os.path.join(SIM_DIR, 'CMakeLists.txt')
VELO_CONFIG = os.path.join(SLAM_DIR, 'config', 'velodyne_vlp16.yaml')


# ==================== 辅助函数 ====================
def read_file(path):
    """读取文件内容"""
    with open(path, 'r') as f:
        return f.read()


# ==================== UT-01: CMakeLists.txt NODE_NAME 定义 ====================
class TestCMakeNodeName:
    """验证 CMakeLists.txt 中可执行目标使用独立名称 fast_lio2_node"""

    def test_node_name_variable_defined(self):
        """UT-01-01: CMakeLists.txt 定义了 NODE_NAME 变量"""
        content = read_file(SLAM_CMAKE)
        assert 'set(NODE_NAME fast_lio2_node)' in content, \
            "CMakeLists.txt 中未找到 set(NODE_NAME fast_lio2_node)"

    def test_add_executable_uses_node_name(self):
        """UT-01-02: add_executable 使用 ${NODE_NAME} 而非 ${PROJECT_NAME}"""
        content = read_file(SLAM_CMAKE)
        assert 'add_executable(${NODE_NAME}' in content, \
            "add_executable 未使用 ${NODE_NAME}"

    def test_no_executable_with_project_name(self):
        """UT-01-03: 不存在 add_executable(${PROJECT_NAME} ...) 用于可执行目标"""
        content = read_file(SLAM_CMAKE)
        # rosidl_generate_interfaces uses ${PROJECT_NAME}, which is correct
        # But add_executable should NOT use ${PROJECT_NAME}
        matches = re.findall(r'add_executable\s*\(\s*\$\{PROJECT_NAME\}', content)
        assert len(matches) == 0, \
            f"发现 add_executable(${{PROJECT_NAME}}...) 共 {len(matches)} 处，应使用 ${{NODE_NAME}}"

    def test_install_targets_uses_node_name(self):
        """UT-01-04: install(TARGETS ...) 使用 ${NODE_NAME}"""
        content = read_file(SLAM_CMAKE)
        # 主可执行目标的 install 应使用 NODE_NAME
        assert re.search(r'install\s*\(\s*TARGETS\s+\$\{NODE_NAME\}', content), \
            "install(TARGETS) 未使用 ${NODE_NAME}"


# ==================== UT-02: rosidl_generate_interfaces 保持 PROJECT_NAME ====================
class TestCMakeRosidl:
    """验证 rosidl_generate_interfaces 仍然使用 ${PROJECT_NAME}"""

    def test_rosidl_uses_project_name(self):
        """UT-02-01: rosidl_generate_interfaces 保持使用 ${PROJECT_NAME}"""
        content = read_file(SLAM_CMAKE)
        assert re.search(r'rosidl_generate_interfaces\s*\(\s*\$\{PROJECT_NAME\}', content), \
            "rosidl_generate_interfaces 未使用 ${PROJECT_NAME}"

    def test_rosidl_includes_msg_and_srv(self):
        """UT-02-02: rosidl_generate_interfaces 包含正确的消息和服务文件"""
        content = read_file(SLAM_CMAKE)
        assert 'msg/LocalizationStatus.msg' in content
        assert 'srv/GlobalLocalize.srv' in content
        assert 'srv/SetInitialPose.srv' in content
        assert 'srv/GetLocalizationStatus.srv' in content


# ==================== UT-03: NODE_NAME 一致性 ====================
class TestCMakeConsistency:
    """验证所有可执行目标引用使用 ${NODE_NAME}"""

    def test_ament_target_dependencies_uses_node_name(self):
        """UT-03-01: ament_target_dependencies 使用 ${NODE_NAME}"""
        content = read_file(SLAM_CMAKE)
        assert re.search(r'ament_target_dependencies\s*\(\s*\$\{NODE_NAME\}', content), \
            "ament_target_dependencies 未使用 ${NODE_NAME}"

    def test_target_link_libraries_uses_node_name(self):
        """UT-03-02: target_link_libraries 使用 ${NODE_NAME}"""
        content = read_file(SLAM_CMAKE)
        matches = re.findall(r'target_link_libraries\s*\(\s*\$\{NODE_NAME\}', content)
        assert len(matches) >= 1, \
            f"target_link_libraries 使用 ${{NODE_NAME}} 共 {len(matches)} 处，期望 >= 1"

    def test_target_compile_definitions_uses_node_name(self):
        """UT-03-03: target_compile_definitions 使用 ${NODE_NAME}"""
        content = read_file(SLAM_CMAKE)
        assert re.search(r'target_compile_definitions\s*\(\s*\$\{NODE_NAME\}', content), \
            "target_compile_definitions 未使用 ${NODE_NAME}"


# ==================== UT-04: fast_lio2.launch.py 修复验证 ====================
class TestFastLio2Launch:
    """验证 fast_lio2.launch.py 包名和可执行名修复"""

    def test_file_exists(self):
        """UT-04-01: 文件存在"""
        assert os.path.isfile(FAST_LIO2_LAUNCH), \
            f"文件不存在: {FAST_LIO2_LAUNCH}"

    def test_find_package_share_uses_rosiwit_slam(self):
        """UT-04-02: FindPackageShare 使用 'rosiwit_slam' 而非 'fast_lio2_slam'"""
        content = read_file(FAST_LIO2_LAUNCH)
        assert "FindPackageShare('rosiwit_slam')" in content, \
            "FindPackageShare 未使用 'rosiwit_slam'"
        assert "FindPackageShare('fast_lio2_slam')" not in content, \
            "FindPackageShare 仍引用 'fast_lio2_slam'"

    def test_node_package_is_rosiwit_slam(self):
        """UT-04-03: Node package 参数为 'rosiwit_slam'"""
        content = read_file(FAST_LIO2_LAUNCH)
        assert "package='rosiwit_slam'" in content, \
            "Node package 未设置为 'rosiwit_slam'"
        assert "package='fast_lio2_slam'" not in content, \
            "Node package 仍为 'fast_lio2_slam'"

    def test_node_executable_is_fast_lio2_node(self):
        """UT-04-04: Node executable 参数为 'fast_lio2_node'"""
        content = read_file(FAST_LIO2_LAUNCH)
        assert "executable='fast_lio2_node'" in content, \
            "Node executable 未设置为 'fast_lio2_node'"

    def test_syntax_valid(self):
        """UT-04-05: Python 语法正确"""
        content = read_file(FAST_LIO2_LAUNCH)
        try:
            ast.parse(content)
        except SyntaxError as e:
            pytest.fail(f"fast_lio2.launch.py 语法错误: {e}")

    def test_has_generate_launch_description(self):
        """UT-04-06: 包含 generate_launch_description 函数"""
        content = read_file(FAST_LIO2_LAUNCH)
        tree = ast.parse(content)
        func_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        assert 'generate_launch_description' in func_names, \
            "缺少 generate_launch_description 函数"

    def test_declares_config_file_arg(self):
        """UT-04-07: 声明 config_file 参数"""
        content = read_file(FAST_LIO2_LAUNCH)
        assert "'config_file'" in content, \
            "缺少 config_file launch 参数"

    def test_declares_use_sim_time_arg(self):
        """UT-04-08: 声明 use_sim_time 参数"""
        content = read_file(FAST_LIO2_LAUNCH)
        assert "'use_sim_time'" in content, \
            "缺少 use_sim_time launch 参数"

    def test_default_lidar_topic_velodyne(self):
        """UT-04-09: 默认 lidar_topic 为 /velodyne_points"""
        content = read_file(FAST_LIO2_LAUNCH)
        assert "'/velodyne_points'" in content, \
            "默认 lidar_topic 不是 /velodyne_points"

    def test_default_imu_topic(self):
        """UT-04-10: 默认 imu_topic 为 /imu"""
        content = read_file(FAST_LIO2_LAUNCH)
        assert "'/imu'" in content, \
            "默认 imu_topic 不是 /imu"


# ==================== UT-05: livox_avia.launch.py 修复验证 ====================
class TestLivoxAviaLaunch:
    """验证 livox_avia.launch.py 包名和可执行名修复"""

    def test_file_exists(self):
        """UT-05-01: 文件存在"""
        assert os.path.isfile(LIVOX_AVIA_LAUNCH), \
            f"文件不存在: {LIVOX_AVIA_LAUNCH}"

    def test_find_package_share_uses_rosiwit_slam(self):
        """UT-05-02: FindPackageShare 使用 'rosiwit_slam'"""
        content = read_file(LIVOX_AVIA_LAUNCH)
        assert "FindPackageShare('rosiwit_slam')" in content, \
            "FindPackageShare 未使用 'rosiwit_slam'"
        assert "FindPackageShare('fast_lio2_slam')" not in content, \
            "FindPackageShare 仍引用 'fast_lio2_slam'"

    def test_node_package_is_rosiwit_slam(self):
        """UT-05-03: Node package 参数为 'rosiwit_slam'"""
        content = read_file(LIVOX_AVIA_LAUNCH)
        assert "package='rosiwit_slam'" in content, \
            "Node package 未设置为 'rosiwit_slam'"

    def test_node_executable_is_fast_lio2_node(self):
        """UT-05-04: Node executable 参数为 'fast_lio2_node'"""
        content = read_file(LIVOX_AVIA_LAUNCH)
        assert "executable='fast_lio2_node'" in content, \
            "Node executable 未设置为 'fast_lio2_node'"

    def test_syntax_valid(self):
        """UT-05-05: Python 语法正确"""
        content = read_file(LIVOX_AVIA_LAUNCH)
        try:
            ast.parse(content)
        except SyntaxError as e:
            pytest.fail(f"livox_avia.launch.py 语法错误: {e}")

    def test_no_fast_lio2_slam_references(self):
        """UT-05-06: 无任何 fast_lio2_slam 引用"""
        content = read_file(LIVOX_AVIA_LAUNCH)
        assert 'fast_lio2_slam' not in content, \
            "livox_avia.launch.py 仍包含 'fast_lio2_slam' 引用"


# ==================== UT-06: package.xml 验证 ====================
class TestPackageXml:
    """验证 package.xml 配置正确"""

    def test_package_name_is_rosiwit_slam(self):
        """UT-06-01: 包名为 rosiwit_slam"""
        content = read_file(SLAM_PKG_XML)
        assert '<name>rosiwit_slam</name>' in content, \
            "package.xml 包名不是 rosiwit_slam"

    def test_has_member_of_group(self):
        """UT-06-02: 包含 member_of_group rosidl_interface_packages"""
        content = read_file(SLAM_PKG_XML)
        assert '<member_of_group>rosidl_interface_packages</member_of_group>' in content, \
            "package.xml 缺少 member_of_group"

    def test_has_rosidl_default_generators(self):
        """UT-06-03: 包含 rosidl_default_generators 构建依赖"""
        content = read_file(SLAM_PKG_XML)
        assert 'rosidl_default_generators' in content, \
            "package.xml 缺少 rosidl_default_generators 依赖"

    def test_has_rosidl_default_runtime(self):
        """UT-06-04: 包含 rosidl_default_runtime 执行依赖"""
        content = read_file(SLAM_PKG_XML)
        assert 'rosidl_default_runtime' in content, \
            "package.xml 缺少 rosidl_default_runtime 依赖"


# ==================== UT-07: simulator_slam_demo.launch.py 语法验证 ====================
class TestDemoLaunchSyntax:
    """验证集成 launch 文件语法正确"""

    def test_file_exists(self):
        """UT-07-01: 文件存在"""
        assert os.path.isfile(DEMO_LAUNCH), \
            f"文件不存在: {DEMO_LAUNCH}"

    def test_syntax_valid(self):
        """UT-07-02: Python 语法正确"""
        content = read_file(DEMO_LAUNCH)
        try:
            ast.parse(content)
        except SyntaxError as e:
            pytest.fail(f"simulator_slam_demo.launch.py 语法错误: {e}")

    def test_has_generate_launch_description(self):
        """UT-07-03: 包含 generate_launch_description 函数"""
        content = read_file(DEMO_LAUNCH)
        tree = ast.parse(content)
        func_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        assert 'generate_launch_description' in func_names, \
            "缺少 generate_launch_description 函数"

    def test_imports_launch_modules(self):
        """UT-07-04: 导入必要的 launch 模块"""
        content = read_file(DEMO_LAUNCH)
        assert 'from launch import LaunchDescription' in content
        assert 'from launch_ros.actions import Node' in content

    def test_imports_xacro(self):
        """UT-07-05: 导入 xacro 模块"""
        content = read_file(DEMO_LAUNCH)
        assert 'import xacro' in content, \
            "集成 launch 未导入 xacro 模块"


# ==================== UT-08: simulator_slam_demo.launch.py 结构验证 ====================
class TestDemoLaunchStructure:
    """验证集成 launch 文件包含所有必需的节点和配置"""

    @pytest.fixture
    def launch_content(self):
        return read_file(DEMO_LAUNCH)

    def test_has_gazebo_headless(self, launch_content):
        """UT-08-01: 设置 Gazebo headless 模式"""
        assert "GAZEBO_HEADLESS" in launch_content, \
            "未设置 GAZEBO_HEADLESS 环境变量"
        assert "'1'" in launch_content or '"1"' in launch_content

    def test_gui_false(self, launch_content):
        """UT-08-02: Gazebo gui 参数设为 false"""
        assert "'gui': 'false'" in launch_content or '"gui": "false"' in launch_content, \
            "Gazebo gui 未设为 false"

    def test_uses_house_world(self, launch_content):
        """UT-08-03: 使用 house.world"""
        assert 'house.world' in launch_content, \
            "未使用 house.world"

    def test_uses_mbot_lidar3d_xacro(self, launch_content):
        """UT-08-04: 使用 mbot_with_lidar3d_gazebo.xacro"""
        assert 'mbot_with_lidar3d_gazebo.xacro' in launch_content, \
            "未使用 mbot_with_lidar3d_gazebo.xacro"

    def test_has_robot_state_publisher(self, launch_content):
        """UT-08-05: 包含 robot_state_publisher 节点"""
        assert 'robot_state_publisher' in launch_content, \
            "缺少 robot_state_publisher 节点"

    def test_has_spawn_entity(self, launch_content):
        """UT-08-06: 包含 spawn_entity 节点"""
        assert 'spawn_entity' in launch_content or 'spawn' in launch_content.lower(), \
            "缺少 spawn_entity"

    def test_has_slam_node(self, launch_content):
        """UT-08-07: 包含 SLAM 节点配置"""
        assert "package='rosiwit_slam'" in launch_content, \
            "SLAM 节点包名不正确"
        assert "executable='fast_lio2_node'" in launch_content, \
            "SLAM 节点可执行名不正确"

    def test_slam_uses_velodyne_config(self, launch_content):
        """UT-08-08: SLAM 使用 velodyne_vlp16.yaml 配置"""
        assert 'velodyne_vlp16.yaml' in launch_content, \
            "SLAM 未使用 velodyne_vlp16.yaml 配置"

    def test_slam_topics_correct(self, launch_content):
        """UT-08-09: SLAM 订阅正确话题"""
        assert '/velodyne_points' in launch_content, \
            "缺少 /velodyne_points 话题配置"
        assert '/imu' in launch_content, \
            "缺少 /imu 话题配置"

    def test_has_auto_motion(self, launch_content):
        """UT-08-10: 包含自动运动节点"""
        assert '/cmd_vel' in launch_content, \
            "缺少 /cmd_vel 话题（自动运动节点）"

    def test_uses_sim_time(self, launch_content):
        """UT-08-11: 使用 use_sim_time"""
        assert 'use_sim_time' in launch_content, \
            "缺少 use_sim_time 配置"

    def test_has_timer_actions(self, launch_content):
        """UT-08-12: 使用 TimerAction 延迟启动"""
        assert 'TimerAction' in launch_content, \
            "缺少 TimerAction 延迟启动"

    def test_has_figure8_motion(self, launch_content):
        """UT-08-13: 包含 figure-8 运动模式"""
        assert 'figure8' in launch_content, \
            "缺少 figure8 运动模式"

    def test_has_gazebo_model_path(self, launch_content):
        """UT-08-14: 设置 GAZEBO_MODEL_PATH"""
        assert 'GAZEBO_MODEL_PATH' in launch_content, \
            "未设置 GAZEBO_MODEL_PATH"

    def test_declares_launch_arguments(self, launch_content):
        """UT-08-15: 声明 launch 参数"""
        assert 'DeclareLaunchArgument' in launch_content, \
            "未使用 DeclareLaunchArgument"

    def test_motion_pattern_configurable(self, launch_content):
        """UT-08-16: 运动模式可配置"""
        assert 'motion_pattern' in launch_content or 'pattern' in launch_content, \
            "运动模式不可配置"

    def test_speed_configurable(self, launch_content):
        """UT-08-17: 速度可配置"""
        assert 'linear_speed' in launch_content or 'speed' in launch_content, \
            "线速度不可配置"
        assert 'angular_speed' in launch_content, \
            "角速度不可配置"


# ==================== UT-09: velodyne_vlp16.yaml 配置验证 ====================
class TestVelodyneConfig:
    """验证 SLAM 配置文件"""

    @pytest.fixture
    def config(self):
        with open(VELO_CONFIG, 'r') as f:
            return yaml.safe_load(f)

    def test_has_imu_section(self, config):
        """UT-09-01: 包含 IMU 参数"""
        assert 'imu' in config, "缺少 imu 配置节"

    def test_has_lidar_section(self, config):
        """UT-09-02: 包含 LiDAR 参数"""
        assert 'lidar' in config, "缺少 lidar 配置节"

    def test_scan_line_16(self, config):
        """UT-09-03: scan_line 为 16"""
        assert config['lidar']['scan_line'] == 16, \
            "VLP-16 scan_line 应为 16"

    def test_lidar_topic_correct(self, config):
        """UT-09-04: lidar_topic 为 /velodyne_points"""
        assert config['ros']['lidar_topic'] == '/velodyne_points', \
            "lidar_topic 配置不正确"

    def test_imu_topic_correct(self, config):
        """UT-09-05: imu_topic 为 /imu"""
        assert config['ros']['imu_topic'] == '/imu', \
            "imu_topic 配置不正确"

    def test_has_extrinsic_section(self, config):
        """UT-09-06: 包含外参配置"""
        assert 'extrinsic' in config, "缺少 extrinsic 配置节"

    def test_has_iekf_section(self, config):
        """UT-09-07: 包含 IEKF 参数"""
        assert 'iekf' in config, "缺少 iekf 配置节"

    def test_has_ros_section(self, config):
        """UT-09-08: 包含 ROS 参数"""
        assert 'ros' in config, "缺少 ros 配置节"


# ==================== UT-10: simulator 包未被修改 ====================
class TestSimulatorUnchanged:
    """验证 simulator 包仅新增了 launch 文件"""

    def test_simulator_cmake_unchanged(self):
        """UT-10-01: simulator CMakeLists.txt 仍使用 install(DIRECTORY launch)"""
        content = read_file(SIM_CMAKE)
        assert 'install(DIRECTORY' in content
        assert 'launch' in content
        # Should be simple - no scripts install
        assert 'ament_cmake' in content

    def test_simulator_cmake_installs_launch_dir(self):
        """UT-10-02: simulator CMakeLists.txt install 包含 launch 目录"""
        content = read_file(SIM_CMAKE)
        # The launch directory is in install(DIRECTORY ... launch ...)
        assert re.search(r'install\s*\(\s*DIRECTORY[^)]*\blaunch\b', content, re.DOTALL), \
            "simulator CMakeLists.txt 未安装 launch 目录"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
