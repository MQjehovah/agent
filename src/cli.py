import argparse


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", "-c", default="config",
                        help="配置目录，包含PROMPT.md、agents/、skills/等 (默认: ./config)")
    parser.add_argument("--workspace", "-w", default=".",
                        help="工作目录，agent 在此目录下读写文件 (默认: 当前目录)")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-plugins", action="store_true")
    parser.add_argument("--skip-config-check", action="store_true")
    parser.add_argument(
        "--mode", "-m",
        choices=["interactive", "autonomous"],
        default="interactive",
        help="运行模式",
    )
    parser.add_argument("--agent", "-a", default="",
                        help="指定子代理名称执行任务")
    parser.add_argument("--web", action="store_true", help="启动Web UI前端")
    parser.add_argument("--web-port", type=int, default=8080, help="Web UI端口 (默认8080)")
    parser.add_argument("--no-web", action="store_true", help="禁用Web UI前端")
    parser.add_argument("task", nargs="*", help="要执行的任务内容")
    return parser.parse_args(argv)
