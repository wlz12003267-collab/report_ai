"""集中管理配置，从环境变量加载。"""
import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM / Embedding
    llm_backend: str                        # vllm | local
    llm_base_url: str  
    llm_api_key: str 
    llm_model_name: str 
    llm_model_path: str                       # 仅 local 模式用
    embedding_model_path: str                                          

    # MySQL
    mysql_host: str
    mysql_port: int 
    mysql_user: str
    mysql_password: str
    mysql_database: str

    # PostgreSQL
    pg_host: str
    pg_port: int 
    pg_user: str
    pg_password: str
    pg_database: str

    # IoTDB
    iotdb_host: str
    iotdb_port: int 
    iotdb_user: str
    iotdb_password: str
    iotdb_database: str

    # 默认查询范围（从测试文件提取）
    factory_code: str = "C"
    workshop_id: str = "1689182658069925888"
    report_root: str = "reports"
    
    # LangSmith
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "fh-v3-report-agent"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings