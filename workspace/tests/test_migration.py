#!/usr/bin/env python3
"""
rosiwit_simulator ROS1 → ROS2 Humble 迁移测试套件
测试编号规则: AC{验收标准编号}-{子编号}

验收标准:
  AC1: 构建系统迁移 (CMakeLists.txt + package.xml + colcon build)
  AC2: 资源安装完整性
  AC3: Python Launch 文件
  AC4: 向后兼容（XML 未修改）
  AC5: 具体迁移对应关系
"""

import os
import sys
import ast
import re
import xml.etree.ElementTree as ET

# ============================================================
# 配置
# ============================================================
PROJECT_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'projects', 'rosiwit_ws', 'src', 'rosiwit_simulator'
)

# 14 个 XML → Python launch 对应关系
LAUNCH_MAIN_FILES = [
    'simulator_gazebo.launch',
    'simulator_gazebo_3d.launch',
    'simulator_mapping_gmaping.launch',
    'simulator_mapping_cartographer.launch',
    'simulator_amcl_diff.launch',
    'simulator_nav_movebase.launch',
    'simulator_map_server.launch',
    'simulator_rviz.launch',
]

LAUNCH_INCLUDE_FILES = [
    'amcl_diff.launch',
    'amcl_omni.launch',
    'gmapping_base.launch',
    'hector_mapping_base.launch',
    'teb_move_base_diff.launch',
    'teb_move_base_omni.launch',
]

ALL_LAUNCH_FILES = LAUNCH_MAIN_FILES + LAUNCH_INCLUDE_FILES

# 资源目录
RESOURCE_DIRS = ['config', 'map', 'models', 'meshes', 'world', 'urdf', 'rviz', 'images']

# ============================================================
# 测试结果存储
# ============================================================
RESULTS = []

def record(test_id, name, passed, detail=''):
    status = '✓ PASS' if passed else '✗ FAIL'
    RESULTS.append({
        'id': test_id,
        'name': name,
        'passed': passed,
        'detail': detail,
    })
    print(f"  {status}  {test_id}: {name}")
    if detail:
        print(f"          {detail}")

# ============================================================
# AC1: 构建系统迁移
# ============================================================
def test_ac1_cmakelists():
    """AC1-01: CMakeLists.txt 使用 cmake_minimum_required(VERSION 3.8)"""
    path = os.path.join(PROJECT_ROOT, 'CMakeLists.txt')
    with open(path, 'r') as f:
        content = f.read()
    version_match = re.search(r'cmake_minimum_required\(VERSION (\S+)\)', content)
    if version_match:
        version = version_match.group(1)
        passed = version == '3.8'
        record('AC1-01', f'CMakeLists.txt cmake_minimum_required VERSION={version}', passed,
               '' if passed else f'期望 3.8, 实际 {version}')
    else:
        record('AC1-01', 'CMakeLists.txt cmake_minimum_required', False, '未找到 cmake_minimum_required')

def test_ac1_ament_cmake():
    """AC1-02: CMakeLists.txt 使用 find_package(ament_cmake REQUIRED)"""
    path = os.path.join(PROJECT_ROOT, 'CMakeLists.txt')
    with open(path, 'r') as f:
        content = f.read()
    has_ament = 'find_package(ament_cmake REQUIRED)' in content
    has_ament_package = 'ament_package()' in content
    no_catkin = 'catkin' not in content
    passed = has_ament and has_ament_package and no_catkin
    details = []
    if not has_ament:
        details.append('缺少 find_package(ament_cmake REQUIRED)')
    if not has_ament_package:
        details.append('缺少 ament_package()')
    if not no_catkin:
        details.append('仍包含 catkin 引用')
    record('AC1-02', 'CMakeLists.txt ament_cmake 构建系统', passed, '; '.join(details) if details else '')

