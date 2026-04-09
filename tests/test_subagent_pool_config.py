"""子代理并发池配置模块测试"""
import json
import os
import tempfile
import pytest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config.subagent_pool import SubagentPoolConfig


class TestSubagentPoolConfigDefault:
    """默认配置创建测试"""

    def test_default_values(self):
        config = SubagentPoolConfig()
        assert config.max_concurrency == 3
        assert config.queue_size == 50
        assert config.timeout == 300
        assert config.enable_priority is True


class TestSubagentPoolConfigLoad:
    """从有效 JSON 文件加载测试"""

    def test_load_full_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_data = {
                "max_concurrency": 5,
                "queue_size": 100,
                "timeout": 600,
                "enable_priority": False,
            }
            config_file = os.path.join(tmpdir, "subagent_pool.json")
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(config_data, f)

            config = SubagentPoolConfig.load(tmpdir)
            assert config.max_concurrency == 5
            assert config.queue_size == 100
            assert config.timeout == 600
            assert config.enable_priority is False

    def test_load_partial_config(self):
        """部分配置时，缺失字段使用默认值"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_data = {"max_concurrency": 10}
            config_file = os.path.join(tmpdir, "subagent_pool.json")
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(config_data, f)

            config = SubagentPoolConfig.load(tmpdir)
            assert config.max_concurrency == 10
            assert config.queue_size == 50
            assert config.timeout == 300
            assert config.enable_priority is True


class TestSubagentPoolConfigFallback:
    """配置加载失败时的回退测试"""

    def test_fallback_on_missing_file(self):
        """文件不存在时返回默认配置"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = SubagentPoolConfig.load(tmpdir)
            assert config.max_concurrency == 3
            assert config.queue_size == 50

    def test_fallback_on_invalid_json(self):
        """JSON 格式错误时返回默认配置"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = os.path.join(tmpdir, "subagent_pool.json")
            with open(config_file, "w", encoding="utf-8") as f:
                f.write("{ invalid json !!!")

            config = SubagentPoolConfig.load(tmpdir)
            assert config.max_concurrency == 3
            assert config.queue_size == 50

    def test_ignores_unknown_fields(self):
        """未知字段应被忽略，不影响加载"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_data = {
                "max_concurrency": 7,
                "unknown_field": "should_be_ignored",
                "another_unknown": 42,
            }
            config_file = os.path.join(tmpdir, "subagent_pool.json")
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(config_data, f)

            config = SubagentPoolConfig.load(tmpdir)
            assert config.max_concurrency == 7
            assert not hasattr(config, "unknown_field")
            assert not hasattr(config, "another_unknown")
