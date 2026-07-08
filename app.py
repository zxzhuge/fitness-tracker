"""Fitness Tracker - Flask Application"""

import json
import os
import calendar
from datetime import datetime, date, timedelta
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect, url_for,
    send_file, Response,
)
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PROMPTS_DIR = BASE_DIR / "prompts"
CHAT_DIR = DATA_DIR / "chats"

DATA_DIR.mkdir(exist_ok=True)
PROMPTS_DIR.mkdir(exist_ok=True)
CHAT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)


# ── Data helpers ──────────────────────────────────────────────

def load_json(filename: str) -> list:
    filepath = DATA_DIR / filename
    if filepath.exists():
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def save_json(filename: str, data: list) -> None:
    filepath = DATA_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def next_id(data: list) -> int:
    return max((item["id"] for item in data), default=0) + 1


def today_str() -> str:
    return date.today().isoformat()


def now_iso_str() -> str:
    return datetime.now().isoformat(timespec="seconds")


def current_datetime_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── Chat history ────────────────────────────────────────────

CHAT_FILE = "history.json"


def infer_legacy_timestamps(messages: list, filepath: Path) -> tuple[list, bool]:
    if not messages:
        return [], False

    try:
        base_time = datetime.fromtimestamp(filepath.stat().st_mtime)
    except OSError:
        base_time = datetime.now()

    base_time = base_time.replace(second=0, microsecond=0)
    inferred_start = base_time - timedelta(minutes=max(len(messages) - 1, 0))

    migrated = []
    changed = False
    for index, message in enumerate(messages):
        item = dict(message)
        timestamp = normalize_timestamp(item.get("timestamp"))
        if timestamp:
            item["timestamp"] = timestamp
            if item.get("timestamp_source") == "estimated":
                item["timestamp_source"] = "estimated"
            else:
                item["timestamp_source"] = "recorded"
        else:
            item["timestamp"] = (inferred_start + timedelta(minutes=index)).isoformat(timespec="seconds")
            item["timestamp_source"] = "estimated"
            changed = True
        migrated.append(item)

    return migrated, changed


def load_chat_history() -> list:
    filepath = CHAT_DIR / CHAT_FILE
    if filepath.exists():
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                history = normalize_chat_messages(data)
                history, changed = infer_legacy_timestamps(history, filepath)
                if changed:
                    save_chat_history(history)
                return history
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def save_chat_history(messages: list) -> None:
    filepath = CHAT_DIR / CHAT_FILE
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)


def normalize_timestamp(timestamp: str | None) -> str | None:
    if not timestamp or not isinstance(timestamp, str):
        return None

    try:
        return datetime.fromisoformat(timestamp).isoformat(timespec="seconds")
    except ValueError:
        return None


def normalize_chat_messages(messages: list) -> list:
    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            continue

        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue

        item = {
            "role": role,
            "content": content,
        }

        timestamp = normalize_timestamp(message.get("timestamp"))
        if timestamp:
            item["timestamp"] = timestamp
            item["timestamp_source"] = "recorded"

        if message.get("timestamp_source") == "estimated":
            item["timestamp_source"] = "estimated"

        normalized.append(item)

    return normalized


def format_chat_timestamp(timestamp: str | None) -> str:
    normalized = normalize_timestamp(timestamp)
    if not normalized:
        return "时间未知"
    return datetime.fromisoformat(normalized).strftime("%Y-%m-%d %H:%M:%S")


def build_chat_history_context(messages: list, max_messages: int = 10) -> str:
    normalized = normalize_chat_messages(messages)
    if not normalized:
        return ""

    lines = []
    for message in normalized[-max_messages:]:
        speaker = "用户" if message["role"] == "user" else "小陈"
        sent_at = format_chat_timestamp(message.get("timestamp"))
        if message.get("timestamp_source") == "estimated":
            sent_at = f"约 {sent_at}"
        lines.append(f"- [{sent_at}] {speaker}: {message['content']}")

    return "\n".join(lines)


def calculate_streak(workouts: list) -> int:
    """Calculate current consecutive check-in days."""
    if not workouts:
        return 0
    dates_set = {w["date"] for w in workouts}
    today = date.today()
    streak = 0
    check_date = today

    if check_date.isoformat() not in dates_set:
        check_date -= timedelta(days=1)
        if check_date.isoformat() not in dates_set:
            return 0

    while check_date.isoformat() in dates_set:
        streak += 1
        check_date -= timedelta(days=1)

    return streak


# ── LLM helpers ───────────────────────────────────────────────

