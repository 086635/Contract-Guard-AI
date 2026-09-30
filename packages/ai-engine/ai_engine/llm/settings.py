"""LLM 接入配置（全仓唯一处）。

设计要点：
1. 默认 provider 为 `offline`，不读 Key、不联网。
2. provider 预设只在构建期展开，业务代码不复制模型名或端点。
3. `.env` 中只允许放真实 Key，代码与测试仅使用占位值。
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from rag_core.config import ProductionConfigError


class LlmSettings(BaseSettings):
    """LLM 接入配置。"""

    model_config = SettingsConfigDict(
        env_prefix="CG_LLM_",
        env_file=".env",
        extra="ignore",
    )

    provider: Literal["offline", "openai", "deepseek", "ollama"] = "offline"
    model: str = "deepseek-flash"
    temperature: float = 0.0
    max_tokens: int = 2048
    timeout_s: float = 30.0
    max_retries: int = 2
    json_mode: Literal["json_schema", "json_mode", "none"] = "json_mode"
    api_key: str | None = None
    base_url: str | None = None

    def validate_production(self) -> None:
        """生产环境拒绝 offline provider。"""
        if self.provider == "offline":
            raise ProductionConfigError(
                "CG_LLM_PROVIDER",
                "production forbids offline provider",
            )


def get_llm_settings() -> LlmSettings:
    """读取当前环境下的 LLM 配置。"""
    return LlmSettings()


__all__ = ["LlmSettings", "get_llm_settings"]
