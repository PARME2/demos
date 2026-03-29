#!/usr/bin/env python3
"""
UI検討エージェントチーム - ビルド&レビューサイクル ダッシュボード
Usage: python dashboard.py "議論のお題"
http://localhost:8765

PM がファシリテーション → UXデザイナーがアイデア出し →
エンジニアがモック実装 → 全員でレビュー → 改善 のサイクルを回す
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

AGENTS_DIR = Path(__file__).parent
PROJECT_DIR = AGENTS_DIR.parent.parent
SESSIONS_DIR = PROJECT_DIR / "agent_sessions"
# SESSION_DIR is set at startup in main()
SESSION_DIR = None
MOCK_DIR = None
PORT = 8765

AGENTS = {
    "01_user_context_verifier": {"label": "コンテキスト検証", "color": "#FF6B6B", "icon": "🎬"},
    "02_value_output_verifier": {"label": "バリュー検証", "color": "#4ECDC4", "icon": "📦"},
    "03_info_cognition_designer": {"label": "認知設計", "color": "#A78BFA", "icon": "🧠"},
    "04_pm": {"label": "PM", "color": "#F59E0B", "icon": "👔"},
    "05_engineer": {"label": "エンジニア", "color": "#34D399", "icon": "🛠"},
    "06_pm_assistant": {"label": "PM補佐", "color": "#F472B6", "icon": "🔍"},
}

REVIEWER_AGENTS = ["01_user_context_verifier", "02_value_output_verifier", "03_info_cognition_designer"]

meeting_log = []
meeting_lock = threading.Lock()
meeting_status = {"phase": "waiting", "current_speaker": "", "mockup_version": 0, "waiting_for_user": False}

# ユーザー入力待ち用
user_input_event = threading.Event()
user_input_text = ""

USER_AGENT = {"label": "あなた", "color": "#60A5FA", "icon": "💬"}


def add_user_message(content):
    """ユー��ーの発言をログに追加"""
    with meeting_lock:
        meeting_log.append({
            "agent_id": "user",
            "label": USER_AGENT["label"],
            "color": USER_AGENT["color"],
            "icon": USER_AGENT["icon"],
            "content": content,
            "type": "user",
            "timestamp": time.time(),
        })


def add_error_message(error_text, agent_id=None):
    """エラーメッセージをログに追加"""
    with meeting_lock:
        meeting_log.append({
            "agent_id": agent_id or "system",
            "label": AGENTS[agent_id]["label"] if agent_id and agent_id in AGENTS else "System",
            "color": "#EF4444",
            "icon": "⚠️",
            "content": error_text,
            "type": "error",
            "timestamp": time.time(),
        })


def wait_for_user_input(timeout=300):
    """ユーザーの入力を待つ（最大timeout秒）"""
    global user_input_text
    meeting_status["waiting_for_user"] = True
    user_input_event.clear()
    user_input_event.wait(timeout=timeout)
    meeting_status["waiting_for_user"] = False
    result = user_input_text
    user_input_text = ""
    return result


def add_message(agent_id, content, msg_type="message"):
    with meeting_lock:
        meeting_log.append({
            "agent_id": agent_id,
            "label": AGENTS[agent_id]["label"],
            "color": AGENTS[agent_id]["color"],
            "icon": AGENTS[agent_id]["icon"],
            "content": content,
            "type": msg_type,
            "timestamp": time.time(),
        })


def call_agent_streaming(agent_id, prompt):
    agent_file = AGENTS_DIR / f"{agent_id}.md"
    if not agent_file.exists():
        error_msg = f"エージェントファイルが見つかりません: {agent_id}"
        add_error_message(error_msg, agent_id)
        return f"[{error_msg}]"

    system_prompt = agent_file.read_text()
    full_prompt = f"""以下のシステムプロンプトに従って回答してください。

--- システムプロンプト ---
{system_prompt}
--- システムプロンプトここまで ---