def load_prompt(name: str) -> str:
    """Load a prompt file from the prompts/ folder."""
    filepath = PROMPTS_DIR / name
    if filepath.exists():
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def get_llm_client() -> OpenAI:
    api_key = os.getenv("LLM_API_KEY", "")
    api_url = os.getenv("LLM_API_URL", "https://api.openai.com/v1")
    return OpenAI(api_key=api_key, base_url=api_url)


def build_system_prompt(messages: list | None = None) -> str:
    """Build the system prompt with user fitness context injected."""
    system_prompt = load_prompt("system_prompt.md")

    workouts = load_json("workouts.json")
    weights = load_json("weight.json")
    # Filter valid entries
    workouts = [w for w in workouts if isinstance(w, dict) and "date" in w]
    weights = [w for w in weights if isinstance(w, dict) and "weight" in w and "date" in w]
    current_weight = weights[-1]["weight"] if weights else "未记录"
    total_checkins = len(workouts)
    streak = calculate_streak(workouts)

    chat_template = load_prompt("chat_template.md")
    if chat_template:
        context = chat_template.format(
            total_workouts=total_checkins,
            last_workout_date=workouts[-1]["date"] if workouts else "暂无",
            current_weight=current_weight,
            weight_change="",
            user_message="",
        )
    else:
        context = ""

    parts = [system_prompt]
    if context:
        parts.append(f"\n\n用户的健身数据：\n{context}")
    history_context = build_chat_history_context(messages or [])
    if history_context:
        parts.append(f"\n最近聊天记录（含发送时间）：\n{history_context}")
    parts.append(f"\n当前时间: {current_datetime_str()}")
    parts.append(f"今日日期: {today_str()}")
    parts.append(f"总打卡次数: {total_checkins}")
    parts.append(f"当前连续打卡: {streak}天")

    return "\n".join(parts)


def stream_llm(messages: list):
    """Generator that yields LLM response chunks as SSE events."""
    normalized_messages = normalize_chat_messages(messages)
    system_prompt = build_system_prompt(normalized_messages)

    if not normalized_messages:
        yield "data: 请发送一条消息\n\n"
        yield "data: [DONE]\n\n"
        return

    try:
        client = get_llm_client()
        model = os.getenv("LLM_MODEL", "gpt-4o-mini")
        stream = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                *[
                    {"role": message["role"], "content": message["content"]}
                    for message in normalized_messages
                ],
            ],
            temperature=0.8,
            max_tokens=500,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield f"data: {chunk.choices[0].delta.content}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        yield f"data: 获取回复失败: {e}\n\n"
        yield "data: [DONE]\n\n"


# ── Routes ────────────────────────────────────────────────────

@app.route("/")
def index():
    """Dashboard: stats, check-in, calendar, weight chart, chat."""
    workouts = load_json("workouts.json")
    weights = load_json("weight.json")
    today = today_str()

    # Filter valid entries
    workouts = [w for w in workouts if isinstance(w, dict) and "date" in w]
    weights = [w for w in weights if isinstance(w, dict) and "weight" in w and "date" in w]

    checked_in_today = any(w["date"] == today for w in workouts)

    # Stats
    total_checkins = len(workouts)
    current_weight = weights[-1]["weight"] if weights else None
    weight_change = None
    if len(weights) >= 2:
        weight_change = round(weights[-1]["weight"] - weights[0]["weight"], 1)
    streak = calculate_streak(workouts)

    # Calendar: current month grid
    now = datetime.now()
    year, month = now.year, now.month
    cal = calendar.Calendar(firstweekday=0)  # Monday start
    month_days = cal.monthdayscalendar(year, month)

    workout_dates = {w["date"] for w in workouts}
    calendar_weeks = []
    for week in month_days:
        row = []
        for day in week:
            if day == 0:
                row.append({"day": 0, "checked": False, "is_today": False})
            else:
                d = f"{year}-{month:02d}-{day:02d}"
                row.append({
                    "day": day,
                    "checked": d in workout_dates,
                    "is_today": d == today,
                })
        calendar_weeks.append(row)

    chart_weights = weights[-30:] if len(weights) > 30 else weights

    return render_template(
        "index.html",
        checked_in_today=checked_in_today,
        total_checkins=total_checkins,
        current_weight=current_weight,
        weight_change=weight_change,
        streak=streak,
        calendar_weeks=calendar_weeks,
        year=year,
        month=month,
        chart_weights=chart_weights,
        today=today,
    )


# ── Check-in routes ──────────────────────────────────────────

@app.route("/workouts")
def workouts_page():
    """Check-in history page."""
    data = load_json("workouts.json")
    data_sorted = sorted(data, key=lambda x: x["date"], reverse=True)
    return render_template("workouts.html", workouts=data_sorted, today_str=today_str())


