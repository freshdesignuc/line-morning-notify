#!/usr/bin/env python3
"""
LINE Push Notification — AI秘書 × Opportunity Radar 統合通知

使い方:
  # 秘書ブリーフィング + Grok Radar を1通に統合（メイン）
  python scripts/line-notify.py --type morning --file briefings/2026-06-17-morning.md

  # 秘書ブリーフィングのみ（Grok APIキー不要）
  python scripts/line-notify.py --type briefing --file briefings/2026-06-17-morning.md

  # Web3市況セクションのみ
  python scripts/line-notify.py --type news --file briefings/2026-06-17-morning.md

  # カスタムメッセージ
  python scripts/line-notify.py --type custom --message "テストメッセージ"

環境変数:
  LINE_CHANNEL_ACCESS_TOKEN  : LINE Messaging APIチャンネルアクセストークン
  LINE_USER_ID               : 送信先のLINEユーザーID
  GROK_API_KEY               : xAI Grok API キー（--type morning 時に使用）
  GROK_MODEL                 : Grokモデル名（省略時: grok-3）
  DRY_RUN                    : 1/true でLINE送信をスキップしてログのみ出力
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows CP932 で絵文字が出力できない問題を回避
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf-8-sig"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf-8-sig"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ─── 定数 ─────────────────────────────────────────────────────────────────────

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
GROK_BASE_URL = "https://api.x.ai/v1"
GROK_MODEL    = os.getenv("GROK_MODEL", "grok-3")
JST           = timezone(timedelta(hours=9))
WEEKDAYS_JA   = ["月", "火", "水", "木", "金", "土", "日"]

_IMPACT_EMOJI   = {"high": "🔴", "medium": "🟡", "low": "🟢"}
_CATEGORY_EMOJI = {"AI": "🤖", "副業": "💼", "転職": "🏢", "FX": "📈", "Web3": "⛓️"}

GROK_SYSTEM_PROMPT = """あなたは日本人の副業投資家向けAIアナリストです。
AI・副業・転職・FX・暗号資産(Web3)に関する最新情報を分析し、
必ず以下のJSON形式のみで回答してください。マークダウン・コードブロック・説明文は不要です。

