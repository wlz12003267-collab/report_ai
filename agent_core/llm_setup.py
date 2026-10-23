"""LLM 加载与连接。

策略：
- vLLM 模式：仅通过 HTTP 调用独立推理服务
- ollama 模式：通过 HTTP 调用 ollama 服务
- local 模式：仅在显式配置时加载本地模型
"""
from functools import lru_cache
import socket
from urllib.parse import urlparse

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline
from langchain_huggingface import HuggingFacePipeline, ChatHuggingFace
from langchain_openai import ChatOpenAI

from configs.settings import get_settings


# ============================================================
# 网络探测
# ============================================================
def can_reach(base_url: str, timeout: float = 1.5) -> bool:
    """探测 base_url 是否可连接。"""
    if not base_url:
        return False
    try:
        parsed = urlparse(base_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ============================================================
# 本地 LLM（首次调用加载，后续走缓存）
# ============================================================
@lru_cache(maxsize=1)
def load_llm() -> ChatHuggingFace:
    s = get_settings()
    print(f"加载本地LLM: {s.llm_model_path}")

    tokenizer = AutoTokenizer.from_pretrained(
        s.llm_model_path, trust_remote_code=True
    )
    model = AutoModelForCausalLM.from_pretrained(
        s.llm_model_path,
        torch_dtype=torch.float16,
        device_map="auto",
        trust_remote_code=True,
    )
    pipe = pipeline(
        "text-generation",
        model=model,
        tokenizer=tokenizer,
        max_new_tokens=2048,
        temperature=0.1,
        top_p=0.9,
        repetition_penalty=1.05,
        return_full_text=False,
    )
    llm = HuggingFacePipeline(pipeline=pipe)
    return ChatHuggingFace(llm=llm)


# ============================================================
# 远端 LLM（vLLM / ollama）
# ============================================================
def build_remote_llm(base_url: str, model: str, api_key: str) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=0.1,
        timeout=180,
        max_retries=2,
    )

# ============================================================
# 主入口：有网络优先远端，无网络回退本地
# ============================================================
@lru_cache(maxsize=1)
def get_llm():
    s = get_settings()
    # 1) 配置为 vLLM（独立推理服务）
    if s.llm_backend == "vllm":
        if can_reach(s.llm_base_url):
            print(f"连接vLLM服务: {s.llm_base_url} / {s.llm_model_name}")
            return build_remote_llm(s.llm_base_url, s.llm_model_name, s.llm_api_key)
        raise ConnectionError(f"vLLM 服务不可达: {s.llm_base_url}")
    # 2) ollama
    if s.llm_backend == "ollama":
        if can_reach(s.llm_base_url):
            print(f"连接Ollama服务: {s.llm_base_url} / {s.llm_model_name}")
            return build_remote_llm(s.llm_base_url, s.llm_model_name, s.llm_api_key)
        raise ConnectionError(f"Ollama 服务不可达: {s.llm_base_url}")
    # 3) 显式 local 模式
    if s.llm_backend == "local":
        print("使用本地transformers模型")
        return load_llm()
    raise ValueError(f"不支持的 llm_backend: {s.llm_backend}")

# if __name__ == "__main__":
#     llm = get_llm()
#     print("successful")