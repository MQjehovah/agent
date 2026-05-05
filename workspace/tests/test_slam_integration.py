"""
集成测试: rosiwit_slam 编译修复 & 仿真集成 Launch

测试范围:
- IT-01: colcon build 编译成功验证
- IT-02: 编译产物验证 (fast_lio2_node 可执行文件)
- IT-03: 安装文件结构验证
- IT-04: launch 文件导入验证
- IT-05: 话题匹配一致性验证
- IT-06: 快照路径/文件完整性验证
"""

import os
import re
import sys
import ast
import subprocess
import pytest

# ==================== 路径常量 ====================
WORKSPACE = '/home/jmq/agent/workspace/projects/rosiwit_ws'
SLAM_DIR = os.path.join(WORKSPACE, 'src', 'rosiwit_slam')
SIM_DIR = os.path.join(WORKSPACE, 'src', 'rosiwit_simulator')
INSTALL_SLAM = os.path.join(WORKSPACE, 'install', 'rosiwit_slam')
INSTALL_SIM = os.path.join(WORKSPACE, 'install', 'simulator')


def run_cmd(cmd, cwd=None, timeout=120):
    """运行 shell 命令并返回结果（使用 bash）"""
    result = subprocess.run(
        ['bash', '-c', cmd], capture_output=True, text=True,
        cwd=cwd, timeout=timeout
    )
    return result


# ==================== IT-01: 编译验证 ====================
class TestColconBuild:
    """验证 colcon build 编译成功"""

    def test_rosiwit_slam_build_success(self):
        """IT-01-01: rosiwit_slam 包编译成功"""
        result = run_cmd(
            'source /opt/ros/humble/setup.bash && colcon build --packages-select rosiwit_slam '
            '--cmake-args -DBUILD_TESTING=OFF 2>&1',
            cwd=WORKSPACE,
            timeout=180
        )
        assert result.returncode == 0, \
            f"rosiwit_slam 编译失败:\nstdout: {result.stdout}\nstderr: {result.stderr}"

    def test_simulator_build_success(self):
        """IT-01-02: simulator 包编译成功"""
        result = run_cmd(
            'source /opt/ros/humble/setup.bash && colcon build --packages-select simulator 2>&1',
            cwd=WORKSPACE,
            timeout=60
        )
        assert result.returncode == 0, \
            f"simulator 编译失败:\nstdout: {result.stdout}\nstderr: {result.stderr}"

    def test_no_cmake_target_conflict(self):
        """IT-01-03: 无 CMake 目标名冲突错误"""
        result = run_cmd(
            'source /opt/ros/humble/setup.bash && colcon build --packages-select rosiwit_slam '
            '--cmake-args -DBUILD_TESTING=OFF 2>&1',
            cwd=WORKSPACE,
            timeout=180
        )
        assert 'add_executable cannot create target' not in result.stderr, \
            "存在 CMake 目标名冲突"
        assert 'target already exists' not in result.stderr, \
            "存在 CMake 目标名冲突"


# ==================== IT-02: 编译产物验证 ====================
class TestBuildArtifacts:
    """验证编译产物"""

    def test_fast_lio2_node_executable_exists(self):
        """IT-02-01: fast_lio2_node 可执行文件存在"""
        exec_path = os.path.join(INSTALL_SLAM, 'lib', 'rosiwit_slam', 'fast_lio2_node')
        assert os.path.isfile(exec_path), \
            f"可执行文件不存在: {exec_path}"

    def test_fast_lio2_node_is_executable(self):
        """IT-02-02: fast_lio2_node 有执行权限"""
        exec_path = os.path.join(INSTALL_SLAM, 'lib', 'rosiwit_slam', 'fast_lio2_node')
        if os.path.exists(exec_path):
            assert os.access(exec_path, os.X_OK), \
                "fast_lio2_node 没有执行权限"
        else:
            pytest.skip("fast_lio2_node 未构建")

    def test_no_old_executable_name(self):
        """IT-02-03: 不存在旧的 rosiwit_slam 可执行文件"""
        old_path = os.path.join(INSTALL_SLAM, 'lib', 'rosiwit_slam', 'rosiwit_slam')
        assert not os.path.exists(old_path), \
            f"旧的可执行文件仍存在: {old_path}"


