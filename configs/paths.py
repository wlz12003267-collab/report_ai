"""项目路径常量。"""
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_DIR = os.path.join(PROJECT_ROOT, "agent_core")
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "templates")
CONFIG_DIR = os.path.join(PROJECT_ROOT, "configs")
TEST_DIR = os.path.join(PROJECT_ROOT, "tests")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

for _d in [AGENT_DIR, REPORTS_DIR, TEMPLATES_DIR, TEST_DIR, LOGS_DIR, CONFIG_DIR]:
    os.makedirs(_d, exist_ok=True)