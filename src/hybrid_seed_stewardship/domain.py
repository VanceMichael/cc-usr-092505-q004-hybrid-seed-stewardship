"""读取并检查仓库中的领域资料。"""

import json
from pathlib import Path

def load_domain(path: Path) -> dict:
    """读取字段完整且版本有效的业务资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {"domain", "version", "sample_id", "actors", "facts", "constraints"}
    if set(value) != required:
        raise ValueError("领域资料字段不完整")
    if value["version"] < 1 or len(value["actors"]) < 2 or len(value["facts"]) < 2 or len(value["constraints"]) < 3:
        raise ValueError("领域资料内容不足")
    return value