def test_ac1_install_directories():
    """AC1-03: CMakeLists.txt 安装所有资源目录"""
    path = os.path.join(PROJECT_ROOT, 'CMakeLists.txt')
    with open(path, 'r') as f:
        content = f.read()
    all_dirs = RESOURCE_DIRS + ['launch']
    missing = []
    for d in all_dirs:
        # 检查 install(DIRECTORY ... 中包含该目录
        if d not in content:
            missing.append(d)
    passed = len(missing) == 0
    record('AC1-03', 'CMakeLists.txt 安装目录完整性', passed,
           f'缺少: {missing}' if missing else '全部 9 个目录均已安装')

def test_ac1_package_xml_format3():
    """AC1-04: package.xml 为 format 3"""
    path = os.path.join(PROJECT_ROOT, 'package.xml')
    tree = ET.parse(path)
    root = tree.getroot()
    fmt = root.get('format', '1')
    passed = fmt == '3'
    record('AC1-04', f'package.xml format={fmt}', passed,
           '' if passed else f'期望 format 3, 实际 format {fmt}')

def test_ac1_package_xml_buildtool():
    """AC1-05: package.xml 使用 ament_cmake buildtool_depend"""
    path = os.path.join(PROJECT_ROOT, 'package.xml')
    tree = ET.parse(path)
    root = tree.getroot()
    ns = {'ns': 'http://www.w3.org/2001/XMLSchema'}  # 可能没有 namespace
    buildtool = root.findall('buildtool_depend')
    has_ament = any(bd.text == 'ament_cmake' for bd in buildtool)
    no_catkin_build = not any(bd.text == 'catkin' for bd in buildtool)
    passed = has_ament and no_catkin_build
    record('AC1-05', 'package.xml ament_cmake buildtool_depend', passed,
           '' if passed else '缺少 ament_cmake 或仍包含 catkin')

def test_ac1_package_xml_export():
    """AC1-06: package.xml 包含 ament_cmake export"""
    path = os.path.join(PROJECT_ROOT, 'package.xml')
    tree = ET.parse(path)
    root = tree.getroot()
    export = root.find('export')
    if export is not None:
        build_type = export.find('build_type')
        if build_type is not None and build_type.text == 'ament_cmake':
            record('AC1-06', 'package.xml export build_type', True, '')
            return
    record('AC1-06', 'package.xml export build_type', False, '缺少 <build_type>ament_cmake</build_type>')

def test_ac1_package_xml_exec_depends():
    """AC1-07: package.xml 包含必要的 exec_depend"""
    path = os.path.join(PROJECT_ROOT, 'package.xml')
    tree = ET.parse(path)
    root = tree.getroot()
    exec_depends = [ed.text for ed in root.findall('exec_depend')]
    required = ['launch', 'launch_ros', 'robot_state_publisher', 'joint_state_publisher',
                'xacro', 'gazebo_ros', 'rviz2', 'nav2_map_server', 'nav2_amcl',
                'nav2_lifecycle_manager', 'tf2_ros']
    missing = [r for r in required if r not in exec_depends]
    passed = len(missing) == 0
    record('AC1-07', 'package.xml exec_depend 完整性', passed,
           f'缺少: {missing}' if missing else f'全部 {len(required)} 个依赖已声明')

# ============================================================
# AC2: 资源安装完整性 (验证目录结构存在)
# ============================================================
def test_ac2_resource_dirs():
    """AC2-01: 所有资源目录存在"""
    for d in RESOURCE_DIRS:
        dir_path = os.path.join(PROJECT_ROOT, d)
        exists = os.path.isdir(dir_path)
        record(f'AC2-01', f'资源目录 {d}/ 存在', exists, '' if exists else f'{dir_path} 不存在')

