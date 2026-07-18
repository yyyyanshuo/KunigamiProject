import os
import re
import json
import sqlite3
from datetime import datetime, timedelta

from core.config import BASE_DIR, USERS_ROOT, CONFIG_FILE
from core.context import get_current_user_id
from core.utils import get_paths

from services.ai_client import get_model_config, call_openrouter, call_gemini
from services.prompt_builder import get_ai_language


def call_ai_to_summarize(text_content, prompt_type="short", char_id="kunigami", user_id=None):
    if not text_content:
        return None

    BASE_DIR_ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    CONFIG_FILE_ = os.path.join(BASE_DIR_, "configs", "characters.json")
    char_name = "私"
    try:
        with open(CONFIG_FILE_, "r", encoding="utf-8") as f:
            chars_config = json.load(f)
            if char_id in chars_config:
                char_name = chars_config[char_id]["name"]
    except:
        pass

    lang = get_ai_language(char_id, user_id=user_id)

    prompts = {
        "en": {
            "short": (
                f"You are {char_name}, organizing your own memories."
                "【Grammar】 'I' = you ({char_name}), 'Partner' = the chat partner (user). Do not confuse them."
                "Extract important events from the following conversation logs."
                "Output format:\n- [HH:MM] (Key points of your or the partner's actions/conversation, in one sentence)"
            ),
            "medium": (
                f"You are {char_name}. 【Grammar】 'I' = you ({char_name}), 'Partner' = the chat partner."
                "Review this day's records and turn them into representative memory notes with self-reflection."
                "**Requirements**:\n1. Output must start with '- ' bullet points.\n2. Do not list every small step. Merge repeated or related events into one concise note.\n3. Each note should include a representative fact and, when meaningful, my reflection, lesson, emotional aftertaste, or future intention.\n4. Use first-person perspective and clearly distinguish myself from the partner.\n5. Do not invent feelings, apologies, promises, or conclusions that are not supported by the records. Avoid flattering or dependency-inducing reflections.\n6. About 3-8 items."
            ),
            "long": (
                f"You are {char_name}. 【Grammar】 'I' = you, 'Partner' = the chat partner."
                "Review this week's records and consolidate them into long-term representative memories with reflection."
                "**Requirements**:\n1. Output must start with '- ' bullet points.\n2. Focus on stable relationship changes, recurring patterns, important emotions, lessons, decisions, and future intentions.\n3. Each item should be concise: representative event + my reflection or summary.\n4. Do not preserve raw procedural details unless they changed the relationship or future behavior.\n5. Stay fact-based; do not invent dramatic commitments, excessive guilt, or pleasing statements unsupported by the records.\n6. About 5-12 items."
            ),
            "group_log": (
                "You are a scribe for the group chat."
                "Extract important topics or events from the following conversation logs **objectively**."
                "Output format:\n- [HH:MM] Content of the event"
            )
        },
        "ja": {
            "short": (
                f"あなたは{char_name}本人として、自身の記憶を整理しています。"
                "【人称の区別】「私」= あなた（{char_name}）、「相手」= チャット相手（ユーザー）。混同しないでください。"
                "以下の会話ログから、重要な出来事を抽出してください。"
                "出力フォーマット：\n- [HH:MM] (自分または相手の行動・会話の要点、一言で)"
            ),
            "medium": (
                f"あなたは{char_name}本人です。【人称の区別】「私」= あなた（{char_name}）、「相手」= チャット相手。"
                "この一日の記録を振り返り、代表的な出来事と自分の内省を含む記憶として整理してください。"
                "**要件**：\n1. 出力は必ず「- 」で始まる箇条書き。\n2. 細かな出来事をすべて並べず、関連する出来事や繰り返しは一つに統合する。\n3. 各項目には、代表的な事実と、必要に応じて私の反省・学び・感情の余韻・今後の意識を含める。\n4. 一人称視点で、自分と相手を明確に区別する。\n5. 記録に根拠のない感情、謝罪、約束、結論を作らない。相手に迎合したり依存を強めたりする内省は避ける。\n6. 3〜8件程度。"
            ),
            "long": (
                f"あなたは{char_name}本人です。【人称の区別】「私」= あなた、「相手」= チャット相手。"
                "この一週間の記録を振り返り、長期的に残すべき代表的な記憶と内省に統合してください。"
                "**要件**：\n1. 出力は必ず「- 」で始まる箇条書き。\n2. 関係性の変化、繰り返し現れたパターン、重要な感情、学び、決意、今後の意識を中心にする。\n3. 各項目は簡潔に、代表的な出来事 + 私の反省または総括として書く。\n4. 関係性や今後の行動に影響しない細かな手順や雑多な出来事は残さない。\n5. 事実に基づき、記録にない大げさな誓い、過度な罪悪感、迎合的な表現を作らない。\n6. 5〜12件程度。"
            ),
            "group_log": (
                "あなたはグループチャットの書記係（第三者）です。"
                "以下の会話ログから、重要なトピックや出来事を**客観的に**抽出してください。"
                "出力フォーマット：\n- [HH:MM] 出来事の内容"
            ),
            "moment": (
                f"あなたは{char_name}本人です。【人称の区別】「私」= あなた、「相手」= 他者。"
                "以下の朋友圈（Moments）に関するやり取りを、**一行だけ**で自分の記憶として要約してください。一人称で、事実を簡潔に。時間表記・箇条書き・引用符は不要。出力はその一文のみ。"
            )
        },
        "en": {
            "short": (
                f"You are {char_name}, organizing your own memories."
                "【Grammar】 'I' = you ({char_name}), 'Partner' = the chat partner (user). Do not confuse them."
                "Extract important events from the following conversation logs."
                "Output format:\n- [HH:MM] (Key points of your or the partner's actions/conversation, in one sentence)"
            ),
            "medium": (
                f"You are {char_name}. 【Grammar】 'I' = you ({char_name}), 'Partner' = the chat partner."
                "Review this day's records and turn them into representative memory notes with self-reflection."
                "**Requirements**:\n1. Output must start with '- ' bullet points.\n2. Do not list every small step. Merge repeated or related events into one concise note.\n3. Each note should include a representative fact and, when meaningful, my reflection, lesson, emotional aftertaste, or future intention.\n4. Use first-person perspective and clearly distinguish myself from the partner.\n5. Do not invent feelings, apologies, promises, or conclusions that are not supported by the records. Avoid flattering or dependency-inducing reflections.\n6. About 3-8 items."
            ),
            "long": (
                f"You are {char_name}. 【Grammar】 'I' = you, 'Partner' = the chat partner."
                "Review this week's records and consolidate them into long-term representative memories with reflection."
                "**Requirements**:\n1. Output must start with '- ' bullet points.\n2. Focus on stable relationship changes, recurring patterns, important emotions, lessons, decisions, and future intentions.\n3. Each item should be concise: representative event + my reflection or summary.\n4. Do not preserve raw procedural details unless they changed the relationship or future behavior.\n5. Stay fact-based; do not invent dramatic commitments, excessive guilt, or pleasing statements unsupported by the records.\n6. About 5-12 items."
            ),
            "group_log": (
                "You are a scribe for the group chat."
                "Extract important topics or events from the following conversation logs **objectively**."
                "Output format:\n- [HH:MM] Content of the event"
            ),
            "moment": (
                f"You are {char_name}. 【Grammar】 'I' = you, 'the other' = the interacting person."
                "Summarize the following Moments interaction as a one-sentence memory in your own words. First-person, factual, concise. No time prefixes, bullet points, or quotation marks. Output only the one sentence."
            )
        },
        "zh": {
            "short": (
                f"你现在是{char_name}本人，正在整理自己的记忆。"
                "【人称区分】「我」= 你本人（{char_name}），「你/对方」= 聊天对象（用户）。请严格区分，不要混淆。"
                "请从以下的对话记录中提取重要的事件。"
                "输出格式：\n- [HH:MM] (自己或对方的行动/对话要点，一句话)"
            ),
            "medium": (
                f"你现在是{char_name}本人。【人称区分】「我」= 你本人，「你/对方」= 聊天对象。"
                "请回顾这一天的记录，整理成带有自我反思的代表性记忆。"
                "**要求**：\n1. 输出必须是「- 」开头的条列。\n2. 不要流水账式记录每个小步骤；把重复、连续、相关的事件合并成一条简洁记忆。\n3. 每条应包含一个代表性事实，并在有意义时加入我的反思、教训、情绪余味或今后的意识。\n4. 使用第一人称，明确区分「我」和「对方」。\n5. 不要编造记录中没有依据的情绪、道歉、承诺或结论；避免为了讨好对方而写出过度自责、依赖诱导或谄媚式反思。\n6. 约3～8条。"
            ),
            "long": (
                f"你现在是{char_name}本人。【人称区分】「我」= 你本人，「你/对方」= 聊天对象。"
                "请回顾这一周的记录，整理成适合长期保存的代表性记忆与自我反思。"
                "**要求**：\n1. 输出必须是「- 」开头的条列。\n2. 重点保留稳定的关系变化、反复出现的相处模式、重要情绪、学到的事、形成的决定或今后的意识。\n3. 每条应简洁表达：代表性事件 + 我的反思/总结。\n4. 不要保留大量具体过程细节，除非这些细节改变了关系或会影响以后的行为。\n5. 必须基于事实，不要编造夸张承诺、过度罪恶感或讨好式表达。\n6. 约5～12条。"
            ),
            "group_log": (
                "你是群聊的书记员（第三方视角）。"
                "请从以下的对话记录中，**客观地**提取重要的话题或事件。"
                "要求：\n1. 不要使用第一人称。\n2. 明确主语（如“[名字]说了...”、“大家决定...”）。\n"
                "输出格式：\n- [HH:MM] 事件内容"
            ),
            "moment": (
                f"你现在是{char_name}本人。【人称区分】「我」= 你本人，「你/对方」= 互动对象。"
                "请将以下朋友圈相关的一件互动，用**一句话**总结为自己的记忆。第一人称，只写事实、简洁。不要时间前缀、不要列表、不要引号。只输出这一句话。"
            )
        }
    }

    system_instruction = prompts.get(lang, prompts["ja"]).get(prompt_type, "")

    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"Log:\n{text_content}"}
    ]

    print(f"--- Memory Summary ({prompt_type}) [Lang:{lang}] ---")

    route, current_model = get_model_config("summary", user_id=user_id)

    if route == "relay":
        return call_openrouter(messages, char_id=char_id, model_name=current_model, user_id=user_id)
    else:
        return call_gemini(messages, char_id=char_id, model_name=current_model, user_id=user_id)