{prompt}"""

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    with meeting_lock:
        msg_index = len(meeting_log)
        meeting_log.append({
            "agent_id": agent_id,
            "label": AGENTS[agent_id]["label"],
            "color": AGENTS[agent_id]["color"],
            "icon": AGENTS[agent_id]["icon"],
            "content": "",
            "type": "message",
            "timestamp": time.time(),
            "streaming": True,
        })

    log(f"  → {AGENTS[agent_id]['label']} 呼び出し開始")
    try:
        proc = subprocess.Popen(
            ["claude", "-p", "--dangerously-skip-permissions"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
        )
        proc.stdin.write(full_prompt)
        proc.stdin.close()

        full_text = ""
        for line in proc.stdout:
            full_text += line
            with meeting_lock:
                meeting_log[msg_index]["content"] = full_text

        exit_code = proc.wait()
        log(f"  ← {AGENTS[agent_id]['label']} 完了 (exit:{exit_code}, {len(full_text)}文字)")
        with meeting_lock:
            meeting_log[msg_index]["streaming"] = False

        # 終了コードやエラー出力をチェック
        if exit_code != 0:
            add_error_message(f"{AGENTS[agent_id]['label']} がエラーで終了しました (exit code: {exit_code})\n内容: {full_text[:200]}", agent_id)
        elif not full_text.strip():
            add_error_message(f"{AGENTS[agent_id]['label']} の応答が空でした", agent_id)
        elif full_text.strip().startswith("error:") or full_text.strip().startswith("Error:"):
            add_error_message(f"{AGENTS[agent_id]['label']}: {full_text.strip()[:300]}", agent_id)

        return full_text.strip()
    except Exception as e:
        log(f"  ✗ {AGENTS[agent_id]['label']} 例外: {e}")
        error_msg = f"{AGENTS[agent_id]['label']} の実行に失敗: {e}"
        with meeting_lock:
            meeting_log[msg_index]["content"] = f"[エラー] {error_msg}"
            meeting_log[msg_index]["streaming"] = False
            meeting_log[msg_index]["type"] = "error"
        add_error_message(error_msg, agent_id)
        return f"[エラー: {e}]"


def extract_html(text):
    """エンジニアの応答からHTMLコードを抽出"""
    match = re.search(r'---HTML_START---(.*?)---HTML_END---', text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # フォールバック: ```html ... ``` を探す
    match = re.search(r'```html\s*(<!DOCTYPE.*?)</\s*html>\s*```', text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip() + "\n</html>"
    return None


def save_mockup(html_content, version):
    """モックアップHTMLをファイルに保存"""
    path = MOCK_DIR / f"mockup_v{version}.html"
    path.write_text(html_content, encoding="utf-8")
    # demosディレクトリにも最新版を保存
    demo_path = PROJECT_DIR / f"demo_prototype_v{version}.html"
    demo_path.write_text(html_content, encoding="utf-8")
    return path


def get_all_messages_text():
    with meeting_lock:
        parts = []
        for msg in meeting_log:
            if msg["type"] in ("message", "user", "facilitator"):
                parts.append(f"**{msg['label']}**: {msg['content']}")
        return "\n\n".join(parts)


def run_parallel_reviewers(prompt_fn):
    """固定層レビューアーエージェントを並列実行"""
    threads = []
    results = {}
    def _run(aid):
        results[aid] = call_agent_streaming(aid, prompt_fn(aid))
    for aid in REVIEWER_AGENTS:
        meeting_status["current_speaker"] = aid
        t = threading.Thread(target=_run, args=(aid,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return results


def check_user_interjection():
    """会議の合間にユーザーからの割り込み発言がないかチェック"""
    global user_input_text
    if user_input_event.is_set():
        text = user_input_text
        user_input_text = ""
        user_input_event.clear()
        return text
    return None


def run_meeting(topic):
    """会議を進行：ヒアリング → アイデア → ビルド → レビュー → 改善サイクル"""
    try:
        _run_meeting_inner(topic)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        add_error_message(f"会議の実行中に予期しないエラーが発生しました:\n{e}\n\n{tb}")
        meeting_status["phase"] = "done"
        meeting_status["current_speaker"] = ""
        save_session_log()
        log(f"エラーで会議終了。ログ保存済み: {SESSION_DIR}")


_log_file = None
_log_lines = []

def log(msg):
    """タイムスタンプ付きでコンソール＋ファイル＋メモリにログ出力"""
    global _log_file
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(f"  {line}", flush=True)
    _log_lines.append(line)
    if len(_log_lines) > 500:
        _log_lines.pop(0)
    if _log_file:
        try:
            _log_file.write(line + "\n")
            _log_file.flush()
        except:
            pass


def _run_meeting_inner(topic):
    log(f"会議開始: {topic[:50]}...")

    # ========== Phase 0: ヒアリング ==========
    log("Phase 0: ヒアリング開始")
    meeting_status["phase"] = "hearing"
    meeting_status["current_speaker"] = "04_pm"

    hearing_response = call_agent_streaming("04_pm", f"""お題: {topic}

PMとして、設計に入る前に以下の**4点を明文化**してください。お題から読み取れる情報で埋め、不足分はユーザーに質問してください。

1. **誰が使うか** — 年齢・関係性・デジタルリテラシー
2. **いつ・どこで使うか** — 場所・時間・デバイス
3. **このプロダクトが出すべきコアアウトプットは何か**
4. **そのアウトプットが「正しい」とはどういう状態か**

お題から明確に判断できるものは明文化し、不足があればユーザーに聞きたい質問を**最大3つ**、「〜について教えてください」の形式で簡潔に聞いてください。
全て読み取れる場合は4点を明文化した上で「会議を始めます。」と言ってください。""")

    # PMが質問したかどうか判定
    needs_input = "教えてください" in hearing_response or "聞かせてください" in hearing_response or "？" in hearing_response
    if needs_input and "情報は十分です" not in hearing_response:
        add_message("04_pm", "回答をお願いします。（下の入力欄から送信してください）", "facilitator")
        user_response = wait_for_user_input()
        if user_response:
            topic = f"{topic}\n\n【補足情報】\n{user_response}"

    time.sleep(1)

    # ========== Phase 1: アイデア出し ==========
    log("Phase 1: アイデア出し開始")
    meeting_status["phase"] = "ideation"
    meeting_status["current_speaker"] = "04_pm"
    add_message("04_pm",
        f"会議を始めます。\n\n"
        f"**お題：{topic}**\n\n"
        f"各自の専門視点から、このプロダクトの設計案を出してください。\n"
        f"**アプリ名・コア体験・画面構成**の3点に絞って簡潔に。\n"
        f"その上で、自分の専門領域から見た最重要チェックポイントを1つ挙げてください。",
        "facilitator")
    time.sleep(1)

    run_parallel_reviewers(lambda aid: f"""お題: {topic}

PMが定義した4点：
{get_all_messages_text()}

PMの指示：「アプリ名・コア体験・画面構成の3点に絞って簡潔に。自分の専門領域から見た最重要チェックポイントを1つ挙げてください。」

あなたの専門視点から提案してください。各項目3行以内で。""")

    time.sleep(1)

    # --- PM補佐: 3軸チェック ---
    meeting_status["current_speaker"] = "06_pm_assistant"
    call_agent_streaming("06_pm_assistant", f"""お題: {topic}

これまでの議論：
{get_all_messages_text()}

PM補佐として3軸でチェックしてください：
- 軸1: ユーザーモデルのズレ（PMが定義したユーザー像と提案がズレていないか）
- 軸2: コアアウトプットの品質（提案されたアウトプットは価値あるものになるか）
- 軸3: 専門知識との整合（領域固有の判断を非専門家が勝手に決めていないか）

簡潔に（10行以内）。""")

    time.sleep(1)

    # --- エンジニア: 開発観点の意見出し ---
    meeting_status["current_speaker"] = "05_engineer"
    call_agent_streaming("05_engineer", f"""お題: {topic}

3名のUXデザイナーから提案が出ました：
{get_all_messages_text()}

エンジニアとして、PMが方針を決める前に開発観点で意見を出してください：
- 各案の実現可能性（単一HTMLモックで作れる範囲か）
- 開発難易度（軽い/普通/重い）
- 「これはモックでは難しいが、こうすれば近い体験を再現できる」等の代替案
- 技術的に面白いアイデアがあれば提案

簡潔に（10行以内）。コードは書かないでください。""")

    time.sleep(1)

    # ========== Phase 2: PM方針決定 ==========
    log("Phase 2: PM方針決定")
    meeting_status["phase"] = "direction"
    meeting_status["current_speaker"] = "04_pm"

    proposals = get_all_messages_text()
    pm_direction = call_agent_streaming("04_pm", f"""お題: {topic}

これまでの議論（PM補佐の情報補足、エンジニアの開発観点コメント含む）：
{proposals}

PMとして：
1. 全員の意見を踏まえ、共通要素と最も強い要素を2-3行で整理
2. エンジニアの実現可能性コメントを考慮して現実的な判断をする
3. エンジニアへの実装指示を出してください：何を作るか、画面構成、必須の体験要素を具体的に
簡潔に（15行以内）""")

    time.sleep(1)

    # --- PM補佐: 方針の軌道チェック ---
    meeting_status["current_speaker"] = "06_pm_assistant"
    call_agent_streaming("06_pm_assistant", f"""お題: {topic}

PMが方針を出しました：
{get_all_messages_text()}

PM補佐として3軸でチェックしてください：
- 軸1: PMの方針はPMが定義したユーザー像と整合しているか
- 軸2: コアアウトプットが価値あるものになる方針か
- 軸3: 領域固有の判断を非専門家が勝手に決めていないか
- 問題なければ「✅ 3軸チェックOK」、問題あれば「⚠️ 軸[1/2/3]チェック」のフォーマットで

簡潔に（5行以内）。""")

    time.sleep(1)

    # ========== Phase 3: エンジニアがビルド ==========
    log("Phase 3: エンジニアがビルド")
    meeting_status["phase"] = "building"
    meeting_status["current_speaker"] = "05_engineer"

    add_message("04_pm", "エンジニア、上記の方針でモックを作ってください。", "facilitator")
    time.sleep(0.5)

    engineer_response = call_agent_streaming("05_engineer", f"""お題: {topic}

会議のこれまでの議論：
{get_all_messages_text()}

上記のPMの指示に従い、動くHTMLモックアップを作ってください。
単一HTMLファイル、モバイルファースト(max-width:430px)、外部ライブラリ不使用。
必ず ---HTML_START--- と ---HTML_END--- でHTMLコードを囲んでください。""")

    # HTMLを抽出して保存
    html = extract_html(engineer_response)
    if html:
        meeting_status["mockup_version"] = 1
        save_mockup(html, 1)
        add_message("04_pm", "モックアップ v1 が完成しました。右のプレビューで確認してください。\nレビューをお願いします。", "facilitator")
    else:
        add_message("04_pm", "HTMLの抽出に失敗しました。エンジニア、再度出力してください。", "facilitator")

    time.sleep(1)

    # ========== Phase 4: レビュー→改善サイクル（PMが完了判断するまで） ==========
    log("Phase 4: レビュー→改善サイクル開始")
    MAX_ITERATIONS = 10  # 安全弁
    iteration = 0

    while html and iteration < MAX_ITERATIONS:
        iteration += 1
        log(f"  サイクル {iteration} 開始")

        # ユーザー割り込みチェック
        interjection = check_user_interjection()
        if interjection:
            add_message("04_pm", "ユーザーからフィードバックが入りました。これを踏まえてレビューを進めます。", "facilitator")

        meeting_status["phase"] = f"review{iteration}"
        current_version = meeting_status["mockup_version"]

        # UXエージェントがレビュー
        run_parallel_reviewers(lambda aid: f"""お題: {topic}

エンジニアがモックアップ v{current_version} を作りました。
以下が会議の流れです：
{get_all_messages_text()}

このモックアップをあなたの専門視点でレビューしてください。
実際に触った想定で：
1. 良い点（1-2個）
2. 改善すべき点（1-2個、具体的に「この画面のここをこう変える」レベルで）
3. このまま完成でOKなら「完成でOK」と明記

簡潔に（10行以内）。""")

        time.sleep(1)

        # PM補佐: レビュー内容の軌道チェック
        meeting_status["current_speaker"] = "06_pm_assistant"
        call_agent_streaming("06_pm_assistant", f"""お題: {topic}

レビューが出ました：
{get_all_messages_text()}

PM補佐として3軸でチェック：
- 軸1: レビューがPM定義のユーザー像と整合しているか
- 軸2: コアアウトプットの品質は改善されているか（UIだけでなく中身）
- 軸3: 領域固有の判断が正しいか
- これ以上改善サイクルを回す必要があるか、完成でよいか

簡潔に（5行以内）。""")

        time.sleep(1)

        # PM が継続/完了を判断
        meeting_status["phase"] = f"feedback{iteration}"
        meeting_status["current_speaker"] = "04_pm"

        pm_decision = call_agent_streaming("04_pm", f"""会議の全内容（PM補佐のチェック含む）：
{get_all_messages_text()}

PMとして判断してください：

A) まだ改善が必要 → 最優先で直すべき点（1-2個）と具体的な変更内容をエンジニアに指示
B) 完成 → 「【完了】これ以上の改善は不要です。」と明記

全員が概ね納得しており、大目的（高校生の第一印象＋継続利用）を満たしていれば完了と判断してください。
完璧を求めすぎず、十分な品質に達したら完了にしてください。
簡潔に（8行以内）。""")

        # PMが完了判断したかチェック
        if "【完了】" in pm_decision:
            log(f"  PM完了判断 → サイクル終了 (計{iteration}回)")
            break
        else:
            log(f"  PM: 改善継続")

        time.sleep(1)

        # エンジニアが改善
        meeting_status["phase"] = f"improving{iteration}"
        meeting_status["current_speaker"] = "05_engineer"

        engineer_response = call_agent_streaming("05_engineer", f"""会議の全内容：
{get_all_messages_text()}

PMの改善指示に従い、モックアップを修正してください。
修正後の完全なHTMLを出力してください（差分ではなく全体）。
必ず ---HTML_START--- と ---HTML_END--- でHTMLコードを囲んでください。""")

        new_html = extract_html(engineer_response)
        if new_html:
            html = new_html
            new_version = current_version + 1
            meeting_status["mockup_version"] = new_version
            save_mockup(html, new_version)
            add_message("04_pm", f"モックアップ v{new_version} に更新されました。プレビューを確認してください。", "facilitator")
        else:
            add_message("04_pm", "HTML抽出に失敗。前のバージョンを維持します。", "facilitator")

        time.sleep(1)

    # ========== Phase 5: 最終判定 ==========
    log("Phase 5: 最終判定")
    meeting_status["phase"] = "decision"
    meeting_status["current_speaker"] = "04_pm"

    call_agent_streaming("04_pm", f"""会議の全内容：
{get_all_messages_text()}