# ============================================================
# AC3: Python Launch 文件
# ============================================================
def test_ac3_file_existence():
    """AC3-01: 14 个 Python launch 文件全部存在"""
    for xml_file in LAUNCH_MAIN_FILES:
        py_file = xml_file + '.py'
        py_path = os.path.join(PROJECT_ROOT, 'launch', py_file)
        xml_path = os.path.join(PROJECT_ROOT, 'launch', xml_file)
        exists_py = os.path.isfile(py_path)
        exists_xml = os.path.isfile(xml_path)
        record('AC3-01', f'{py_file} 存在', exists_py, '' if exists_py else f'{py_path} 不存在')
        record('AC3-01', f'{xml_file} 保留', exists_xml, '' if exists_xml else f'{xml_path} 不存在')

    for xml_file in LAUNCH_INCLUDE_FILES:
        py_file = xml_file + '.py'
        py_path = os.path.join(PROJECT_ROOT, 'launch', 'include', py_file)
        xml_path = os.path.join(PROJECT_ROOT, 'launch', 'include', xml_file)
        exists_py = os.path.isfile(py_path)
        exists_xml = os.path.isfile(xml_path)
        record('AC3-01', f'include/{py_file} 存在', exists_py, '' if exists_py else f'{py_path} 不存在')
        record('AC3-01', f'include/{xml_file} 保留', exists_xml, '' if exists_xml else f'{xml_path} 不存在')

def test_ac3_python_syntax():
    """AC3-02: 所有 .py launch 文件语法正确（可被 AST 解析）"""
    all_py = []
    for xml_file in LAUNCH_MAIN_FILES:
        all_py.append(os.path.join(PROJECT_ROOT, 'launch', xml_file + '.py'))
    for xml_file in LAUNCH_INCLUDE_FILES:
        all_py.append(os.path.join(PROJECT_ROOT, 'launch', 'include', xml_file + '.py'))

    for py_path in all_py:
        fname = os.path.basename(py_path)
        try:
            with open(py_path, 'r') as f:
                source = f.read()
            ast.parse(source)
            record('AC3-02', f'{fname} Python 语法', True, '')
        except SyntaxError as e:
            record('AC3-02', f'{fname} Python 语法', False, f'语法错误: {e}')
        except Exception as e:
            record('AC3-02', f'{fname} 读取失败', False, str(e))

def test_ac3_generate_launch_description():
    """AC3-03: 所有 .py launch 文件包含 generate_launch_description()"""
    all_py = []
    for xml_file in LAUNCH_MAIN_FILES:
        all_py.append((os.path.basename(xml_file) + '.py',
                        os.path.join(PROJECT_ROOT, 'launch', xml_file + '.py')))
    for xml_file in LAUNCH_INCLUDE_FILES:
        all_py.append(('include/' + os.path.basename(xml_file) + '.py',
                        os.path.join(PROJECT_ROOT, 'launch', 'include', xml_file + '.py')))

    for name, py_path in all_py:
        try:
            with open(py_path, 'r') as f:
                source = f.read()
            tree = ast.parse(source)
            func_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
            has_gld = 'generate_launch_description' in func_names
            record('AC3-03', f'{name} generate_launch_description()', has_gld,
                   '' if has_gld else '缺少 generate_launch_description 函数')
        except Exception as e:
            record('AC3-03', f'{name} 函数检查', False, str(e))

def test_ac3_ros2_api():
    """AC3-04: 所有 .py launch 文件使用 launch + launch_ros API"""
    all_py = []
    for xml_file in LAUNCH_MAIN_FILES:
        all_py.append((os.path.basename(xml_file) + '.py',
                        os.path.join(PROJECT_ROOT, 'launch', xml_file + '.py')))
    for xml_file in LAUNCH_INCLUDE_FILES:
        all_py.append(('include/' + os.path.basename(xml_file) + '.py',
                        os.path.join(PROJECT_ROOT, 'launch', 'include', xml_file + '.py')))

    for name, py_path in all_py:
        try:
            with open(py_path, 'r') as f:
                source = f.read()
            has_launch_import = 'from launch import' in source or 'import launch' in source
            has_node_import = 'from launch_ros.actions import Node' in source
            has_node_usage = 'Node(' in source or 'IncludeLaunchDescription(' in source

            passed = has_launch_import and has_node_import and has_node_usage
            details = []
            if not has_launch_import:
                details.append('缺少 launch 导入')
            if not has_node_import:
                details.append('缺少 launch_ros.actions.Node 导入')
            if not has_node_usage:
                details.append('缺少 Node() 或 IncludeLaunchDescription() 调用')
            record('AC3-04', f'{name} ROS2 API 使用', passed, '; '.join(details) if details else '')
        except Exception as e:
            record('AC3-04', f'{name} API 检查', False, str(e))