@app.route("/workouts/add", methods=["POST"])
def add_checkin():
    """Add a daily check-in."""
    data = load_json("workouts.json")

    workout_date = request.form.get("date", today_str())
    note = request.form.get("note", "").strip()

    # Don't duplicate check-in for same date
    if any(w["date"] == workout_date for w in data):
        return redirect(url_for("workouts_page"))

    data.append({
        "id": next_id(data),
        "date": workout_date,
        "note": note,
    })

    save_json("workouts.json", data)
    return redirect(url_for("workouts_page"))


@app.route("/workouts/<int:workout_id>/delete", methods=["POST"])
def delete_checkin(workout_id: int):
    """Delete a check-in entry."""
    data = load_json("workouts.json")
    data = [w for w in data if w["id"] != workout_id]
    save_json("workouts.json", data)
    return redirect(url_for("workouts_page"))


# ── Weight routes ─────────────────────────────────────────────

@app.route("/weight")
def weight_page():
    """List all weight records."""
    data = load_json("weight.json")
    data_sorted = sorted(data, key=lambda x: x["date"], reverse=True)
    return render_template("weight.html", weights=data_sorted)


@app.route("/weight/add", methods=["POST"])
def add_weight():
    """Add a new weight entry."""
    data = load_json("weight.json")

    weight_date = request.form.get("date", today_str())
    weight_val = request.form.get("weight", "")

    if weight_val:
        for w in data:
            if w["date"] == weight_date:
                w["weight"] = float(weight_val)
                save_json("weight.json", data)
                return redirect(url_for("weight_page"))

        data.append({
            "id": next_id(data),
            "date": weight_date,
            "weight": float(weight_val),
        })
        save_json("weight.json", data)

    return redirect(url_for("weight_page"))


@app.route("/weight/<int:weight_id>/delete", methods=["POST"])
def delete_weight(weight_id: int):
    """Delete a weight entry."""
    data = load_json("weight.json")
    data = [w for w in data if w["id"] != weight_id]
    save_json("weight.json", data)
    return redirect(url_for("weight_page"))