最終判定を出してください：

━━━ 最終判定 ━━━
【完成したもの】（1行）
【バージョン】v{meeting_status['mockup_version']}
【改善サイクル】{iteration}回
【採用した要素】（誰のどの意見か。2-3行）
【残課題】（次回改善すべき点。2-3個）
【ファイル】demo_prototype_v{meeting_status['mockup_version']}.html として保存済み
""")

    meeting_status["phase"] = "done"
    meeting_status["current_speaker"] = ""
    add_message("04_pm", "会議を終了します。お疲れ様でした。", "facilitator")

    # セッションログ自動保存
    save_session_log()
    log(f"会議完了。セッション: {SESSION_DIR}")


# ============================================================
# HTML Template
# ============================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent Meeting + Live Build</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans',sans-serif;background:#0F0F1A;color:#E2E8F0;display:flex;flex-direction:column}

.header{background:linear-gradient(135deg,#1a1a2e,#16213e);padding:14px 20px;border-bottom:1px solid #2D3748;display:flex;align-items:center;gap:14px;flex-shrink:0}
.header h1{font-size:17px;font-weight:700;background:linear-gradient(135deg,#4ECDC4,#A78BFA);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header .ver-badge{font-size:12px;padding:3px 10px;border-radius:10px;background:#34D39933;color:#34D399;font-weight:600}

.status-bar{display:flex;gap:8px;padding:8px 20px;background:#1A1A2E;border-bottom:1px solid #2D3748;flex-shrink:0;flex-wrap:wrap;align-items:center;font-size:12px}
.phase-badge{padding:3px 10px;border-radius:10px;font-size:11px;font-weight:600;background:#2D3748;color:#64748B}
.phase-badge.active{background:#F59E0B;color:#0F0F1A}
.status-item{display:flex;align-items:center;gap:3px;color:#94A3B8}
.status-dot{width:7px;height:7px;border-radius:50%;background:#4A5568}
.status-dot.speaking{background:#4ADE80;animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

/* Main layout: timeline left, preview right */
.main{display:flex;flex:1;overflow:hidden;min-height:0}

.timeline-panel{flex:1;display:flex;flex-direction:column;border-right:1px solid #2D3748;min-width:0;min-height:0;overflow:hidden}
.timeline-container{flex:1;overflow-y:auto;padding:20px;scroll-behavior:smooth;min-height:0}
.timeline{max-width:700px;display:flex;flex-direction:column;gap:16px;padding-bottom:80px}

.preview-panel{width:480px;display:flex;flex-direction:column;flex-shrink:0;background:#1A1A2E}
.preview-header{padding:12px 16px;border-bottom:1px solid #2D3748;display:flex;align-items:center;gap:8px;flex-shrink:0}
.preview-header h2{font-size:14px;font-weight:600;color:#34D399}
.preview-header .ver{font-size:11px;color:#94A3B8;margin-left:auto}
.preview-frame-wrap{flex:1;display:flex;align-items:center;justify-content:center;padding:16px;background:#111;overflow:hidden}
.preview-frame{width:430px;height:100%;border:none;border-radius:12px;background:#fff}
.preview-empty{color:#64748B;font-size:13px;text-align:center}

/* Console */
.console-toggle{padding:8px 16px;border-top:1px solid #2D3748;font-size:11px;color:#64748B;cursor:pointer;flex-shrink:0;display:flex;align-items:center;gap:6px;user-select:none}
.console-toggle:hover{color:#94A3B8}
.console-panel{height:180px;overflow-y:auto;background:#0a0a12;padding:8px 12px;font-family:'SF Mono',Menlo,monospace;font-size:11px;line-height:1.6;color:#94A3B8;flex-shrink:0;display:none}
.console-panel.open{display:block}
.console-panel::-webkit-scrollbar{width:4px}
.console-panel::-webkit-scrollbar-thumb{background:#4A5568;border-radius:2px}
.log-error{color:#FCA5A5}
.log-ok{color:#86EFAC}

/* Messages */
.msg{display:flex;gap:10px;animation:fadeIn .3s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.msg-avatar{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
.msg-body{flex:1;min-width:0}
.msg-header{display:flex;align-items:center;gap:8px;margin-bottom:4px}
.msg-name{font-size:13px;font-weight:700}
.msg-badge{font-size:10px;padding:1px 7px;border-radius:6px;background:#2D3748;color:#94A3B8}
.msg-content{background:#1A1A2E;border:1px solid #2D3748;border-radius:4px 14px 14px 14px;padding:12px 16px;font-size:13px;line-height:1.85;white-space:pre-wrap;word-break:break-word;overflow-x:auto}
.msg.facilitator .msg-content{background:linear-gradient(135deg,#1a1a2e,#1e293b);border-color:#F59E0B33}
.msg.user-msg .msg-content{background:linear-gradient(135deg,#1a1a2e,#172554);border-color:#60A5FA33}
.msg.error-msg .msg-content{background:linear-gradient(135deg,#1a1a2e,#3b1111);border-color:#EF444444;color:#FCA5A5}
.msg-content h1{font-size:16px;color:#F1F5F9;margin:12px 0 4px}
.msg-content h2{font-size:15px;color:#F1F5F9;margin:10px 0 4px}
.msg-content h3{font-size:14px;color:#F1F5F9;margin:8px 0 4px}
.msg-content strong{color:#F1F5F9}
.msg-content code{background:#0F0F1A;padding:2px 5px;border-radius:3px;font-size:11px}
.msg-content pre{background:#0F0F1A;padding:10px;border-radius:6px;margin:6px 0;overflow-x:auto;font-size:11px}
.msg-content table{border-collapse:collapse;margin:6px 0;width:100%}
.msg-content th,.msg-content td{border:1px solid #4A5568;padding:5px 8px;font-size:11px;text-align:left}
.msg-content th{background:#0F0F1A}
.msg-content hr{border:none;border-top:1px solid #2D3748;margin:10px 0}
.msg-content ul,.msg-content ol{padding-left:18px;margin:4px 0}
.streaming-cursor::after{content:'▊';animation:blink .8s step-end infinite;color:#4ECDC4}
@keyframes blink{0%,50%{opacity:1}51%,100%{opacity:0}}

.footer{padding:6px 20px;background:#1A1A2E;border-top:1px solid #2D3748;font-size:11px;color:#64748B;display:flex;justify-content:space-between;flex-shrink:0}

.timeline-container::-webkit-scrollbar{width:5px}
.timeline-container::-webkit-scrollbar-thumb{background:#4A5568;border-radius:3px}

/* User input bar */
.user-input-bar{padding:10px 16px;background:#1A1A2E;border-top:1px solid #2D3748;display:flex;gap:8px;align-items:center;flex-shrink:0}
.user-input-bar input{flex:1;background:#0F0F1A;border:1px solid #4A5568;border-radius:8px;padding:10px 14px;color:#E2E8F0;font-size:13px;font-family:inherit;outline:none;transition:border-color .2s}
.user-input-bar input:focus{border-color:#60A5FA}
.user-input-bar input::placeholder{color:#64748B}
.user-input-bar button{padding:10px 18px;border:none;border-radius:8px;background:#60A5FA;color:#0F0F1A;font-size:13px;font-weight:600;font-family:inherit;cursor:pointer;white-space:nowrap;transition:opacity .2s}
.user-input-bar button:hover{opacity:.85}
.user-input-bar button:disabled{opacity:.4;cursor:not-allowed}
.waiting-badge{padding:3px 10px;border-radius:10px;font-size:11px;font-weight:600;background:#60A5FA33;color:#60A5FA;animation:pulse 1.5s infinite}

/* Start screen */
.start-screen{display:flex;align-items:center;justify-content:center;flex:1;padding:40px}
.start-card{background:#1A1A2E;border:1px solid #2D3748;border-radius:16px;padding:40px;max-width:560px;width:100%}
.start-card h2{font-size:22px;font-weight:700;margin-bottom:8px;background:linear-gradient(135deg,#4ECDC4,#A78BFA);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.start-card p{font-size:13px;color:#94A3B8;margin-bottom:24px;line-height:1.7}
.start-card textarea{width:100%;background:#0F0F1A;border:1px solid #4A5568;border-radius:10px;padding:14px 16px;color:#E2E8F0;font-size:14px;font-family:inherit;resize:vertical;min-height:80px;line-height:1.6;outline:none;transition:border-color .2s}
.start-card textarea:focus{border-color:#4ECDC4}
.start-card textarea::placeholder{color:#64748B}
.start-btn{width:100%;margin-top:16px;padding:14px;border:none;border-radius:10px;background:linear-gradient(135deg,#4ECDC4,#A78BFA);color:#0F0F1A;font-size:15px;font-weight:700;font-family:inherit;cursor:pointer;transition:opacity .2s,transform .1s}
.start-btn:hover{opacity:.9}
.start-btn:active{transform:scale(.98)}
.start-btn:disabled{opacity:.5;cursor:not-allowed}
.team-list{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:24px}
.team-chip{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:8px;font-size:11px;font-weight:600;background:#2D3748;color:#94A3B8}
.sessions-title{font-size:13px;font-weight:600;color:#94A3B8;margin-bottom:10px;border-top:1px solid #2D3748;padding-top:16px}
.session-item{display:flex;align-items:center;gap:10px;padding:10px 12px;background:#0F0F1A;border:1px solid #2D3748;border-radius:8px;margin-bottom:8px;cursor:pointer;transition:border-color .2s}
.session-item:hover{border-color:#4ECDC4}
.session-meta{flex:1;min-width:0}
.session-meta .id{font-size:12px;color:#94A3B8;font-family:monospace}
.session-meta .topic{font-size:12px;color:#E2E8F0;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.session-stats{font-size:11px;color:#64748B;display:flex;gap:8px;flex-shrink:0}
.session-resume-btn{padding:5px 12px;border:1px solid #4ECDC4;border-radius:6px;background:transparent;color:#4ECDC4;font-size:11px;font-weight:600;cursor:pointer;flex-shrink:0;font-family:inherit}
.session-resume-btn:hover{background:#4ECDC422}
</style>
</head>
<body>

<!-- Start Screen -->
<div id="startScreen">
  <div class="header">
    <h1>Agent Meeting + Live Build</h1>
  </div>
  <div class="start-screen">
    <div class="start-card">
      <h2>会議を始める</h2>
      <p>お題を入力してスタート。エージェントチームがアイデア出し → モック開発 → レビュー → 改善を自動で回します。</p>
      <div class="team-list" id="teamList"></div>
      <textarea id="topicInput" placeholder="例：「1年の振り返り → 進路の悩み解決」高校生向けアプリ。見た瞬間やりたいと思い、継続的に使いたくなるUIを作ってください。" rows="3"></textarea>
      <button class="start-btn" id="startBtn" onclick="startMeeting()">新しい会議をスタート</button>
      <div id="sessionsArea" style="margin-top:24px"></div>
    </div>
  </div>
</div>

<!-- Meeting Screen (hidden initially) -->
<div id="meetingScreen" style="display:none;height:100vh;flex-direction:column;overflow:hidden">
  <div class="header">
    <h1>Agent Meeting + Live Build</h1>
    <span class="ver-badge" id="verBadge">v0</span>
  </div>
  <div class="status-bar" id="statusBar"></div>
  <div class="main">
    <div class="timeline-panel">
      <div class="timeline-container" id="timelineContainer">
        <div class="timeline" id="timeline"></div>
      </div>
      <div class="user-input-bar" id="userInputBar">
        <span class="waiting-badge" id="waitingBadge" style="display:none">回答待ち</span>
        <input type="text" id="userInput" placeholder="補足情報や指示を入力..." />
        <button id="userSendBtn" onclick="sendUserMessage()">送信</button>
      </div>
    </div>
    <div class="preview-panel">
      <div class="preview-header">
        <h2>Live Preview</h2>
        <span class="ver" id="previewVer">ビルド待ち</span>
      </div>
      <div class="preview-frame-wrap" id="previewWrap">
        <div class="preview-empty">エンジニアがモックを<br>ビルドすると表示されます</div>
      </div>
      <div class="console-toggle" onclick="toggleConsole()"><span id="consoleArrow">▶</span> Console</div>
      <div class="console-panel" id="consolePanel"></div>
    </div>
  </div>
  <div class="footer">
    <span id="elapsed">Elapsed: 0:00</span>
    <button id="restartBtn" style="display:none;padding:4px 14px;border:1px solid #4A5568;border-radius:6px;background:transparent;color:#E2E8F0;font-size:11px;cursor:pointer;font-family:inherit" onclick="location.reload()">新しい会議を始める</button>
    <span id="msgCount">Messages: 0</span>
  </div>
</div>

<script>
const AGENTS=__AGENTS_JSON__;
let startTime=Date.now();
let renderedCount=0;
let lastHashes={};
let autoScroll=true;
let currentMockVer=0;
let meetingStarted=false;

// Show team chips on start screen
const teamList=document.getElementById('teamList');
for(const[id,info]of Object.entries(AGENTS)){
  teamList.innerHTML+=`<span class="team-chip"><span style="color:${info.color}">${info.icon}</span>${info.label}</span>`;
}

// Auto-start if meeting already running (CLI arg mode)
fetch('/api/meeting').then(r=>r.json()).then(d=>{
  if(d.messages.length>0||d.status.phase!=='waiting'){
    switchToMeeting();
  }
});

// Load past sessions
async function loadSessions(){
  const resp=await fetch('/api/sessions');
  const sessions=await resp.json();
  const area=document.getElementById('sessionsArea');
  const valid=sessions.filter(s=>s.messages>0);
  if(!valid.length){area.innerHTML='';return;}
  let h='<div class="sessions-title">過去のセッション</div>';
  valid.forEach(s=>{
    const phaseText=s.phase==='done'?'完了':s.phase||'不明';
    h+=`<div class="session-item" onclick="resumeSession('${s.id}')">
      <div class="session-meta">
        <div class="id">${s.id}</div>
        <div class="topic">${s.topic||'(お題なし)'}</div>
      </div>
      <div class="session-stats">${s.messages}件 / v${s.mockups} / ${phaseText}</div>
      <button class="session-resume-btn" onclick="event.stopPropagation();resumeSession('${s.id}')">続きから</button>
    </div>`;
  });
  area.innerHTML=h;
}
loadSessions();

async function resumeSession(sessionId){
  const topic=document.getElementById('topicInput').value.trim();
  const btn=document.getElementById('startBtn');
  btn.disabled=true;
  btn.textContent='再開中...';
  const resp=await fetch('/api/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sessionId,topic:topic})});
  if(resp.ok){
    switchToMeeting();
  }else{
    btn.disabled=false;
    btn.textContent='新しい会議をスタート';
  }
}

async function startMeeting(){
  const input=document.getElementById('topicInput');
  const btn=document.getElementById('startBtn');
  const topic=input.value.trim();
  if(!topic)return input.focus();

  btn.disabled=true;
  btn.textContent='起動中...';

  const resp=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({topic})});
  if(resp.ok){
    switchToMeeting();
  }else{
    btn.disabled=false;
    btn.textContent='会議をスタート';
  }
}

function switchToMeeting(){
  meetingStarted=true;
  startTime=Date.now();
  renderedCount=0;
  lastHashes={};
  currentMockVer=0;
  document.getElementById('startScreen').style.display='none';
  const ms=document.getElementById('meetingScreen');
  ms.style.display='flex';
  setInterval(poll,800);
  poll();
}

// Enter key does NOT start - button only
document.getElementById('topicInput').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();}
});

const container=document.getElementById('timelineContainer');
container.addEventListener('scroll',()=>{
  autoScroll=container.scrollHeight-container.scrollTop-container.clientHeight<80;
});

// User message input
async function sendUserMessage(){
  const input=document.getElementById('userInput');
  const btn=document.getElementById('userSendBtn');
  const msg=input.value.trim();
  if(!msg)return input.focus();
  btn.disabled=true;
  input.disabled=true;
  await fetch('/api/user_message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
  input.value='';
  btn.disabled=false;
  input.disabled=false;
  input.focus();
}
document.getElementById('userInput').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&e.shiftKey){e.preventDefault();sendUserMessage();}
});

const PHASE_LABELS_BASE={waiting:'準備中',hearing:'ヒアリング',ideation:'アイデア出し',direction:'方針決定',building:'ビルド中',decision:'最終判定',done:'完了'};
function phaseLabel(p){
  if(PHASE_LABELS_BASE[p])return PHASE_LABELS_BASE[p];
  const m=p.match(/(review|feedback|improving)(\d+)/);
  if(m){const n=m[2];if(m[1]==='review')return'レビュー'+n;if(m[1]==='feedback')return'FB整理'+n;if(m[1]==='improving')return'改善'+n;}
  return p;
}

function hash(s){let h=0;for(let i=0;i<s.length;i++)h=((h<<5)-h+s.charCodeAt(i))|0;return h}

const CODE_PLACEHOLDER='__CODE_BLOCK__';
const CODE_BADGE='<div style="padding:8px 12px;background:#34D39915;border:1px solid #34D39933;border-radius:8px;margin:8px 0;font-size:12px;color:#34D399">📦 HTMLコード出力済み → 右のプレビューに反映</div>';

function md(t){
  // エンジニアのHTMLコードを除去してからエスケープ
  t=t.replace(/---HTML_START---[\s\S]*?---HTML_END---/g,CODE_PLACEHOLDER);
  t=t.replace(/```html\s*<!DOCTYPE[\s\S]*?```/gi,CODE_PLACEHOLDER);
  // ストリーミング中に途中まで出ているHTMLコードも非表示
  t=t.replace(/---HTML_START---[\s\S]*/g,CODE_PLACEHOLDER);
  return t
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/```(\w*)\n([\s\S]*?)```/g,'<pre><code>$2</code></pre>')
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/^### (.+)$/gm,'<h3>$1</h3>')
    .replace(/^## (.+)$/gm,'<h2>$1</h2>')
    .replace(/^# (.+)$/gm,'<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/^---$/gm,'<hr>')
    .replace(/^\|(.+)\|$/gm,m=>{
      const c=m.split('|').filter(x=>x.trim());
      if(c.every(x=>/^[\s:-]+$/.test(x)))return '';
      return '<tr>'+c.map(x=>'<td>'+x.trim()+'</td>').join('')+'</tr>';
    })
    .replace(/((?:<tr>.*<\/tr>\s*)+)/g,'<table>$1</table>')
    .replace(/^- (.+)$/gm,'<li>$1</li>')
    .replace(/((?:<li>.*<\/li>\s*)+)/g,'<ul>$1</ul>')
    .replace(/__CODE_BLOCK__/g,CODE_BADGE);
}

function renderMsg(m){
  const f=m.type==='facilitator';
  const u=m.type==='user';
  const e=m.type==='error';
  return `<div class="msg ${f?'facilitator':''} ${u?'user-msg':''} ${e?'error-msg':''}" id="msg-${renderedCount}">
    <div class="msg-avatar" style="background:${m.color}18;border:2px solid ${m.color}44">${m.icon}</div>
    <div class="msg-body">
      <div class="msg-header">
        <div class="msg-name" style="color:${m.color}">${m.label}</div>
        ${f?'<span class="msg-badge">ファシリ</span>':''}
      </div>
      <div class="msg-content ${m.streaming?'streaming-cursor':''}">${md(m.content)}</div>
    </div>
  </div>`;
}

let _lastStatus='';
async function poll(){
  try{
    const r=await fetch('/api/meeting');
    const d=await r.json();
    const phase=d.status.phase,speaker=d.status.current_speaker,mockVer=d.status.mockup_version;

    // Status bar
    const sk=phase+'|'+speaker;
    if(sk!==_lastStatus){
      _lastStatus=sk;
      let h='';
      const mainPhases=['waiting','hearing','ideation','direction','building'];
      mainPhases.forEach(k=>{h+=`<span class="phase-badge ${k===phase?'active':''}">${PHASE_LABELS_BASE[k]}</span>`;});
      // Dynamic cycle phase
      const cycleMatch=phase.match(/(review|feedback|improving)(\d+)/);
      if(cycleMatch)h+=`<span class="phase-badge active">${phaseLabel(phase)}</span>`;
      else h+=`<span class="phase-badge ${phase==='decision'?'active':''}">最終判定</span>`;
      h+=`<span class="phase-badge ${phase==='done'?'active':''}">完了</span>`;
      h+='<span style="margin-left:auto"></span>';
      for(const[id,info]of Object.entries(AGENTS)){
        h+=`<span class="status-item"><span class="status-dot ${speaker===id?'speaking':''}"></span>${info.icon}${info.label}</span>`;
      }
      document.getElementById('statusBar').innerHTML=h;
    }

    // Timeline
    const msgs=d.messages,tl=document.getElementById('timeline');
    for(let i=renderedCount;i<msgs.length;i++){
      const w=document.createElement('div');
      w.innerHTML=renderMsg(msgs[i]);
      tl.appendChild(w.firstElementChild);
      renderedCount++;
    }
    msgs.forEach((m,i)=>{
      const h2=hash(m.content)+(m.streaming?1:0);
      if(lastHashes[i]!==h2){
        lastHashes[i]=h2;
        const el=tl.children[i];
        if(el){const c=el.querySelector('.msg-content');if(c){c.innerHTML=md(m.content);if(m.streaming)c.classList.add('streaming-cursor');else c.classList.remove('streaming-cursor');}}
      }
    });
    if(autoScroll)container.scrollTop=container.scrollHeight;

    // Preview
    if(mockVer>0&&mockVer!==currentMockVer){
      currentMockVer=mockVer;
      document.getElementById('verBadge').textContent='v'+mockVer;
      document.getElementById('previewVer').textContent='v'+mockVer;
      const wrap=document.getElementById('previewWrap');
      wrap.innerHTML=`<iframe class="preview-frame" src="/mockup/v${mockVer}"></iframe>`;
    }

    document.getElementById('msgCount').textContent='Messages: '+msgs.length;

    // Show/hide waiting badge
    const wb=document.getElementById('waitingBadge');
    if(d.status.waiting_for_user){wb.style.display='';document.getElementById('userInput').placeholder='PMの質問に回答してください...';}
    else{wb.style.display='none';document.getElementById('userInput').placeholder='補足情報や指示を入力...';}

    // Always show restart button during meeting
    document.getElementById('restartBtn').style.display='';
  }catch(e){}

  const el=Math.floor((Date.now()-startTime)/1000);
  const phase=_lastStatus.split('|')[0]||'';
  const speaker=_lastStatus.split('|')[1]||'';
  const speakerInfo=speaker&&AGENTS[speaker]?`${AGENTS[speaker].icon} ${AGENTS[speaker].label} 応答中...`:'';
  document.getElementById('elapsed').textContent=`Elapsed: ${Math.floor(el/60)}:${String(el%60).padStart(2,'0')}${speakerInfo?' | '+speakerInfo:''}`;
}
// Console
let consoleOpen=false;
function toggleConsole(){
  consoleOpen=!consoleOpen;
  document.getElementById('consolePanel').classList.toggle('open',consoleOpen);
  document.getElementById('consoleArrow').textContent=consoleOpen?'▼':'▶';
}
let prevLogCount=0;
async function pollLogs(){
  if(!consoleOpen)return;
  try{
    const r=await fetch('/api/logs');
    const lines=await r.json();
    if(lines.length!==prevLogCount){
      prevLogCount=lines.length;
      const cp=document.getElementById('consolePanel');
      cp.innerHTML=lines.map(l=>{
        const cls=l.includes('✗')||l.includes('エラー')||l.includes('Error')?'log-error':l.includes('完了')||l.includes('OK')?'log-ok':'';
        return `<div class="${cls}">${l.replace(/</g,'&lt;')}</div>`;
      }).join('');
      cp.scrollTop=cp.scrollHeight;
    }
  }catch(e){}
}
setInterval(pollLogs,1500);

// poll is started by switchToMeeting()
</script>
</body>
</html>"""


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            aj = {k: {"label": v["label"], "color": v["color"], "icon": v["icon"]} for k, v in AGENTS.items()}
            html = HTML_TEMPLATE.replace("__AGENTS_JSON__", json.dumps(aj))
            self.wfile.write(html.encode("utf-8"))
        elif self.path == "/api/meeting":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            with meeting_lock:
                data = {"messages": list(meeting_log), "status": dict(meeting_status)}
            self.wfile.write(json.dumps(data).encode("utf-8"))
        elif self.path == "/api/logs":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(_log_lines).encode("utf-8"))
        elif self.path.startswith("/mockup/v"):
            version = self.path.split("/v")[-1]
            path = MOCK_DIR / f"mockup_v{version}.html"
            if path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(path.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path == "/api/sessions":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            sessions = []
            if SESSIONS_DIR.exists():
                for d in sorted(SESSIONS_DIR.iterdir(), reverse=True):
                    if d.is_dir():
                        log_file = d / "meeting_log.json"
                        topic_file = d / "topic.txt"
                        topic_text = topic_file.read_text(encoding="utf-8").strip()[:80] if topic_file.exists() else ""
                        msg_count = 0
                        mockup_count = 0
                        phase = ""
                        if log_file.exists():
                            try:
                                ld = json.loads(log_file.read_text(encoding="utf-8"))
                                msg_count = len(ld.get("messages", []))
                                phase = ld.get("status", {}).get("phase", "")
                            except: pass
                        mockup_dir = d / "mockups"
                        if mockup_dir.exists():
                            mockup_count = len(list(mockup_dir.glob("*.html")))
                        sessions.append({
                            "id": d.name,
                            "topic": topic_text,
                            "messages": msg_count,
                            "mockups": mockup_count,
                            "phase": phase,
                        })
            self.wfile.write(json.dumps(sessions).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global user_input_text
        if self.path == "/api/start":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
            topic = data.get("topic", "").strip()

            if not topic:
                self.send_response(400)
                self.end_headers()
                return

            # Reset state
            with meeting_lock:
                meeting_log.clear()
                meeting_status["phase"] = "waiting"
                meeting_status["current_speaker"] = ""
                meeting_status["mockup_version"] = 0
                meeting_status["waiting_for_user"] = False
            user_input_event.clear()
            user_input_text = ""

            # 前回のログを保存してから新セッション作成
            if SESSION_DIR and meeting_log:
                save_session_log()
            create_session()

            # Start meeting
            t = threading.Thread(target=run_meeting, args=(topic,), daemon=True)
            t.start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

        elif self.path == "/api/user_message":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
            message = data.get("message", "").strip()

            if not message:
                self.send_response(400)
                self.end_headers()
                return

            # ログに追加
            add_user_message(message)

            # 入力待ち状態なら解除
            if meeting_status.get("waiting_for_user"):
                user_input_text = message
                user_input_event.set()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

        elif self.path == "/api/resume":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
            session_id = data.get("session_id", "").strip()
            new_topic = data.get("topic", "").strip()

            old_session = SESSIONS_DIR / session_id

            if not old_session.exists():
                self.send_response(404)
                self.end_headers()
                return

            # 前回のログを読み込み（なければ空）
            log_file = old_session / "meeting_log.json"
            old_messages = []
            old_mockup_ver = 0
            if log_file.exists():
                old_data = json.loads(log_file.read_text(encoding="utf-8"))
                old_messages = old_data.get("messages", [])
                old_mockup_ver = old_data.get("status", {}).get("mockup_version", 0)

            # ログがなくてもモックアップがあればバージョンを検出
            old_mockup_dir = old_session / "mockups"
            if old_mockup_ver == 0 and old_mockup_dir.exists():
                versions = [int(f.stem.split("_v")[-1]) for f in old_mockup_dir.glob("mockup_v*.html")]
                if versions:
                    old_mockup_ver = max(versions)

            # 新セッション作成
            create_session()

            # 前回のモックアップをコピー
            old_mockup_dir = old_session / "mockups"
            if old_mockup_dir.exists():
                for f in old_mockup_dir.glob("*.html"):
                    import shutil
                    shutil.copy2(f, MOCK_DIR / f.name)

            # ログを復元
            with meeting_lock:
                meeting_log.clear()
                meeting_log.extend(old_messages)
                meeting_status["phase"] = "waiting"
                meeting_status["current_speaker"] = ""
                meeting_status["mockup_version"] = old_mockup_ver
                meeting_status["waiting_for_user"] = False

            # 「前回の続き」として会議再開
            # 前回の会話コンテキスト + 新しい指示で再開
            resume_topic = new_topic if new_topic else "前回の議論を踏まえて改善を続けてください"

            add_message("04_pm", f"前回のセッション ({session_id}) から再開します。\n\n**指示：{resume_topic}**", "facilitator")

            t = threading.Thread(target=run_meeting, args=(resume_topic,), daemon=True)
            t.start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def create_session():
    """タイムスタンプ付きのセッションフォルダを作成"""
    global SESSION_DIR, MOCK_DIR
    ts = time.strftime("%Y%m%d_%H%M%S")
    SESSION_DIR = SESSIONS_DIR / ts
    MOCK_DIR = SESSION_DIR / "mockups"
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    MOCK_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR


def save_session_log():
    """会議ログをセッションフォルダに保存"""
    if not SESSION_DIR:
        return
    # 会話ログ（JSON）
    with meeting_lock:
        log_data = {
            "status": dict(meeting_status),
            "messages": list(meeting_log),
        }
    (SESSION_DIR / "meeting_log.json").write_text(
        json.dumps(log_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 会話ログ（読みやすいテキスト）
    lines = []
    for msg in log_data["messages"]:
        lines.append(f"--- {msg['label']} ({msg['type']}) ---")
        lines.append(msg["content"])
        lines.append("")
    (SESSION_DIR / "meeting_log.txt").write_text("\n".join(lines), encoding="utf-8")

    # お題
    topic_msgs = [m for m in log_data["messages"] if m["type"] == "facilitator"]
    if topic_msgs:
        (SESSION_DIR / "topic.txt").write_text(topic_msgs[0]["content"], encoding="utf-8")

    log(f"セッションログ保存: {SESSION_DIR}")


def main():
    global _log_file
    session_dir = create_session()

    # サーバーログもセッションフォルダに
    _log_file = open(session_dir / "server.log", "w", encoding="utf-8")

    # CLIから直接お題を渡された場合は即開始
    topic = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else None

    print(f"{'='*50}")
    print(f"  Agent Meeting + Live Build")
    print(f"{'='*50}")
    print(f"  ダッシュボード: http://localhost:{PORT}")
    print(f"  セッション: {session_dir}")
    print(f"{'='*50}\n")

    if topic:
        meeting_thread = threading.Thread(target=run_meeting, args=(topic,), daemon=True)
        meeting_thread.start()
        print(f"  お題: {topic}\n")

    server = HTTPServer(("localhost", PORT), DashboardHandler)
    webbrowser.open(f"http://localhost:{PORT}")
    print(f"  Ctrl+C で終了\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します。ログを保存中...")
        save_session_log()
        server.shutdown()


if __name__ == "__main__":
    main()