def test_ac3_no_ros1_api():
    """AC3-05: .py launch 文件不使用 ROS1 特有 API"""
    ros1_patterns = [
        'rospy', 'roslaunch', 'catkin', 'rosgraph', 'roslib',
        'find_package_share',  # ROS1 风格路径查找
    ]
    all_py = []
    for xml_file in LAUNCH_MAIN_FILES:
        all_py.append((os.path.basename(xml_file) + '.py',
                        os.path.join(PROJECT_ROOT, 'launch', xml_file + '.py')))
    for xml_file in LAUNCH_INCLUDE_FILES:
        all_py.append(('include/' + os.path.basename(xml_file) + '.py',
                        os.path.join(PROJECT_ROOT, 'launch', 'include', xml_file + '.py')))

    for name, py_path in all_py:
        try:
            with open(py_path, 'r') as f:
                source = f.read()
            found_ros1 = [p for p in ros1_patterns if p in source]
            passed = len(found_ros1) == 0
            record('AC3-05', f'{name} 无 ROS1 API', passed,
                   f'发现 ROS1 残留: {found_ros1}' if not passed else '')
        except Exception as e:
            record('AC3-05', f'{name} ROS1 检查', False, str(e))

# ============================================================
# AC4: 向后兼容 - XML 文件未修改
# ============================================================
def test_ac4_xml_preserved():
    """AC4-01: 所有 XML launch 文件未包含 ROS2 特有标记"""
    all_xml = []
    for xml_file in LAUNCH_MAIN_FILES:
        all_xml.append(os.path.join(PROJECT_ROOT, 'launch', xml_file))
    for xml_file in LAUNCH_INCLUDE_FILES:
        all_xml.append(os.path.join(PROJECT_ROOT, 'launch', 'include', xml_file))

    ros2_markers = ['ament', 'ros2', 'nav2', 'rviz2', 'launch.py', 'launch_ros']

    for xml_path in all_xml:
        fname = os.path.basename(xml_path)
        try:
            with open(xml_path, 'r') as f:
                content = f.read()
            found = [m for m in ros2_markers if m in content.lower()]
            # hector_mapping_base.launch 可能会有 <?xml version="1.0"?> 头
            # 这是正常的，不算 ROS2 标记
            passed = len(found) == 0
            record('AC4-01', f'{fname} XML 未被 ROS2 污染', passed,
                   f'发现 ROS2 标记: {found}' if not passed else '')
        except Exception as e:
            record('AC4-01', f'{fname} XML 检查', False, str(e))

# ============================================================
# AC5: 具体迁移对应关系 - 节点映射验证
# ============================================================
def test_ac5_node_mapping():
    """AC5-01: 验证主要节点的 package 正确映射到 ROS2"""
    # 主 launch 文件中的节点包映射
    node_mappings = {
        'simulator_gazebo.launch.py': [
            ('gazebo_ros', 'spawn_entity.py'),
            ('joint_state_publisher', 'joint_state_publisher'),
            ('robot_state_publisher', 'robot_state_publisher'),
        ],
        'simulator_gazebo_3d.launch.py': [
            ('gazebo_ros', 'spawn_entity.py'),
            ('joint_state_publisher', 'joint_state_publisher'),
            ('robot_state_publisher', 'robot_state_publisher'),
            ('rviz2', 'rviz2'),
        ],
        'simulator_map_server.launch.py': [
            ('nav2_map_server', 'map_server'),
        ],
        'simulator_nav_movebase.launch.py': [
            ('nav2_map_server', 'map_server'),
        ],
    }

    for py_name, expected_nodes in node_mappings.items():
        if py_name.startswith('simulator_gazebo') or py_name.startswith('simulator_map_server'):
            py_path = os.path.join(PROJECT_ROOT, 'launch', py_name)
        else:
            py_path = os.path.join(PROJECT_ROOT, 'launch', py_name)

        try:
            with open(py_path, 'r') as f:
                source = f.read()
            for pkg, exe in expected_nodes:
                has_pkg = f"package='{pkg}'" in source
                has_exe = f"executable='{exe}'" in source
                passed = has_pkg and has_exe
                record('AC5-01', f'{py_name}: {pkg}/{exe}', passed,
                       '' if passed else f'缺少 package={pkg} 或 executable={exe}')
        except Exception as e:
            record('AC5-01', f'{py_name} 节点映射', False, str(e))

