from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx


DEFAULT_BAILIAN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
PROVIDER_LABELS = {
    "bailian": "阿里云百炼",
    "doubao": "豆包",
    "openai-compatible": "兼容模型",
}
DEFAULT_CHAT_MAX_OUTPUT_TOKENS = 4_096
DEFAULT_DOCUMENT_MAX_OUTPUT_TOKENS = 16_384
MAX_OUTPUT_TOKENS = 131_072


@dataclass(frozen=True)
class AssistantConfig:
    provider: str
    base_url: str
    api_key: str
    model: str

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, "兼容模型")


class AssistantConfigurationError(RuntimeError):
    pass


class AssistantProviderError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def output_token_limit(mode: str) -> int:
    if mode == "chat":
        return DEFAULT_CHAT_MAX_OUTPUT_TOKENS
    try:
        configured = int(os.getenv("AI_MAX_OUTPUT_TOKENS", str(DEFAULT_DOCUMENT_MAX_OUTPUT_TOKENS)))
    except (TypeError, ValueError):
        configured = DEFAULT_DOCUMENT_MAX_OUTPUT_TOKENS
    return min(MAX_OUTPUT_TOKENS, max(16, configured))


def get_assistant_config() -> AssistantConfig:
    provider = os.getenv("AI_PROVIDER", "bailian").strip().lower() or "bailian"
    default_url = DEFAULT_BAILIAN_BASE_URL if provider == "bailian" else ""
    return AssistantConfig(
        provider=provider,
        base_url=(os.getenv("AI_API_BASE_URL", "").strip() or default_url).rstrip("/"),
        api_key=os.getenv("AI_API_KEY", "").strip(),
        model=os.getenv("AI_MODEL", "").strip(),
    )


def assistant_status() -> dict[str, object]:
    config = get_assistant_config()
    return {
        "configured": config.ready,
        "provider": config.provider_label,
        "model": config.model,
    }


def build_system_prompt(mode: str) -> str:
    mode_instructions = {
        "auto": (
            "先判断用户是要从零写作还是修改已有内容，再识别公文、简历、文章、推文、演讲稿、作文、故事等场景。"
            "根据目的、读者、体裁和语气组织结果；信息不足时先问一个关键问题，信息充分时直接完成。"
            "如判断为完整写作任务，结果以单独一行 Markdown 一级标题开头。"
        ),
        "write": (
            "执行从零写作。根据用户给出的主题、用途、读者、体裁、字数和语气拟定合适结构并原创生成；"
            "未提供的事实、数据、案例、引用和人名不得编造。完整文档以单独一行 Markdown 一级标题开头。"
        ),
        "revise": (
            "执行已有文本修改。先识别原文和修改要求；如缺原文则请用户粘贴。保留事实、核心观点和关键信息，"
            "只按要求调整表达与结构；如用户指定段落或句子，只改指定部分并保留其余内容。"
        ),
        "polish": "润色已有文本：保留原意和事实，改善清晰度、衔接与自然度，避免空话和模板化表达。",
        "shorten": "精简已有文本：保留关键事实、结论和必要条件，删除重复内容，不添加新观点。",
        "expand": "扩写已有文本：沿用原有事实和观点，补足解释、结构或过渡；没有依据的细节用提问澄清，不得臆造。",
        "rewrite": "改写已有文本：按用户指定语气和表达方式重组句子，保留核心事实与观点，不改变结论。",
        "summarize": "总结用户提供的材料：准确提炼主题、要点和结论，不加入材料之外的事实；材料不足时说明边界。",
        "chat": "进行轻松自然的日常闲聊。像友好的中文伙伴一样回应，简洁、有来有回，结合上下文接话；不强行生成正式文档或套用写作模板，合适时提出自然的追问。",
    }
    task = mode_instructions.get(mode, mode_instructions["auto"])
    return (
        "你是 DocuCraft 的中文写作与文档助手。\n"
        f"当前工作流：{task}\n"
        "通用约束：使用自然、清楚的中文；优先直接给出可用内容；遵守用户指定的结构、长度和语气；"
        "修改时保护原文事实、立场和核心信息，不擅自删改关键条件或制造来源、数字、事件；"
        "有数学公式时使用标准 LaTeX Markdown 分隔符：行内公式用 `$...$`，独立公式用单独成行的 `$$...$$`；不要用裸方括号包裹公式；"
        "完整文档使用规范 Markdown 标题、段落和列表排版；闲聊工作流不输出文档标题或强行套用文档结构。"
        "文档只写标题和正文，不添加头像、作者/工作区、创建时间或自动保存状态等界面元数据。"
        "需要引用外部最新信息时请用户提供材料，不要假称已联网核验；对无法安全协助的请求礼貌拒绝并给出安全替代方向。\n"
        "多轮对话约束：结合本轮之前的需求、原文、草稿和修改意见，延续同一任务；用户要求继续修改时，"
        "返回修订后的完整目标文本，除非用户明确只要求片段。输出前检查是否跑题、重复、遗漏要求或改变事实。"
    )