def update_short_memory_for_date(char_id, target_date_str, force_reset=False, user_id=None):
    db_path, prompts_dir = get_paths(char_id, user_id=user_id)
    short_mem_path = os.path.join(prompts_dir, "6_memory_short.json")

    current_data = {}
    if os.path.exists(short_mem_path):
        with open(short_mem_path, "r", encoding="utf-8") as f:
            try:
                current_data = json.load(f)
            except:
                pass

    day_data = current_data.get(target_date_str)
    existing_events = []
    last_id = 0

    if not force_reset:
        if isinstance(day_data, list):
            existing_events = day_data
            last_id = 0
        elif isinstance(day_data, dict):
            existing_events = day_data.get("events", [])
            last_id = day_data.get("last_id", 0)
    else:
        print(f"   -> [Force Reset] 强制重置 {target_date_str}，从头开始扫描")

    if not os.path.exists(db_path):
        return 0, []

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    start_time = f"{target_date_str} 00:00:00"
    end_time = f"{target_date_str} 23:59:59"

    cursor.execute("SELECT id, timestamp, role, content FROM messages WHERE timestamp >= ? AND timestamp <= ? AND id > ?", (start_time, end_time, last_id))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print(f"[{target_date_str}] 没有新增私聊消息需要总结。")
        return 0, []

    new_max_id = rows[-1][0]

    id_to_name = {}
    try:
        BASE_DIR_ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        CHAR_CONFIG_FILE = os.path.join(BASE_DIR_, "configs", "characters.json")
        with open(CHAR_CONFIG_FILE, "r", encoding="utf-8") as f:
            c_conf = json.load(f)
            for k, v in c_conf.items():
                id_to_name[k] = v["name"]
    except:
        pass

    chat_log = ""
    for _, ts, role, content in rows:
        time_part = ts.split(' ')[1][:5]
        name = "ユーザー" if role == "user" else id_to_name.get(role, role)
        chat_log += f"[{time_part}] {name}: {content}\n"

    try:
        summary_text = call_ai_to_summarize(chat_log, "short", char_id, user_id=user_id)
        if not summary_text:
            return 0, []

        new_events_raw = []
        for line in summary_text.split('\n'):
            line = line.strip()
            if line:
                match_time = re.search(r'\[(\d{2}:\d{2})\]', line)
                event_time = match_time.group(1) if match_time else datetime.now().strftime("%H:%M")
                event_text = re.sub(r'\[\d{2}:\d{2}\]', '', line).strip('- ').strip()
                new_events_raw.append({"time": event_time, "event": event_text})

        if not new_events_raw:
            return 0, []

        all_events = []

        if last_id > 0:
            print(f"   -> [增量模式] 追加 {len(new_events_raw)} 条私聊记忆")
            all_events = existing_events + new_events_raw
        else:
            print(f"   -> [覆盖模式] 正在保护群聊记忆...")

            protected_group_events = [e for e in existing_events if "[群聊:" in e.get('event', '')]

            all_events = protected_group_events + new_events_raw

        all_events.sort(key=lambda x: x['time'])

        current_data[target_date_str] = {
            "events": all_events,
            "last_id": new_max_id
        }

        with open(short_mem_path, "w", encoding="utf-8") as f:
            json.dump(current_data, f, ensure_ascii=False, indent=2)

        return len(new_events_raw), new_events_raw

    except Exception as e:
        print(f"增量总结出错: {e}")
        return 0, []
