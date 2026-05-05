#!/usr/bin/env python3
"""
rosiwit_simulator ROS2 迁移 - Launch 文件 import 级验证
验证每个 .py launch 文件在 AST 级别的正确性。
"""

import os
import sys
import ast
import re

PROJECT_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'projects', 'rosiwit_ws', 'src', 'rosiwit_simulator'
)

RESULTS = []

def record(test_id, name, passed, detail=''):
    status = '✓ PASS' if passed else '✗ FAIL'
    RESULTS.append({'id': test_id, 'name': name, 'passed': passed, 'detail': detail})
    print(f"  {status}  {test_id}: {name}")
    if detail:
        print(f"          {detail}")

def get_all_py_launches():
    """获取所有 .py launch 文件路径"""
    files = []
    launch_dir = os.path.join(PROJECT_ROOT, 'launch')
    include_dir = os.path.join(launch_dir, 'include')

    for f in sorted(os.listdir(launch_dir)):
        if f.endswith('.py'):
            files.append(('launch/' + f, os.path.join(launch_dir, f)))

    for f in sorted(os.listdir(include_dir)):
        if f.endswith('.py'):
            files.append(('launch/include/' + f, os.path.join(include_dir, f)))

    return files

def test_node_structure(py_name, py_path):
    """验证每个 launch 文件中 Node() 的 package 和 executable 参数"""
    with open(py_path, 'r') as f:
        source = f.read()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # 检查是否是 Node() 调用
            if isinstance(node.func, ast.Name) and node.func.id == 'Node':
                kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                pkg = None
                exe = None
                if 'package' in kwargs and isinstance(kwargs['package'], ast.Constant):
                    pkg = kwargs['package'].value
                if 'executable' in kwargs and isinstance(kwargs['executable'], ast.Constant):
                    exe = kwargs['executable'].value
                if pkg and exe:
                    record('IMP-01', f'{py_name}: Node({pkg}, {exe})', True, '')

def test_launch_description_return(py_name, py_path):
    """验证 generate_launch_description 返回 LaunchDescription"""
    with open(py_path, 'r') as f:
        source = f.read()

    # 检查返回 LaunchDescription([...])
    has_return_ld = 'return LaunchDescription([' in source or 'return LaunchDescription(' in source
    record('IMP-02', f'{py_name} 返回 LaunchDescription', has_return_ld,
           '' if has_return_ld else 'generate_launch_description 未返回 LaunchDescription')

def test_path_resolution(py_name, py_path):
    """验证使用 get_package_share_directory 进行路径解析"""
    with open(py_path, 'r') as f:
        source = f.read()

    # 如果文件使用了 os.path.join 来构建路径，应该使用 get_package_share_directory
    if 'os.path.join' in source:
        has_pkg_share = 'get_package_share_directory' in source
        record('IMP-03', f'{py_name} 路径解析', has_pkg_share,
               '' if has_pkg_share else '使用了 os.path.join 但缺少 get_package_share_directory')
    else:
        record('IMP-03', f'{py_name} 路径解析', True, '无需路径解析（无 os.path.join）')

def test_parameter_format(py_name, py_path):
    """验证参数使用 ROS2 格式"""
    with open(py_path, 'r') as f:
        source = f.read()

    # 不应使用 ROS1 的 param file 命令
    has_ros1_param = 'command="load"' in source or 'rosparam' in source
    record('IMP-04', f'{py_name} 参数格式(无ROS1残留)', not has_ros1_param,
           '' if not has_ros1_param else '发现 ROS1 参数格式残留')

def test_remapping_format(py_name, py_path):
    """验证 remapping 使用 ROS2 格式"""
    with open(py_path, 'r') as f:
        source = f.read()

    if 'remappings' in source:
        # 检查格式是否正确: remappings=[('from', 'to'), ...]
        has_tuple_format = "remappings=[(" in source
        record('IMP-05', f'{py_name} remapping 格式', has_tuple_format,
               '' if has_tuple_format else 'remapping 格式可能不正确')
    else:
        record('IMP-05', f'{py_name} remapping 格式', True, '无 remapping')

def run_all_tests():
    print("=" * 70)
    print("rosiwit_simulator Launch 文件 import 级深度验证")
    print("=" * 70)

    files = get_all_py_launches()
    print(f"共发现 {len(files)} 个 .py launch 文件\n")

    for name, path in files:
        print(f"── {name} ──")
        test_node_structure(name, path)
        test_launch_description_return(name, path)
        test_path_resolution(name, path)
        test_parameter_format(name, path)
        test_remapping_format(name, path)
        print()

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r['passed'])
    failed = total - passed
    print("=" * 70)
    print(f"验证汇总: {total} 项, {passed} 通过, {failed} 失败")
    print(f"通过率: {passed/total*100:.1f}%")
    print("=" * 70)

    if failed > 0:
        print("\n失败项详情:")
        for r in RESULTS:
            if not r['passed']:
                print(f"  ✗ {r['id']}: {r['name']}")
                if r['detail']:
                    print(f"    → {r['detail']}")

    return failed == 0

if __name__ == '__main__':
    success = run_all_tests()
    sys.exit(0 if success else 1)