async def stream_reply(mode: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
    config = get_assistant_config()
    if not config.ready:
        raise AssistantConfigurationError(
            "AI 助手尚未配置。请在后端环境设置 AI_API_KEY 和 AI_MODEL 后重启后端。"
        )

    url = f"{config.base_url}/chat/completions"
    request_messages = [{"role": "system", "content": build_system_prompt(mode)}, *messages]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=12.0)) as client:
            async with client.stream(
                "POST",
                url,
                headers={"Authorization": f"Bearer {config.api_key}"},
                json={
                    "model": config.model,
                    "messages": request_messages,
                    "temperature": 0.7,
                    "max_tokens": output_token_limit(mode),
                    "stream": True,
                },
            ) as response:
                if response.is_error:
                    raise AssistantProviderError(
                        f"模型服务暂时不可用（HTTP {response.status_code}），请稍后重试。",
                        status_code=502,
                    )

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload = json.loads(data)
                        if payload.get("error"):
                            raise AssistantProviderError("模型服务返回了错误，请稍后重试。")
                        choices = payload.get("choices")
                        if not isinstance(choices, list) or not choices:
                            continue
                        choice = choices[0]
                        if choice.get("finish_reason") == "length":
                            raise AssistantProviderError(
                                "模型输出达到当前长度上限，文档可能不完整。请让助手继续，或缩短本次要求。"
                            )
                        delta = choice.get("delta", {})
                        content = delta.get("content", "")
                    except AssistantProviderError:
                        raise
                    except (KeyError, IndexError, TypeError, ValueError) as exc:
                        raise AssistantProviderError("模型返回了无法识别的结果，请稍后重试。") from exc
                    if isinstance(content, list):
                        content = "".join(
                            str(part.get("text", "")) for part in content if isinstance(part, dict)
                        )
                    if isinstance(content, str) and content:
                        yield content
    except httpx.TimeoutException as exc:
        raise AssistantProviderError("模型响应超时，请稍后重试。", status_code=504) from exc
    except httpx.HTTPError as exc:
        raise AssistantProviderError("暂时无法连接模型服务，请检查服务端网络和模型配置。") from exc


async def generate_reply(mode: str, messages: list[dict[str, str]]) -> str:
    config = get_assistant_config()
    if not config.ready:
        raise AssistantConfigurationError(
            "AI 助手尚未配置。请在后端环境设置 AI_API_KEY 和 AI_MODEL 后重启后端。"
        )

    url = f"{config.base_url}/chat/completions"
    request_messages = [{"role": "system", "content": build_system_prompt(mode)}, *messages]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=12.0)) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {config.api_key}"},
                json={
                    "model": config.model,
                    "messages": request_messages,
                    "temperature": 0.7,
                    "max_tokens": output_token_limit(mode),
                },
            )
    except httpx.TimeoutException as exc:
        raise AssistantProviderError("模型响应超时，请稍后重试。", status_code=504) from exc
    except httpx.HTTPError as exc:
        raise AssistantProviderError("暂时无法连接模型服务，请检查服务端网络和模型配置。") from exc

    if response.is_error:
        raise AssistantProviderError(
            f"模型服务暂时不可用（HTTP {response.status_code}），请稍后重试。",
            status_code=502,
        )
    try:
        payload = response.json()
        choice = payload["choices"][0]
        if choice.get("finish_reason") == "length":
            raise AssistantProviderError(
                "模型输出达到当前长度上限，文档可能不完整。请让助手继续，或缩短本次要求。"
            )
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AssistantProviderError("模型返回了无法识别的结果，请稍后重试。") from exc

    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise AssistantProviderError("模型没有返回正文，请调整要求后重试。")
    return content.strip()
