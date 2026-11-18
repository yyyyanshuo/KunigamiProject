"""Official model catalogue checked 2026-09-23; product task matching is our recommendation.

Source links and selection rationale: MODEL_SETTINGS_ZH.md.
Only general text/vision and supported image-generation endpoints are listed."""

# Gemini entries verified against Google documentation on 2026-11-18.
# Pro/Flash grouping for Gemini 4 is a product classification.
MODEL_CATALOG = {
    "gemini": {
        "gemini-3.8-flash": {"group": "Flash · 均衡", "description": "gemini-3.8-flash", "capabilities": ["text", "vision"]},
        "gemini-3.1-flash-lite": {"group": "Flash-Lite · 速度优先", "description": "gemini-3.1-flash-lite", "capabilities": ["text", "vision"]},
        "gemini-3.1-pro-preview": {"group": "Pro · 能力优先", "description": "gemini-3.1-pro-preview", "capabilities": ["text", "vision"]},
        "gemini-4-carbon": {"group": "Pro · 能力优先", "description": "Gemini 4 Carbon", "capabilities": ["text", "vision"]},
        "gemini-4-argon": {"group": "Pro · 能力优先", "description": "Gemini 4 Argon", "capabilities": ["text", "vision"]},
        "gemini-4-hydrogen": {"group": "Flash · 均衡", "description": "Gemini 4 Hydrogen", "capabilities": ["text", "vision"]},
        "gemini-3-flash-preview": {"group": "Flash · 均衡", "description": "Gemini 3 Flash preview", "capabilities": ["text", "vision"]},
        "gemini-nano-banana-2.1": {"group": "画质优先", "description": "Nano Banana 2.1", "capabilities": ["image_generation"]},
    },
    "openai": {
        "gpt-6-astra": {
            "group": "能力优先",
            "description": "旗舰能力，适合复杂人设和细节识图；推理耗时随请求变化。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "gpt-6-sol": {
            "group": "能力与效率均衡",
            "description": "较强推理，适合聊天和角色表现；速度受推理量影响。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "gpt-6-luna": {
            "group": "速度与成本优先",
            "description": "高吞吐、重效率，适合翻译、通话回复和日常内容。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "gpt-5.4": {
            "group": "其他在役版本",
            "description": "可继续使用的较早版本；不按版本号推断速度。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "gpt-5.4-mini": {
            "group": "轻量备选",
            "description": "较轻量、响应更快，适合记忆整理和日常内容。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "gpt-5.4-nano": {
            "group": "轻量备选",
            "description": "侧重速度和成本，适合简单短文本；复杂语境优先更强模型。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "gpt-5-mini": {
            "group": "其他在役版本",
            "description": "较早的轻量模型，可用于已有配置；复杂任务优先更新型号。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "gpt-image-2.5-sunburst": {
            "group": "画质优先",
            "description": "旗舰生图，优先画面质量与提示词遵循。",
            "capabilities": [
                "image_generation"
            ]
        },
        "gpt-image-2.5-flare": {
            "group": "生图速度优先",
            "description": "日常生图，兼顾画质与速度。",
            "capabilities": [
                "image_generation"
            ]
        }
    },
    "anthropic": {
        "claude-fable-5-1": {
            "group": "能力优先",
            "description": "面向高难度推理，延迟较高；复杂人设的进阶选择。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "claude-opus-5-5": {
            "group": "能力优先",
            "description": "高能力、延迟适中，适合复杂人设和细节识图。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "claude-sonnet-5": {
            "group": "能力与效率均衡",
            "description": "兼顾能力和速度，适合聊天、朋友圈与记忆整理。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "claude-haiku-4-5-20251001": {
            "group": "速度与成本优先",
            "description": "Claude 当前最快档，适合翻译和低延迟回复。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "claude-opus-5": {
            "group": "其他在役版本",
            "description": "可继续使用的较早版本；不按版本号推断速度。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "claude-opus-4-8": {
            "group": "其他在役版本",
            "description": "可继续使用的较早版本；不按版本号推断速度。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "claude-opus-4-7": {
            "group": "其他在役版本",
            "description": "可继续使用的较早版本；不按版本号推断速度。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "claude-opus-4-6": {
            "group": "其他在役版本",
            "description": "可继续使用的较早版本；不按版本号推断速度。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "claude-sonnet-4-6": {
            "group": "其他在役版本",
            "description": "可继续使用的较早版本；不按版本号推断速度。",
            "capabilities": [
                "text",
                "vision"
            ]
        }
    },
    "xai": {
        "grok-4.7": {
            "group": "能力优先",
            "description": "当前旗舰，适合聊天、人设与识图；推理耗时随请求变化。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "grok-4.6": {
            "group": "其他在役版本",
            "description": "支持推理的在役备选；不代表比新版本更快或更便宜。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "grok-4.5": {
            "group": "其他在役版本",
            "description": "支持推理的在役备选；不代表比新版本更快或更便宜。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "grok-4.3": {
            "group": "速度与成本优先",
            "description": "响应快、成本较低，适合翻译、通话回复、记忆与日常内容。",
            "capabilities": [
                "text",
                "vision"
            ]
        },
        "grok-imagine-image-2.0": {
            "group": "画质优先",
            "description": "Grok 专用生图模型。",
            "capabilities": [
                "image_generation"
            ]
        }
    }
}

PROVIDER_ADVICE = {
    "openai": {
        "text": "Astra 偏能力，Sol 偏较强推理，Luna 偏效率；Mini、Nano 可作轻量备选。生图使用 Image 系列。",
        "url": "https://developers.openai.com/api/docs/models/all"
    },
    "anthropic": {
        "text": "Fable、Opus 偏能力，Sonnet 兼顾能力与速度，Haiku 偏速度。Claude 支持识图，暂不支持生图。",
        "url": "https://platform.claude.com/docs/en/models/overview"
    },
    "xai": {
        "text": "Grok 4.7 偏能力，4.3 偏速度和成本；4.6、4.5 是在役备选，不能仅凭版本号比较速度。",
        "url": "https://docs.x.ai/developers/models"
    }
}
