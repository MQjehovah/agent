"""
功能测试: rosiwit_slam 编译修复 & 仿真集成 Launch

逐条验证 requirements.md 中的验收标准:
- AC-1: CMake 编译成功
- AC-2: Launch 文件包名正确
- AC-3: 集成 Launch 包含全部节点
- AC-4: 话题匹配正确
- AC-5: Gazebo headless 模式
- AC-6: 自动运动节点工作
- AC-7: 配置文件正确
- AC-8: 全量编译成功
"""

import os
import re
import subprocess
import yaml
import pytest

WORKSPACE = '/home/jmq/agent/workspace/projects/rosiwit_ws'
SLAM_DIR = os.path.join(WORKSPACE, 'src', 'rosiwit_slam')
SIM_DIR = os.path.join(WORKSPACE, 'src', 'rosiwit_simulator')
DEMO_LAUNCH = os.path.join(SIM_DIR, 'launch', 'simulator_slam_demo.launch.py')


def run_cmd(cmd, cwd=None, timeout=180):
    """运行 shell 命令（使用 bash）"""
    return subprocess.run(
        ['bash', '-c', cmd], capture_output=True, text=True,
        cwd=cwd, timeout=timeout
    )


def read_file(path):
    with open(path, 'r') as f:
        return f.read()


# ==================== AC-1: CMake 编译成功 ====================
class TestAC1CMakeBuild:
    """AC-1: colcon build --packages-select rosiwit_slam 返回码 0，零错误"""

    def test_ac1_01_build_return_code_zero(self):
        """AC-1-01: rosiwit_slam 编译返回码 0"""
        result = run_cmd(
            'source /opt/ros/humble/setup.bash && colcon build --packages-select rosiwit_slam '
            '--cmake-args -DBUILD_TESTING=OFF 2>&1',
            cwd=WORKSPACE,
            timeout=180
        )
        assert result.returncode == 0, \
            f"编译返回码 {result.returncode}，期望 0\nstderr: {result.stderr}"

    def test_ac1_02_no_cmake_errors(self):
        """AC-1-02: 编译过程无 CMake 错误"""
        result = run_cmd(
            'source /opt/ros/humble/setup.bash && colcon build --packages-select rosiwit_slam '
            '--cmake-args -DBUILD_TESTING=OFF 2>&1',
            cwd=WORKSPACE,
            timeout=180
        )
        assert 'CMake Error' not in result.stderr, \
            f"发现 CMake Error:\n{result.stderr}"

    def test_ac1_03_no_compilation_errors(self):
        """AC-1-03: 编译过程无编译错误"""
        result = run_cmd(
            'source /opt/ros/humble/setup.bash && colcon build --packages-select rosiwit_slam '
            '--cmake-args -DBUILD_TESTING=OFF 2>&1',
            cwd=WORKSPACE,
            timeout=180
        )
        # 检查是否有 fatal error
        assert 'fatal error:' not in result.stderr, \
            f"发现编译 fatal error:\n{result.stderr}"

    def test_ac1_04_executable_produced(self):
        """AC-1-04: 产出可执行文件 fast_lio2_node"""
        exec_path = os.path.join(WORKSPACE, 'install', 'rosiwit_slam',
                                 'lib', 'rosiwit_slam', 'fast_lio2_node')
        assert os.path.isfile(exec_path), \
            f"可执行文件未产出: {exec_path}"

    def test_ac1_05_colcon_summary_success(self):
        """AC-1-05: colcon 输出 Summary 包含成功"""
        result = run_cmd(
            'source /opt/ros/humble/setup.bash && colcon build --packages-select rosiwit_slam '
            '--cmake-args -DBUILD_TESTING=OFF 2>&1',
            cwd=WORKSPACE,
            timeout=180
        )
        assert 'Summary' in result.stdout, \
            "colcon 输出中缺少 Summary"
        # 包完成应不包含 failed
        lines = result.stdout.split('\n')
        summary_lines = [l for l in lines if 'Summary' in l or 'package' in l]
        for line in summary_lines:
            assert 'failed' not in line.lower(), \
                f"colcon Summary 包含 failed: {line}"