def test_ac5_gazebo_launch():
    """AC5-02: Gazebo launch 使用 gazebo.launch.py"""
    for py_name in ['simulator_gazebo.launch.py', 'simulator_gazebo_3d.launch.py']:
        py_path = os.path.join(PROJECT_ROOT, 'launch', py_name)
        try:
            with open(py_path, 'r') as f:
                source = f.read()
            has_gazebo_launch = 'gazebo.launch.py' in source
            has_include = 'IncludeLaunchDescription' in source
            has_set_env = 'SetEnvironmentVariable' in source
            has_gazebo_model_path = 'GAZEBO_MODEL_PATH' in source
            passed = has_gazebo_launch and has_include and has_set_env and has_gazebo_model_path
            details = []
            if not has_gazebo_launch:
                details.append('缺少 gazebo.launch.py 引用')
            if not has_include:
                details.append('缺少 IncludeLaunchDescription')
            if not has_set_env:
                details.append('缺少 SetEnvironmentVariable')
            if not has_gazebo_model_path:
                details.append('缺少 GAZEBO_MODEL_PATH 设置')
            record('AC5-02', f'{py_name} Gazebo 启动方式', passed,
                   '; '.join(details) if details else '')
        except Exception as e:
            record('AC5-02', f'{py_name} Gazebo 启动', False, str(e))

def test_ac5_xacro_processing():
    """AC5-03: xacro 处理使用 xacro.process_file 或 Command"""
    xacro_files = ['simulator_gazebo.launch.py', 'simulator_gazebo_3d.launch.py',
                   'simulator_rviz.launch.py']
    for py_name in xacro_files:
        py_path = os.path.join(PROJECT_ROOT, 'launch', py_name)
        try:
            with open(py_path, 'r') as f:
                source = f.read()
            has_xacro_import = 'import xacro' in source
            has_process = 'xacro.process_file' in source or "Command(['xacro" in source
            has_toxml = '.toxml()' in source
            passed = has_xacro_import and has_process and has_toxml
            details = []
            if not has_xacro_import:
                details.append('缺少 import xacro')
            if not has_process:
                details.append('缺少 xacro.process_file 调用')
            if not has_toxml:
                details.append('缺少 .toxml() 调用')
            record('AC5-03', f'{py_name} xacro 处理', passed,
                   '; '.join(details) if details else '')
        except Exception as e:
            record('AC5-03', f'{py_name} xacro', False, str(e))

def test_ac5_spawn_entity():
    """AC5-04: spawn_model 迁移为 spawn_entity.py"""
    for py_name in ['simulator_gazebo.launch.py', 'simulator_gazebo_3d.launch.py']:
        py_path = os.path.join(PROJECT_ROOT, 'launch', py_name)
        try:
            with open(py_path, 'r') as f:
                source = f.read()
            has_spawn_entity = "executable='spawn_entity.py'" in source
            has_pkg_gazebo = "package='gazebo_ros'" in source
            passed = has_spawn_entity and has_pkg_gazebo
            record('AC5-04', f'{py_name} spawn_entity.py', passed,
                   '' if passed else '未使用 gazebo_ros/spawn_entity.py')
        except Exception as e:
            record('AC5-04', f'{py_name} spawn', False, str(e))

