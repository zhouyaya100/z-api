"""Z API - Token 计数 (tiktoken)"""
import logging

logger = logging.getLogger("z-api")

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        """使用 tiktoken 精确计数"""
        if not text:
            return 0
        return len(_ENC.encode(text))
except Exception:
    def count_tokens(text: str) -> int:
        """Fallback 估算"""
        if not text:
            return 0
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other_chars = len(text) - chinese_chars
        return max(1, int(chinese_chars / 1.5 + other_chars / 4))


def count_prompt_tokens(body_json: dict) -> int:
    """从请求体计算 prompt tokens"""
    total = 0
    messages = body_json.get("messages", [])
    for msg in messages:
        total += 4
        content = msg.get("content", "")
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if text:
                        total += count_tokens(text)
        name = msg.get("name", "")
        if name:
            total += count_tokens(name) + 1
    total += 3
    return total