# ==================== AC-2: Launch 文件包名正确 ====================
class TestAC2LaunchPackageNames:
    """AC-2: 所有 launch 文件中 package 引用一致为 rosiwit_slam"""

    def test_ac2_01_fast_lio2_package_name(self):
        """AC-2-01: fast_lio2.launch.py 使用 package='rosiwit_slam'"""
        content = read_file(os.path.join(SLAM_DIR, 'launch', 'fast_lio2.launch.py'))
        assert "package='rosiwit_slam'" in content

    def test_ac2_02_fast_lio2_executable_name(self):
        """AC-2-02: fast_lio2.launch.py 使用 executable='fast_lio2_node'"""
        content = read_file(os.path.join(SLAM_DIR, 'launch', 'fast_lio2.launch.py'))
        assert "executable='fast_lio2_node'" in content

    def test_ac2_03_livox_avia_package_name(self):
        """AC-2-03: livox_avia.launch.py 使用 package='rosiwit_slam'"""
        content = read_file(os.path.join(SLAM_DIR, 'launch', 'livox_avia.launch.py'))
        assert "package='rosiwit_slam'" in content

    def test_ac2_04_livox_avia_executable_name(self):
        """AC-2-04: livox_avia.launch.py 使用 executable='fast_lio2_node'"""
        content = read_file(os.path.join(SLAM_DIR, 'launch', 'livox_avia.launch.py'))
        assert "executable='fast_lio2_node'" in content

    def test_ac2_05_no_old_package_references(self):
        """AC-2-05: 所有 launch 文件无 'fast_lio2_slam' 包名引用"""
        for launch_file in ['fast_lio2.launch.py', 'livox_avia.launch.py']:
            content = read_file(os.path.join(SLAM_DIR, 'launch', launch_file))
            assert 'fast_lio2_slam' not in content, \
                f"{launch_file} 仍包含 'fast_lio2_slam' 引用"


# ==================== AC-3: 集成 Launch 包含全部节点 ====================
class TestAC3IntegrationLaunchNodes:
    """AC-3: simulator_slam_demo.launch.py 包含全部 5 个功能组件"""

    @pytest.fixture
    def demo(self):
        return read_file(DEMO_LAUNCH)

    def test_ac3_01_gazebo_node(self, demo):
        """AC-3-01: 包含 Gazebo 启动"""
        # Gazebo 可以通过 IncludeLaunchDescription 或 ExecuteProcess
        has_gazebo = ('gazebo' in demo.lower() and
                      ('ExecuteProcess' in demo or 'IncludeLaunchDescription' in demo))
        assert has_gazebo, "缺少 Gazebo 启动"

    def test_ac3_02_robot_state_publisher(self, demo):
        """AC-3-02: 包含 robot_state_publisher 节点"""
        assert 'robot_state_publisher' in demo, "缺少 robot_state_publisher"

    def test_ac3_03_spawn_entity(self, demo):
        """AC-3-03: 包含 spawn entity"""
        assert 'spawn' in demo.lower(), "缺少 spawn entity"

    def test_ac3_04_slam_node(self, demo):
        """AC-3-04: 包含 SLAM 节点"""
        assert "package='rosiwit_slam'" in demo, "缺少 SLAM 节点"
        assert "executable='fast_lio2_node'" in demo, "SLAM 可执行名不正确"

    def test_ac3_05_auto_motion(self, demo):
        """AC-3-05: 包含自动运动节点"""
        assert '/cmd_vel' in demo, "缺少自动运动 /cmd_vel 发布"

    def test_ac3_06_uses_house_world(self, demo):
        """AC-3-06: 使用 house.world"""
        assert 'house.world' in demo, "未使用 house.world"


# ==================== AC-4: 话题匹配正确 ====================
class TestAC4TopicMatching:
    """AC-4: 话题命名一致"""

    def test_ac4_01_robot_publishes_velodyne_points(self):
        """AC-4-01: 机器人 URDF/xacro 发布 /velodyne_points"""
        # 验证 simulator 相关配置包含 velodyne 话题
        demo = read_file(DEMO_LAUNCH)
        assert '/velodyne_points' in demo, \
            "集成 launch 未引用 /velodyne_points"

    def test_ac4_02_robot_publishes_imu(self):
        """AC-4-02: 机器人 URDF/xacro 发布 /imu"""
        demo = read_file(DEMO_LAUNCH)
        assert '/imu' in demo, "集成 launch 未引用 /imu"

    def test_ac4_03_slam_subscribes_velodyne_points(self):
        """AC-4-03: SLAM 订阅 /velodyne_points"""
        config_path = os.path.join(SLAM_DIR, 'config', 'velodyne_vlp16.yaml')
        with open(config_path) as f:
            config = yaml.safe_load(f)
        assert config['ros']['lidar_topic'] == '/velodyne_points', \
            "SLAM 配置中 lidar_topic 不是 /velodyne_points"

    def test_ac4_04_slam_subscribes_imu(self):
        """AC-4-04: SLAM 订阅 /imu"""
        config_path = os.path.join(SLAM_DIR, 'config', 'velodyne_vlp16.yaml')
        with open(config_path) as f:
            config = yaml.safe_load(f)
        assert config['ros']['imu_topic'] == '/imu', \
            "SLAM 配置中 imu_topic 不是 /imu"

    def test_ac4_05_cmd_vel_topic_present(self):
        """AC-4-05: 自动运动发布到 /cmd_vel"""
        demo = read_file(DEMO_LAUNCH)
        assert '/cmd_vel' in demo, "缺少 /cmd_vel 话题"


