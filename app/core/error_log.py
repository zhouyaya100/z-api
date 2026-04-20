"""Z API - 错误日志文件记录

职责：将未捕获异常和运行时错误写入 logs/error.log
支持：最大条数限制，超出自动清理最旧的记录
"""
import os
import logging
from datetime import datetime
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_LOG_FILE = _LOG_DIR / "error.log"

# 确保日志目录存在
_LOG_DIR.mkdir(parents=True, exist_ok=True)


def _count_lines(filepath: Path) -> int:
    """快速统计文件行数"""
    if not filepath.exists():
        return 0
    count = 0
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for _ in f:
                count += 1
    except Exception:
        pass
    return count


def _trim_log(filepath: Path, max_entries: int):
    """保留最新的 max_entries 条日志，删除最旧的"""
    if not filepath.exists():
        return
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        if len(lines) <= max_entries:
            return
        # 保留最后 max_entries 行
        kept = lines[-max_entries:]
        with open(filepath, "w", encoding="utf-8") as f:
            f.writelines(kept)
    except Exception:
        pass


class MaxEntriesHandler(logging.FileHandler):
    """带最大条数限制的文件日志 Handler"""

    def __init__(self, filepath: Path, max_entries: int = 10000):
        super().__init__(str(filepath), encoding="utf-8")
        self._max_entries = max(100, max_entries)
        self._counter = 0
        self._check_interval = 100  # 每写100条检查一次

    def emit(self, record):
        super().emit(record)
        self._counter += 1
        if self._counter >= self._check_interval:
            self._counter = 0
            total = _count_lines(Path(self.baseFilename))
            if total > self._max_entries:
                _trim_log(Path(self.baseFilename), self._max_entries)

    def update_max_entries(self, max_entries: int):
        self._max_entries = max(100, max_entries)


# ---- 全局错误日志 Logger ----
error_logger = logging.getLogger("z-api-error")
error_logger.setLevel(logging.ERROR)
error_logger.propagate = False  # 关键：不传播给 root logger，避免重复写入

_handler: MaxEntriesHandler | None = None
_initialized = False


def setup_error_log(max_entries: int = 10000):
    """初始化错误日志文件 Handler（只初始化一次）"""
    global _handler, _initialized
    if _initialized:
        # 已初始化，只更新 max_entries
        if _handler:
            _handler.update_max_entries(max_entries)
        return

    _handler = MaxEntriesHandler(_LOG_FILE, max_entries)
    _handler.setFormatter(logging.Formatter(
        '[%(asctime)s] %(levelname)s in %(module)s:%(lineno)d\n'
        '  Message: %(message)s'
    ))
    error_logger.addHandler(_handler)
    _initialized = True


def update_max_entries(max_entries: int):
    """热更新最大条数"""
    if _handler:
        _handler.update_max_entries(max_entries)


def get_error_log_content(offset: int = 0, limit: int = 100) -> dict:
    """读取错误日志内容（分页）"""
    if not _LOG_FILE.exists():
        return {"total": 0, "lines": [], "file": str(_LOG_FILE)}

    try:
        with open(_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
        total = len(all_lines)
        selected = all_lines[offset:offset + limit]
        return {
            "total": total,
            "lines": [line.rstrip("\n") for line in selected],
            "file": str(_LOG_FILE),
        }
    except Exception as e:
        return {"total": 0, "lines": [f"读取错误日志失败: {e}"], "file": str(_LOG_FILE)}


def clear_error_log() -> bool:
    """清空错误日志"""
    try:
        if _LOG_FILE.exists():
            with open(_LOG_FILE, "w", encoding="utf-8") as f:
                f.write("")
        return True
    except Exception:
        return False