{
  "news": [
    {"title": "タイトル(25字以内)", "summary": "要約(50字以内)", "impact": "high|medium|low"},
    ...5件
  ],
  "opportunities": [
    {"title": "チャンス名(20字以内)", "description": "説明(60字以内)", "category": "AI|副業|転職|FX|Web3"},
    ...3件
  ],
  "actions": [
    {"action": "今日やること(30字以内)", "reason": "理由(40字以内)"},
    ...3件
  ]
}"""

# ─── .env 読み込み ────────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    env_path = Path(__file__).parent.parent / ".env.LINE通知"
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_path)
        return
    except ImportError:
        pass
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

_load_dotenv()

# ─── LINE 送信 ────────────────────────────────────────────────────────────────

def push_message(text: str) -> None:
    token   = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.environ.get("LINE_USER_ID", "")

    if not token:
        print("ERROR: LINE_CHANNEL_ACCESS_TOKEN が設定されていません", file=sys.stderr)
        sys.exit(1)
    if not user_id:
        print("ERROR: LINE_USER_ID が設定されていません", file=sys.stderr)
        sys.exit(1)

    # 前後の空白・改行を除去
    token   = token.strip()
    user_id = user_id.strip()

    # Authorizationヘッダーに使うためASCIIのみ許可
    try:
        token.encode("ascii")
    except UnicodeEncodeError as exc:
        print(f"ERROR: LINE_CHANNEL_ACCESS_TOKEN に非ASCII文字が含まれています: {exc}", file=sys.stderr)
        print("GitHub Settings → Secrets → LINE_CHANNEL_ACCESS_TOKEN を再設定してください", file=sys.stderr)
        sys.exit(1)

    preview = (token[:4] + "..." + token[-4:]) if len(token) > 8 else "***"
    print(f"[INFO] TOKEN len={len(token)}, preview={preview}", file=sys.stderr)

    if os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes"):
        print(f"[DRY_RUN] LINE送信スキップ\n--- メッセージ ({len(text)}字) ---\n{text}\n---")
        return

    payload = json.dumps({
        "to": user_id,
        "messages": [{"type": "text", "text": text}]
    }).encode("utf-8")

    req = urllib.request.Request(
        LINE_PUSH_URL,
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"[OK] LINE送信完了 (status={resp.status}, {len(text)}字)")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: LINE送信失敗 {e.code}: {body}", file=sys.stderr)
        sys.exit(1)

# ─── マークダウン除去 ──────────────────────────────────────────────────────────

def strip_md(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*",     r"\1", text)
    text = re.sub(r"`(.+?)`",       r"\1", text)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    text = re.sub(r"#+\s",          "",    text)
    return text.strip()

def _read_md(md_path: str) -> str:
    try:
        return Path(md_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""

# ─── 秘書ブリーフィング抽出 ───────────────────────────────────────────────────

def extract_briefing_summary(md_path: str) -> str:
    """フォーカス + BTC価格 + 部署タスク"""
    content = _read_md(md_path)
    if not content:
        return f"⚠ ブリーフィングファイルが見つかりません: {md_path}"

    today = datetime.now(JST).strftime("%Y年%m月%d日")

    # 今日のフォーカス
    focus_lines: list[str] = []
    focus_match = re.search(r"## 今日のフォーカス.*?\n((?:.*\n){1,8})", content)
    if focus_match:
        for line in focus_match.group(1).splitlines():
            line = strip_md(line.strip())
            if re.match(r"^\d+\.", line):
                # 先頭の "1. " を除去してからフォーカス内容を取得
                item_text = re.sub(r"^\d+\.\s*", "", line).split("→")[0].strip()
                focus_lines.append(item_text[:40])

    # BTC価格
    btc_match  = re.search(r"BTC.*?¥([\d,]+).*?(\([^)]+\))?", content)
    btc_price  = f"¥{btc_match.group(1)}" if btc_match else "—"
    btc_change = (btc_match.group(2) or "") if btc_match else ""

    # 部署タスク
    dept_tasks: list[str] = []
    skip = {"部署", "---", "今日のアクション"}
    for m in re.finditer(r"\| (.+?) \| (.+?) \|", content):
        dept = strip_md(m.group(1).strip())
        task = strip_md(m.group(2).strip())
        if dept not in skip and task not in skip:
            dept_tasks.append(f"  {dept}: {task[:28]}{'…' if len(task)>28 else ''}")

    lines = [f"【今日のフォーカス】"]
    lines += [f"  {i+1}. {f}" for i, f in enumerate(focus_lines[:3])]
    lines += ["", f"【BTC】 {btc_price} {btc_change}".strip()]
    if dept_tasks:
        lines += ["", "【部署タスク】"] + dept_tasks[:5]

    return "\n".join(lines)


def extract_news_summary(md_path: str) -> str:
    """Web3.0市況セクションを丸ごと送信"""
    content = _read_md(md_path)
    if not content:
        return f"⚠ ブリーフィングファイルが見つかりません: {md_path}"

    today = datetime.now(JST).strftime("%Y年%m月%d日")

    news_match = re.search(r"## Web3\.0市況\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not news_match:
        return "Web3.0市況情報が見つかりませんでした"

    news_lines: list[str] = []
    for line in news_match.group(1).splitlines():
        line = strip_md(line.strip())
        if not line or line.startswith("|") or line.startswith("---"):
            continue
        if line.startswith("-"):
            news_lines.append("  " + line[1:].strip())
        elif not line.startswith("#"):
            news_lines.append("  " + line)

    return "\n".join([f"Web3.0市況 — {today}", ""] + news_lines)

# ─── Grok Radar ───────────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """LLM応答からJSONを抽出（コードブロック・余分なテキストに対応）"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            pass
    match = re.search(r"\{[\s\S]+\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    raise ValueError(f"JSONを抽出できませんでした: {text[:200]}")


def fetch_grok_radar(today_str: str) -> dict | None:
    """Grok APIで市況分析を取得。APIキー未設定/パッケージ未インストール時はNoneを返す"""
    api_key = os.getenv("GROK_API_KEY")
    if not api_key:
        print("[INFO] GROK_API_KEY 未設定 → Radarセクションをスキップ", file=sys.stderr)
        return None

    try:
        from openai import OpenAI, APIError, RateLimitError
    except ImportError:
        print("[INFO] openaiパッケージ未インストール → Radarセクションをスキップ", file=sys.stderr)
        return None

    client = OpenAI(api_key=api_key, base_url=GROK_BASE_URL)

    user_msg = (
        f"今日は {today_str} です。\n"
        "AI技術・副業市場・転職市場・FX相場・暗号資産(Web3)の最新動向を分析し、"
        "日本人の個人投資家・副業実践者向けのJSON情報を返してください。"
    )

    for attempt in range(1, 4):
        try:
            print(f"[INFO] Grok API呼び出し (attempt {attempt}/3): {GROK_MODEL}", file=sys.stderr)
            response = client.chat.completions.create(
                model=GROK_MODEL,
                messages=[
                    {"role": "system", "content": GROK_SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.7,
                max_tokens=2000,
                extra_body={
                    "search_parameters": {
                        "mode": "auto",
                        "sources": [{"type": "web"}, {"type": "x"}],
                    }
                },
            )
            raw  = response.choices[0].message.content
            data = _extract_json(raw)
            print(
                f"[INFO] Radar取得完了: news={len(data.get('news',[]))}, "
                f"opps={len(data.get('opportunities',[]))}, "
                f"actions={len(data.get('actions',[]))}",
                file=sys.stderr,
            )
            return data

        except RateLimitError:
            print(f"[WARN] RateLimit → 5秒後リトライ", file=sys.stderr)
            time.sleep(5)
        except APIError as e:
            print(f"[WARN] Grok APIError (attempt {attempt}): {e}", file=sys.stderr)
            if attempt < 3:
                time.sleep(5)
        except (ValueError, Exception) as e:
            print(f"[WARN] Radar取得失敗 (attempt {attempt}): {e}", file=sys.stderr)
            if attempt < 3:
                time.sleep(5)

    print("[WARN] Grok API全リトライ失敗 → Radarセクションをスキップ", file=sys.stderr)
    return None


def format_radar_section(data: dict) -> str:
    """Radarデータ → LINEテキスト（セクションのみ）"""
    lines: list[str] = ["📰 今日の重要ニュース"]
    for i, item in enumerate(data.get("news", [])[:5], 1):
        emoji = _IMPACT_EMOJI.get(item.get("impact", "medium"), "🟡")
        lines.append(f"{emoji} {i}. {item.get('title', '')}")
        lines.append(f"   {item.get('summary', '')}")

    lines += ["", "💡 今日のチャンス"]
    for i, item in enumerate(data.get("opportunities", [])[:3], 1):
        cat   = item.get("category", "")
        emoji = _CATEGORY_EMOJI.get(cat, "✨")
        lines.append(f"{emoji} {i}.【{cat}】{item.get('title', '')}")
        lines.append(f"   {item.get('description', '')}")

    lines += ["", "⚡ 今日のアクション"]
    for i, item in enumerate(data.get("actions", [])[:3], 1):
        lines.append(f"▶ {i}. {item.get('action', '')}")
        lines.append(f"   → {item.get('reason', '')}")

    return "\n".join(lines)

# ─── 統合モーニングメッセージ ──────────────────────────────────────────────────

def build_morning_message(md_path: str) -> str:
    """秘書ブリーフィング（フォーカス＋BTC＋タスク＋Web3市況）を1通に統合。
    GROK_API_KEY が設定されている場合は Grok Radar セクションも追加。
    設定されていない場合は secretary の WebSearch 結果（Web3.0市況）を使用。
    """
    now       = datetime.now(JST)
    weekday   = WEEKDAYS_JA[now.weekday()]
    today_str = now.strftime(f"%Y年%m月%d日（{weekday}）")

    briefing_section = extract_briefing_summary(md_path)
    web3_section     = extract_news_summary(md_path)   # WebSearch 結果（無料）
    radar_data       = fetch_grok_radar(today_str)     # Grok（オプション）
    news_section     = build_free_news_section()       # RSS無料ニュース

    lines = [
        "🌅 AI秘書ブリーフィング",
        f"📅 {today_str}",
        "━━━━━━━━━━━━━━━",
        "",
        briefing_section,
        "",
        "━━━━━━━━━━━━━━━",
        "",
        web3_section,
    ]

    # RSS無料ニュース（Grok不問で常に追加）
    if news_section:
        lines += [
            "",
            "━━━━━━━━━━━━━━━",
            "",
            news_section,
        ]

    # Grok API キーがある場合のみ Radar セクションを追加
    if radar_data:
        lines += [
            "",
            "━━━━━━━━━━━━━━━",
            "📡 Grok Radar（追加分析）",
            "",
            format_radar_section(radar_data),
        ]

    lines += ["", "━━━━━━━━━━━━━━━", "Good luck today! 🚀"]
    return "\n".join(lines)

# ─── 自動モード（AI不要・完全無料） ──────────────────────────────────────────

def _fetch_btc_jpy() -> str:
    """CoinGecko 無料APIでBTC/JPY価格を取得。失敗時は '取得失敗' を返す"""
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=jpy"
    try:
        req  = urllib.request.Request(url, headers={"User-Agent": "line-notify/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            price = data["bitcoin"]["jpy"]
            return f"¥{price:,.0f}"
    except Exception as e:
        print(f"[WARN] BTC価格取得失敗: {e}", file=sys.stderr)
        return "取得失敗"


def _fetch_rss_news(url: str, max_items: int = 3) -> list[str]:
    """RSSフィードからニュースタイトルを取得"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "line-notify/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        titles: list[str] = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip().replace("　", " ").replace("  ", " ")
            if title:
                titles.append(title)
            if len(titles) >= max_items:
                break
        return titles
    except Exception as e:
        print(f"[WARN] RSS取得失敗 ({url}): {e}", file=sys.stderr)
        return []


def build_free_news_section() -> str:
    """無料RSSから暗号資産・AIニュースを取得してフォーマット（APIキー不要）"""
    sections: list[str] = []

    crypto = _fetch_rss_news("https://coinpost.jp/?feed=rss2", max_items=3)
    if crypto:
        block = ["⛓️ 暗号資産ニュース"]
        block += [f"  {i+1}. {t[:60]}{'…' if len(t) > 60 else ''}" for i, t in enumerate(crypto)]
        sections.append("\n".join(block))

    ai_news = _fetch_rss_news("https://rss.itmedia.co.jp/rss/2.0/aiplus.xml", max_items=2)
    if ai_news:
        block = ["🤖 AIニュース"]
        block += [f"  {i+1}. {t[:60]}{'…' if len(t) > 60 else ''}" for i, t in enumerate(ai_news)]
        sections.append("\n".join(block))

    if not sections:
        return ""
    return "📰 今日のニュース\n\n" + "\n\n".join(sections)


def _parse_tasks_md(tasks_path: str = "briefings/tasks.md") -> dict[str, list[str]]:
    """tasks.md から未完了タスクをセクション別に取得"""
    content = _read_md(tasks_path)
    sections: dict[str, list[str]] = {}
    current = ""
    for line in content.splitlines():
        if line.startswith("## "):
            current = line.lstrip("# ").strip()
            sections[current] = []
        elif current and re.match(r"- \[ \]", line):
            task = re.sub(r"^- \[ \]\s*", "", line).split("→")[0].strip()
            sections[current].append(task[:35] + ("…" if len(task) > 35 else ""))
    return sections


def build_auto_message() -> str:
    """AI不要・完全無料の自動モーニングメッセージ。
    - tasks.md からタスク取得（コミット済みデータ）
    - CoinGecko から BTC/JPY 取得（無料API）
    - LINE に送信
    """
    now       = datetime.now(JST)
    weekday   = WEEKDAYS_JA[now.weekday()]
    today_str = now.strftime(f"%Y年%m月%d日（{weekday}）")

    tasks     = _parse_tasks_md()
    btc_jpy   = _fetch_btc_jpy()
    news_section = build_free_news_section()

    # 優先タスク → フォーカスに使用
    priority = tasks.get("優先タスク（今日必ずやる）", [])
    side     = tasks.get("副業タスク", [])

    lines = [
        "🌅 AI秘書モーニング",
        f"📅 {today_str}",
        "━━━━━━━━━━━━━━━",
        "",
        "【今日の優先タスク】",
    ]
    if priority:
        lines += [f"  {i+1}. {t}" for i, t in enumerate(priority[:3])]
    else:
        lines.append("  （tasks.md に未完了タスクなし）")

    lines += ["", f"【BTC】 {btc_jpy}（CoinGecko）"]

    if side:
        lines += ["", "【副業タスク】"]
        lines += [f"  - {t}" for t in side[:3]]

    if news_section:
        lines += ["", "━━━━━━━━━━━━━━━", "", news_section]

    lines += ["", "━━━━━━━━━━━━━━━", "Good luck today! 🚀"]
    return "\n".join(lines)


# ─── エントリーポイント ────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="LINE Push Notification")
    parser.add_argument(
        "--type",
        choices=["auto", "morning", "briefing", "news", "custom"],
        default="auto",
        help=(
            "送信タイプ: "
            "auto=AI不要・完全無料（tasks.md＋CoinGecko）, "
            "morning=秘書ブリーフィング統合, "
            "briefing=秘書のみ, news=市況のみ, custom=任意文字列"
        ),
    )
    parser.add_argument("--file",    help="ブリーフィングMDファイルパス（morning/briefing/news 時）")
    parser.add_argument("--message", help="カスタムメッセージ（custom 時）")
    args = parser.parse_args()

    if args.type == "auto":
        text = build_auto_message()

    elif args.type == "morning":
        if not args.file:
            today    = datetime.now(JST).strftime("%Y-%m-%d")
            auto_path = Path("briefings") / f"{today}-morning.md"
            args.file = str(auto_path)
            print(f"[INFO] --file 未指定 → {args.file} を使用", file=sys.stderr)
        text = build_morning_message(args.file)

    elif args.type in ("briefing", "news"):
        if not args.file:
            print("ERROR: --file を指定してください", file=sys.stderr)
            sys.exit(1)
        text = (extract_briefing_summary(args.file)
                if args.type == "briefing"
                else extract_news_summary(args.file))

    else:  # custom
        if not args.message:
            print("ERROR: --message を指定してください", file=sys.stderr)
            sys.exit(1)
        text = args.message

    push_message(text)


if __name__ == "__main__":
    main()