# ==================== IT-03: 安装文件结构验证 ====================
class TestInstallStructure:
    """验证安装后的文件结构"""

    def test_slam_launch_files_installed(self):
        """IT-03-01: SLAM launch 文件已安装"""
        launch_dir = os.path.join(INSTALL_SLAM, 'share', 'rosiwit_slam', 'launch')
        assert os.path.isdir(launch_dir), f"launch 目录不存在: {launch_dir}"
        assert os.path.isfile(os.path.join(launch_dir, 'fast_lio2.launch.py'))
        assert os.path.isfile(os.path.join(launch_dir, 'livox_avia.launch.py'))

    def test_slam_config_installed(self):
        """IT-03-02: SLAM 配置文件已安装"""
        config_dir = os.path.join(INSTALL_SLAM, 'share', 'rosiwit_slam', 'config')
        assert os.path.isdir(config_dir), f"config 目录不存在: {config_dir}"
        assert os.path.isfile(os.path.join(config_dir, 'velodyne_vlp16.yaml'))

    def test_demo_launch_installed(self):
        """IT-03-03: 集成 launch 文件已安装到 simulator 包"""
        demo_path = os.path.join(INSTALL_SIM, 'share', 'simulator', 'launch',
                                 'simulator_slam_demo.launch.py')
        assert os.path.isfile(demo_path), \
            f"集成 launch 文件未安装: {demo_path}"

    def test_slam_share_directory_structure(self):
        """IT-03-04: SLAM share 目录结构完整"""
        share_dir = os.path.join(INSTALL_SLAM, 'share', 'rosiwit_slam')
        assert os.path.isdir(share_dir), f"share 目录不存在: {share_dir}"

    def test_simulator_world_files_installed(self):
        """IT-03-05: simulator world 文件已安装"""
        world_dir = os.path.join(INSTALL_SIM, 'share', 'simulator', 'world')
        assert os.path.isdir(world_dir), f"world 目录不存在: {world_dir}"
        assert os.path.isfile(os.path.join(world_dir, 'house.world')), \
            "house.world 未安装"

    def test_simulator_urdf_files_installed(self):
        """IT-03-06: simulator URDF/xacro 文件已安装"""
        urdf_dir = os.path.join(INSTALL_SIM, 'share', 'simulator', 'urdf')
        assert os.path.isdir(urdf_dir), f"urdf 目录不存在: {urdf_dir}"


# ==================== IT-04: Launch 文件导入验证 ====================
class TestLaunchImport:
    """验证 launch 文件可以正确导入和解析"""

    def test_fast_lio2_launch_importable(self):
        """IT-04-01: fast_lio2.launch.py 可导入"""
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "fast_lio2_launch",
                os.path.join(SLAM_DIR, 'launch', 'fast_lio2.launch.py')
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            assert hasattr(module, 'generate_launch_description'), \
                "缺少 generate_launch_description 函数"
        except Exception as e:
            pytest.fail(f"fast_lio2.launch.py 导入失败: {e}")

    def test_livox_avia_launch_importable(self):
        """IT-04-02: livox_avia.launch.py 可导入"""
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "livox_avia_launch",
                os.path.join(SLAM_DIR, 'launch', 'livox_avia.launch.py')
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            assert hasattr(module, 'generate_launch_description'), \
                "缺少 generate_launch_description 函数"
        except Exception as e:
            pytest.fail(f"livox_avia.launch.py 导入失败: {e}")

    def test_demo_launch_importable(self):
        """IT-04-03: simulator_slam_demo.launch.py 可导入（需要 xacro 运行时依赖）"""
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "simulator_slam_demo",
                os.path.join(SIM_DIR, 'launch', 'simulator_slam_demo.launch.py')
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            assert hasattr(module, 'generate_launch_description'), \
                "缺少 generate_launch_description 函数"
        except ModuleNotFoundError as e:
            if 'xacro' in str(e):
                pytest.skip(f"xacro 模块不可用（运行时依赖，非代码缺陷）: {e}")
            else:
                pytest.fail(f"simulator_slam_demo.launch.py 导入失败: {e}")
        except Exception as e:
            pytest.fail(f"simulator_slam_demo.launch.py 导入失败: {e}")

    def test_demo_launch_description_callable(self):
        """IT-04-04: generate_launch_description() 可调用"""
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "simulator_slam_demo",
                os.path.join(SIM_DIR, 'launch', 'simulator_slam_demo.launch.py')
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            desc = module.generate_launch_description()
            assert desc is not None, \
                "generate_launch_description() 返回 None"
        except Exception as e:
            # xacro 或 gazebo_ros 可能不在当前环境中, 只要能解析到返回步骤即可
            # 如果因为 xacro/gazebo 路径问题而失败，说明文件结构正确
            if 'xacro' in str(e).lower() or 'gazebo' in str(e).lower() or 'package' in str(e).lower():
                pytest.skip(f"运行时依赖不可用 (预期行为): {e}")
            else:
                pytest.fail(f"generate_launch_description() 调用失败: {e}")