# ── Chat API (SSE streaming) ─────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def api_chat():
    """SSE streaming endpoint: accepts conversation history as JSON."""
    data = request.get_json() or {}
    messages = normalize_chat_messages(data.get("messages", []))

    # Save full conversation to disk
    save_chat_history(messages)

    # Only send last 10 messages to LLM for context
    context = messages[-10:] if len(messages) > 10 else messages

    return Response(
        stream_llm(context),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/chat/history")
def api_chat_history():
    """Return saved chat history."""
    history = load_chat_history()
    return {"messages": history}


@app.route("/api/chat/save", methods=["POST"])
def api_chat_save():
    """Save the full chat history (called after assistant response)."""
    data = request.get_json() or {}
    messages = normalize_chat_messages(data.get("messages", []))
    save_chat_history(messages)
    return {"ok": True}


# ── AI Summary API (single model, JSON output, SSE streaming) ─────

def get_summary_client() -> OpenAI:
    """Get LLM client for the summary model (model 2)."""
    api_key = os.getenv("LLM_API_KEY_2", os.getenv("LLM_API_KEY", ""))
    api_url = os.getenv("LLM_API_URL_2", os.getenv("LLM_API_URL", "https://api.openai.com/v1"))
    return OpenAI(api_key=api_key, base_url=api_url)


def build_summary_prompt() -> str:
    """Build prompt that asks the model to output JSON."""
    workouts = load_json("workouts.json")
    weights = load_json("weight.json")
    streak = calculate_streak(workouts)

    workouts = [w for w in workouts if isinstance(w, dict) and "date" in w]
    weights = [w for w in weights if isinstance(w, dict) and "weight" in w and "date" in w]

    # Time span
    if workouts:
        sorted_w = sorted(workouts, key=lambda x: x["date"])
        first_date = sorted_w[0]["date"]
        try:
            span_days = (date.today() - date.fromisoformat(first_date)).days + 1
            if span_days >= 30:
                span_str = f"{span_days}天（约{span_days // 30}个月）"
            elif span_days >= 7:
                span_str = f"{span_days}天（约{span_days // 7}周）"
            else:
                span_str = f"{span_days}天"
        except ValueError:
            span_str = "未知"
    else:
        span_str = "暂无数据"

    lines = [
        "你是一位专业的健身数据分析师。请根据以下健身数据进行分析，并以JSON格式输出。",
        "",
        "【输出要求】",
        "只输出一个JSON对象，不要输出任何其他文字、解释或Markdown代码块。",
        "JSON的key固定为以下5个（没有数据的key直接省略，不要输出空字符串）：",
        '- "时间跨度": 从第一次打卡到今天的天数/周数/月数',
        '- "打卡总成绩": 总打卡天数、连续打卡天数等',
        '- "体重总成绩": 初始体重、当前体重、最高最低、净变化',
        '- "核心里程碑": 2-3个值得庆祝的成就，用逗号分隔',
        '- "教练建议": 2-3条科学建议，用逗号分隔',
        "",
        "示例（仅展示格式）：",
        '{"时间跨度":"坚持健身共60天，约8周","打卡总成绩":"累计打卡45天，最长连续12天","体重总成绩":"从75kg减到70kg，最低69.5kg，净减5kg","核心里程碑":"累计打卡突破30天，体重首次跌破70kg","教练建议":"保持当前节奏，建议每周增加一次力量训练，注意蛋白质摄入"}',
        "",
        "禁止输出 #、* 等Markdown符号。内容简洁，每项不超过30字。语气专业温暖。",
        "",
        f"今日日期: {today_str()}",
        f"时间跨度: {span_str}",
        f"总打卡次数: {len(workouts)}",
        f"当前连续打卡: {streak}天",
        "",
    ]

    if weights:
        sorted_weights = sorted(weights, key=lambda x: x["date"])
        w_values = [w["weight"] for w in sorted_weights]
        lines.append("体重数据:")
        for w in sorted_weights[-10:]:
            lines.append(f"  {w['date']}: {w['weight']}kg")
        if len(w_values) >= 2:
            change = round(w_values[-1] - w_values[0], 1)
            lines.append(f"  初始: {w_values[0]}kg, 当前: {w_values[-1]}kg, "
                         f"最高: {max(w_values)}kg, 最低: {min(w_values)}kg, "
                         f"净变化: {'+' if change > 0 else ''}{change}kg")
        lines.append("")

    if workouts:
        lines.append("最近打卡记录:")
        for w in sorted(workouts, key=lambda x: x["date"], reverse=True)[:10]:
            note = f" ({w['note']})" if w.get("note") else ""
            lines.append(f"  {w['date']} ✅{note}")

    return "\n".join(lines)


def stream_summary():
    """Single model pipeline: generate JSON summary (streamed)."""
    try:
        client = get_summary_client()
        model = os.getenv("LLM_MODEL_2", os.getenv("LLM_MODEL", "gpt-4o-mini"))
        prompt = build_summary_prompt()

        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=500,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield f"data: {chunk.choices[0].delta.content}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        yield f"data: 获取总结失败: {e}\n\n"
        yield "data: [DONE]\n\n"


@app.route("/api/summary")
def api_summary():
    """SSE streaming endpoint: AI fitness summary (JSON output)."""
    return Response(
        stream_summary(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Export ────────────────────────────────────────────────────

@app.route("/export")
def export_markdown():
    """Export all data as a Markdown file."""
    workouts = load_json("workouts.json")
    weights = load_json("weight.json")
    streak = calculate_streak(workouts)

    lines = []
    lines.append("# 健身记录报告")
    lines.append("")
    lines.append(f"**导出时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # Summary
    lines.append("## 数据概览")
    lines.append("")
    lines.append(f"- 总打卡次数: {len(workouts)}")
    lines.append(f"- 当前连续打卡: {streak}天")
    if weights:
        lines.append(f"- 当前体重: {weights[-1]['weight']}kg")
        if len(weights) >= 2:
            change = round(weights[-1]["weight"] - weights[0]["weight"], 1)
            sign = "+" if change > 0 else ""
            lines.append(
                f"- 体重变化: {sign}{change}kg "
                f"(从 {weights[0]['weight']}kg 到 {weights[-1]['weight']}kg)"
            )
    lines.append("")

    # Weight records
    if weights:
        lines.append("## 体重记录")
        lines.append("")
        lines.append("| 日期 | 体重 (kg) |")
        lines.append("|------|-----------|")
        for w in sorted(weights, key=lambda x: x["date"]):
            lines.append(f"| {w['date']} | {w['weight']} |")
        lines.append("")

    # Check-in records
    if workouts:
        lines.append("## 打卡记录")
        lines.append("")
        for w in sorted(workouts, key=lambda x: x["date"]):
            note_part = f" - {w['note']}" if w.get("note") else ""
            lines.append(f"- {w['date']} ✅{note_part}")
        lines.append("")

    md_content = "\n".join(lines)

    export_path = DATA_DIR / "export_temp.md"
    with open(export_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    return send_file(
        export_path,
        as_attachment=True,
        download_name=f"fitness_report_{today_str()}.md",
        mimetype="text/markdown",
    )


# ── Main ──────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