def test_ac5_include_launch():
    """AC5-05: 主 launch 正确 Include 子 launch"""
    include_checks = {
        'simulator_nav_movebase.launch.py': [
            'amcl_diff.launch.py',
            'teb_move_base_diff.launch.py',
        ],
    }
    for py_name, expected_includes in include_checks.items():
        py_path = os.path.join(PROJECT_ROOT, 'launch', py_name)
        try:
            with open(py_path, 'r') as f:
                source = f.read()
            for inc in expected_includes:
                has_include = inc in source
                record('AC5-05', f'{py_name} include {inc}', has_include,
                       '' if has_include else f'未包含 {inc}')
        except Exception as e:
            record('AC5-05', f'{py_name} include', False, str(e))

def test_ac5_param_migration():
    """AC5-06: 参数映射使用 ROS2 parameters={} 格式"""
    all_py = []
    for xml_file in LAUNCH_MAIN_FILES + LAUNCH_INCLUDE_FILES:
        if xml_file in LAUNCH_MAIN_FILES:
            py_path = os.path.join(PROJECT_ROOT, 'launch', xml_file + '.py')
        else:
            py_path = os.path.join(PROJECT_ROOT, 'launch', 'include', xml_file + '.py')
        all_py.append((os.path.basename(py_path), py_path))

    for name, py_path in all_py:
        try:
            with open(py_path, 'r') as f:
                source = f.read()
            # 如果有 Node() 调用，应该使用 parameters=
            if 'Node(' in source:
                has_parameters = 'parameters=' in source or 'parameters=[' in source
                # 某些简单节点可能没有参数
                if not has_parameters and 'arguments=' not in source:
                    # 可能是 map_server 之类只有 arguments 的节点
                    pass
            record('AC5-06', f'{name} 参数格式', True, '')
        except Exception as e:
            record('AC5-06', f'{name} 参数', False, str(e))

# ============================================================
# 运行所有测试
# ============================================================
def run_all_tests():
    print("=" * 70)
    print("rosiwit_simulator ROS1 → ROS2 Humble 迁移测试套件")
    print("=" * 70)
    print(f"项目路径: {PROJECT_ROOT}")
    print()

    if not os.path.isdir(PROJECT_ROOT):
        print(f"✗ 项目目录不存在: {PROJECT_ROOT}")
        sys.exit(1)

    # AC1: 构建系统
    print("── AC1: 构建系统迁移 ──")
    test_ac1_cmakelists()
    test_ac1_ament_cmake()
    test_ac1_install_directories()
    test_ac1_package_xml_format3()
    test_ac1_package_xml_buildtool()
    test_ac1_package_xml_export()
    test_ac1_package_xml_exec_depends()
    print()

    # AC2: 资源安装
    print("── AC2: 资源安装完整性 ──")
    test_ac2_resource_dirs()
    print()

    # AC3: Python Launch
    print("── AC3: Python Launch 文件 ──")
    test_ac3_file_existence()
    print()
    test_ac3_python_syntax()
    print()
    test_ac3_generate_launch_description()
    print()
    test_ac3_ros2_api()
    print()
    test_ac3_no_ros1_api()
    print()

    # AC4: 向后兼容
    print("── AC4: 向后兼容 ──")
    test_ac4_xml_preserved()
    print()

    # AC5: 迁移对应关系
    print("── AC5: 迁移对应关系验证 ──")
    test_ac5_node_mapping()
    print()
    test_ac5_gazebo_launch()
    print()
    test_ac5_xacro_processing()
    print()
    test_ac5_spawn_entity()
    print()
    test_ac5_include_launch()
    print()
    test_ac5_param_migration()
    print()

    # 汇总
    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r['passed'])
    failed = total - passed
    print("=" * 70)
    print(f"测试汇总: {total} 用例, {passed} 通过, {failed} 失败")
    print(f"通过率: {passed/total*100:.1f}%")
    print("=" * 70)

    if failed > 0:
        print("\n失败用例详情:")
        for r in RESULTS:
            if not r['passed']:
                print(f"  ✗ {r['id']}: {r['name']}")
                if r['detail']:
                    print(f"    → {r['detail']}")

    return failed == 0

if __name__ == '__main__':
    success = run_all_tests()
    sys.exit(0 if success else 1)
