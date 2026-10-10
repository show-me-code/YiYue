"""内置翻译风格预设（润色风格），用户在新建任务时选择。

预设文本作为"自定义翻译规范"注入两条引擎路径的系统提示词：
与配置档里的 custom_system_prompt 合并（预设在前，用户规范在后，
用户规范优先级更高——后写的规则通常被模型更看重）。
"""

# 每个预设末尾追加的硬约束：风格不得破坏结构占位符
_PLACEHOLDER_RULE = "占位符（如 [[M12]]、[[C3]]）与 LaTeX 结构原样保留的优先级高于一切风格要求。"

# key → (下拉标签, 注入提示词)
POLISH_PRESETS: dict[str, tuple[str, str]] = {
    "default": (
        "默认（学术规范）",
        "翻译风格要求：地道的中文学术写作，杜绝逐字硬译的欧式中文。英文长句按中文"
        "表达习惯拆分为通顺短句，主谓宾清晰；主动语态优先，慎用「被」字句；避免"
        "「的」字堆叠和定语过长；连接词符合中文逻辑（因此/然而/此外/具体而言）。"
        "专业术语准确且全文一致；人名、机构名保留原文。专业含义必须与原文完全一致，"
        "不得增删信息。" + _PLACEHOLDER_RULE,
    ),
    "faithful": (
        "忠实直译",
        "翻译风格要求：忠实直译。紧贴原文句序与措辞，保留原文的强调、限定词与"
        "逻辑连接词，不意译、不增删；术语严格遵循术语表。" + _PLACEHOLDER_RULE,
    ),
    "polished": (
        "流畅润色",
        "翻译风格要求：在保证专业准确的前提下做地道的中文化润色，消除翻译腔——"
        "按中文表达习惯重组语序、把英文长句拆成短句、化被动为主动、使用中文"
        "惯用搭配与衔接词。不得改变专业含义，术语严格遵循术语表。" + _PLACEHOLDER_RULE,
    ),
    "plain": (
        "通俗易懂",
        "翻译风格要求：通俗易懂。在不损失专业准确性的前提下，把复杂概念用更直白的"
        "中文讲清楚：长句拆短、抽象表述配简单解释、关键术语可保留英文原词于括号内。"
        + _PLACEHOLDER_RULE,
    ),
}


def preset_label(key: str) -> str:
    return POLISH_PRESETS.get(key, POLISH_PRESETS["default"])[0]


def effective_custom_prompt(custom: str | None, polish: str | None) -> str | None:
    """配置档自定义规范 + 风格预设 → 合并后的注入文本（都为空则 None）。"""
    parts = []
    preset = POLISH_PRESETS.get((polish or "default"), POLISH_PRESETS["default"])[1]
    if preset:
        parts.append(preset)
    if custom and custom.strip():
        parts.append(custom.strip())
    return "\n\n".join(parts) if parts else None