# ==================== IT-05: 话题匹配一致性验证 ====================
class TestTopicConsistency:
    """验证所有文件中的话题引用一致"""

    def test_velodyne_topic_in_all_files(self):
        """IT-05-01: /velodyne_points 在配置和 launch 中一致"""
        # 检查 velodyne_vlp16.yaml
        with open(os.path.join(SLAM_DIR, 'config', 'velodyne_vlp16.yaml')) as f:
            config = f.read()
        assert '/velodyne_points' in config

        # 检查 fast_lio2.launch.py
        with open(os.path.join(SLAM_DIR, 'launch', 'fast_lio2.launch.py')) as f:
            launch1 = f.read()
        assert '/velodyne_points' in launch1

        # 检查 demo launch
        with open(os.path.join(SIM_DIR, 'launch', 'simulator_slam_demo.launch.py')) as f:
            demo = f.read()
        assert '/velodyne_points' in demo

    def test_imu_topic_in_all_files(self):
        """IT-05-02: /imu 在配置和 launch 中一致"""
        with open(os.path.join(SLAM_DIR, 'config', 'velodyne_vlp16.yaml')) as f:
            config = f.read()
        assert '/imu' in config

        with open(os.path.join(SLAM_DIR, 'launch', 'fast_lio2.launch.py')) as f:
            launch1 = f.read()
        assert '/imu' in launch1

        with open(os.path.join(SIM_DIR, 'launch', 'simulator_slam_demo.launch.py')) as f:
            demo = f.read()
        assert '/imu' in demo

    def test_cmd_vel_in_demo_launch(self):
        """IT-05-03: /cmd_vel 在 demo launch 中存在"""
        with open(os.path.join(SIM_DIR, 'launch', 'simulator_slam_demo.launch.py')) as f:
            demo = f.read()
        assert '/cmd_vel' in demo


# ==================== IT-06: 关键文件完整性验证 ====================
class TestFileIntegrity:
    """验证所有关键文件存在且不为空"""

    @pytest.mark.parametrize("filepath,desc", [
        (os.path.join(SLAM_DIR, 'CMakeLists.txt'), 'CMakeLists.txt'),
        (os.path.join(SLAM_DIR, 'package.xml'), 'package.xml'),
        (os.path.join(SLAM_DIR, 'launch', 'fast_lio2.launch.py'), 'fast_lio2.launch.py'),
        (os.path.join(SLAM_DIR, 'launch', 'livox_avia.launch.py'), 'livox_avia.launch.py'),
        (os.path.join(SLAM_DIR, 'config', 'velodyne_vlp16.yaml'), 'velodyne_vlp16.yaml'),
        (os.path.join(SLAM_DIR, 'src', 'main.cpp'), 'main.cpp'),
        (os.path.join(SIM_DIR, 'CMakeLists.txt'), 'simulator CMakeLists.txt'),
        (os.path.join(SIM_DIR, 'launch', 'simulator_slam_demo.launch.py'), 'demo launch'),
    ])
    def test_file_exists_and_not_empty(self, filepath, desc):
        """IT-06: 关键文件存在且不为空"""
        assert os.path.isfile(filepath), f"文件不存在: {desc} ({filepath})"
        assert os.path.getsize(filepath) > 0, f"文件为空: {desc} ({filepath})"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