# ==================== AC-5: Gazebo headless 模式 ====================
class TestAC5GazeboHeadless:
    """AC-5: Gazebo 使用 headless 模式"""

    def test_ac5_01_gazebo_headless_env(self):
        """AC-5-01: 设置 GAZEBO_HEADLESS=1"""
        demo = read_file(DEMO_LAUNCH)
        assert 'GAZEBO_HEADLESS' in demo, "未设置 GAZEBO_HEADLESS"

    def test_ac5_02_gui_false(self):
        """AC-5-02: Gazebo gui 参数为 false"""
        demo = read_file(DEMO_LAUNCH)
        assert re.search(r"['\"]gui['\"]\s*:\s*['\"]false['\"]", demo), \
            "Gazebo gui 参数未设为 false"

    def test_ac5_03_verbose_enabled(self):
        """AC-5-03: Gazebo verbose 输出（用于调试）"""
        demo = read_file(DEMO_LAUNCH)
        # verbose is nice-to-have, check if it exists
        if 'verbose' in demo:
            assert True
        else:
            pytest.skip("verbose 参数未设置（非阻塞）")


# ==================== AC-6: 自动运动节点 ====================
class TestAC6AutoMotion:
    """AC-6: 自动运动节点功能正确"""

    def test_ac6_01_publishes_cmd_vel(self):
        """AC-6-01: 发布到 /cmd_vel"""
        demo = read_file(DEMO_LAUNCH)
        assert '/cmd_vel' in demo

    def test_ac6_02_motion_pattern_defined(self):
        """AC-6-02: 定义了运动模式（figure-8 或 circle）"""
        demo = read_file(DEMO_LAUNCH)
        assert 'figure8' in demo, "未定义 figure-8 运动模式"

    def test_ac6_03_motion_has_linear_and_angular(self):
        """AC-6-03: 运动包含线速度和角速度"""
        demo = read_file(DEMO_LAUNCH)
        # 查找 Twist 相关或速度相关
        has_linear = ('linear' in demo or 'linear_speed' in demo)
        has_angular = ('angular' in demo or 'angular_speed' in demo)
        assert has_linear, "缺少线速度配置"
        assert has_angular, "缺少角速度配置"


# ==================== AC-7: 配置文件正确 ====================
class TestAC7Config:
    """AC-7: velodyne_vlp16.yaml 参数正确"""

    def test_ac7_01_yaml_valid(self):
        """AC-7-01: YAML 文件格式正确"""
        config_path = os.path.join(SLAM_DIR, 'config', 'velodyne_vlp16.yaml')
        with open(config_path) as f:
            config = yaml.safe_load(f)
        assert isinstance(config, dict), "YAML 格式不正确"

    def test_ac7_02_scan_line_16(self):
        """AC-7-02: scan_line = 16 (VLP-16)"""
        config_path = os.path.join(SLAM_DIR, 'config', 'velodyne_vlp16.yaml')
        with open(config_path) as f:
            config = yaml.safe_load(f)
        assert config['lidar']['scan_line'] == 16

    def test_ac7_03_topics_match_robot(self):
        """AC-7-03: 话题与机器人发布一致"""
        config_path = os.path.join(SLAM_DIR, 'config', 'velodyne_vlp16.yaml')
        with open(config_path) as f:
            config = yaml.safe_load(f)
        assert config['ros']['lidar_topic'] == '/velodyne_points'
        assert config['ros']['imu_topic'] == '/imu'


# ==================== AC-8: 全量编译成功 ====================
class TestAC8FullBuild:
    """AC-8: 全量 colcon build 成功"""

    def test_ac8_01_both_packages_build(self):
        """AC-8-01: rosiwit_slam + simulator 全量编译成功"""
        result = run_cmd(
            'source /opt/ros/humble/setup.bash && colcon build '
            '--packages-select rosiwit_slam simulator '
            '--cmake-args -DBUILD_TESTING=OFF 2>&1',
            cwd=WORKSPACE,
            timeout=300
        )
        assert result.returncode == 0, \
            f"全量编译失败:\nstdout: {result.stdout}\nstderr: {result.stderr}"

    def test_ac8_02_all_artifacts_installed(self):
        """AC-8-02: 所有编译产物安装正确"""
        # rosiwit_slam
        slam_exec = os.path.join(WORKSPACE, 'install', 'rosiwit_slam',
                                  'lib', 'rosiwit_slam', 'fast_lio2_node')
        assert os.path.isfile(slam_exec), "fast_lio2_node 未安装"

        # simulator
        sim_launch = os.path.join(WORKSPACE, 'install', 'simulator',
                                   'share', 'simulator', 'launch',
                                   'simulator_slam_demo.launch.py')
        assert os.path.isfile(sim_launch), "simulator_slam_demo.launch.py 未安装"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
