"""
air-curiosity-01 仪表盘（独立进程，极简版）。

护眼黄背景 + 黑字。单页面 = 控制条（状态灯 + 开始/结束）+ 左对话框 + 右上元认知自省记录 + 右下短期记忆。

启动：python dashboard.py（独立进程，端口 9878）。页面点「开始」拉起 main.py（大脑），
「结束」写 stop.flag——大脑当前 tick 完整做完才退出（优雅停止，不直接断开）。

API：
  GET  /            → 页面
  GET  /api/status  → 大脑状态（data/status.json）
  GET  /api/state   → 元认知自省记录（memory.json）+ 短期记忆快照（status.json）
  GET  /api/chat_history → 完整对话记录（创建者来信 + 她的话，按时间排序）
  POST /api/send    → 创建者发信（写 inbox）
  POST /api/start   → 拉起大脑（main.py 子进程）
  POST /api/stop    → 写 stop.flag（优雅停止）
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜常量 —— 路径 / 拉起大脑的 Python / 配色 + I18N 文案表 + 内嵌前端页面 PAGE_TPL 模板
#   段 2｜class Dashboard —— HTTP 服务（纯 http.server）：
#       读状态与聊天/自省、判断大脑是否真在跑（内核锁）、收发消息、启停大脑
#   段 3｜顶层 —— _port_in_use / main()：仪表盘单实例锁 + 起 http.server
# =====================================================================
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("AIR2_DATA_ROOT", str(ROOT / "data")))
sys.path.insert(0, str(ROOT))   # 先插 sys.path 才能 import core
from core.lock import InstanceLock, read_text_retry, atomic_write_json  # noqa: E402
from config import Config  # noqa: E402
STATUS_FILE = DATA / "status.json"
MEMORY_FILE = DATA / "memory.json"
SETTINGS_FILE = DATA / "settings.json"
STOP_FILE = DATA / "stop.flag"
# 拉起大脑用的 Python：用 pythonw（无控制台窗口，不弹黑框）；可用 AIR2_PYTHON 覆盖。
# 默认按项目根推导，不硬编码盘符——换机器/换盘直接用，代码里也不留私人路径。
PYTHON = os.getenv("AIR2_PYTHON", str(ROOT / ".venv" / "Scripts" / "pythonw.exe"))
MAIN = ROOT / "main.py"
PORT = int(os.getenv("AIR2_DASH_PORT", "9878"))

BG = "#fff8e7"      # 护眼黄
BG_PANEL = "#fffbee"  # 面板略浅
FG = "#222222"      # 黑字

# ---- 界面文案（zh / en）----
# 仪表盘语言跟随 AIR2_LANG：每次进页面都重读，改完刷新即可（不用重启仪表盘）。
# 她的说话语言也跟它走，但那个要重启大脑才生效——config 只在她启动时加载一次。
# 同一份表既给 Python 渲染静态文案用，也注入 JS 给动态文案（状态栏/报错）用，
# 避免前后端各写一套、改一处忘一处。
I18N = {
    "zh": {
        "title": "air · 仪表盘",
        "checking": "检查中…",
        "btn_start": "开始",
        "btn_stop": "结束",
        "btn_settings": "设置",
        "btn_save": "保存",
        "btn_send": "发送",
        "settings_note": "改完要重启她才生效",
        "input_ph": "给 air 留言…",
        "panel_meta": "元认知自省记录",
        "panel_short": "短期记忆",
        "empty": "（空）",
        "tick_base": 10000,      # 万进制：12000 显示「1.2万」
        "tick_unit": "万",
        "mem": " · 记忆 L1 {l1}条 L2 {l2}条 L3 {l3}条",
        "halt": "脑子掉线 · {halt}（她原地挂机，等服务恢复；tick 停在 {tick}）· {ts} · 她 PID {pid}",
        "stopping": "停止中 · 等她这一轮做完（已等 {n} 秒，做完自动停）",
        "alive": "活着 · tick {tick} · {activity}{mem} · {ts} · 她 PID {her} ／ 仪表盘 PID {dash}",
        "waking": "正在醒来…（已等 {n} 秒）",
        "resting": "休息中 · 仪表盘 PID {dash}",
        "start_err": "她没醒过来。air.log 最后几行：",
        "meta_resolved": "（已解决）",
        "meta_abandoned": "（放弃）",
        "meta_type": "自省",
        "saved_ok": "已保存——重启她之后生效。",
        "save_fail": "保存失败：",
        "hint_saved": "（已设置）",
        "hint_effective": "当前生效：",
        "hint_secret_saved": "（已设置，不回显）",
        "hint_secret_env": "（环境变量/默认值中已设置，不回显）",
        "hint_unset": "（未设置）",
        "launch_fail": "启动失败",
    },
    "en": {
        "title": "air · dashboard",
        "checking": "checking…",
        "btn_start": "Start",
        "btn_stop": "Stop",
        "btn_settings": "Settings",
        "btn_save": "Save",
        "btn_send": "Send",
        "settings_note": "changes take effect after restarting her",
        "input_ph": "leave a message for air…",
        "panel_meta": "Metacognition log",
        "panel_short": "Short-term memory",
        "empty": "(empty)",
        "tick_base": 1000,       # 千进制：12000 显示「12k」（英文没有「万」这个单位）
        "tick_unit": "k",
        "mem": " · memory L1 {l1} L2 {l2} L3 {l3}",
        "halt": "Mind offline · {halt} (she is idle, waiting for the service; "
                "tick stalled at {tick}) · {ts} · her PID {pid}",
        "stopping": "Stopping · waiting for her to finish this round "
                    "(waited {n}s, she stops by herself)",
        "alive": "Alive · tick {tick} · {activity}{mem} · {ts} · her PID {her}"
                 " / dashboard PID {dash}",
        "waking": "Waking up… (waited {n}s)",
        "resting": "Resting · dashboard PID {dash}",
        "start_err": "She did not wake up. Last lines of air.log:",
        "meta_resolved": "(resolved)",
        "meta_abandoned": "(abandoned)",
        "meta_type": "reflection",
        "saved_ok": "Saved — takes effect after restarting her.",
        "save_fail": "Save failed: ",
        "hint_saved": "(set)",
        "hint_effective": "in effect: ",
        "hint_secret_saved": "(set, hidden)",
        "hint_secret_env": "(set via environment/default, hidden)",
        "hint_unset": "(not set)",
        "launch_fail": "failed to start",
    },
}


def _render_page() -> str:
    """按当前 AIR2_LANG 渲染页面。读不到就回落中文（语言只是显示层，不该挡住她）。"""
    try:
        lang = Config().lang
    except Exception:
        lang = "zh"
    T = I18N.get(lang) or I18N["zh"]
    return PAGE_TPL.format(T_JSON=json.dumps(T, ensure_ascii=False),
                           LANG=lang, BG=BG, BG_PANEL=BG_PANEL, FG=FG, **T)


PAGE_TPL = """<!doctype html>
<html lang="{LANG}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: "Microsoft YaHei", "Segoe UI", sans-serif; margin: 0;
          background: {BG}; color: {FG}; height: 100vh; display: flex; flex-direction: column; }}
  #ctrl {{ display: flex; align-items: center; gap: 12px; padding: 10px 16px;
           border-bottom: 1px solid #d8c9a0; background: {BG_PANEL}; }}
  #dot {{ width: 14px; height: 14px; border-radius: 50%; background: #aaa; }}
  #dot.on {{ background: #3a9a3a; }}
  #dot.stop {{ background: #c9a227; }}
  #dot.halt {{ background: #c0392b; }}
  button {{ padding: 6px 18px; border: 1px solid #b8a878; border-radius: 6px;
            background: {BG_PANEL}; color: {FG}; font-size: 14px; cursor: pointer; }}
  button:hover {{ background: #f5e9c8; }}
  #statusText {{ font-size: 14px; }}
  #startErr {{ display: none; padding: 8px 16px; background: #ffe9e0; color: #8a3b1e;
               font-size: 12px; white-space: pre-wrap; border-bottom: 1px solid #e0c0a8;
               font-family: Consolas, "Courier New", monospace; }}
  #main {{ flex: 1; display: flex; min-height: 0; }}
  #chat {{ flex: 1.4; display: flex; flex-direction: column; border-right: 1px solid #d8c9a0; min-width: 0; }}
  #right {{ flex: 1; display: flex; flex-direction: column; min-width: 0; }}
  .panel {{ flex: 1; display: flex; flex-direction: column; min-height: 0; padding: 10px 14px; }}
  .panel h2 {{ font-size: 14px; margin: 0 0 6px; color: #6b5d3a; }}
  .panel h2 .ts {{ font-size: 11px; color: #9a8c6a; font-weight: normal; }}
  #log {{ flex: 1; overflow-y: auto; padding: 8px 12px; font-size: 14px; }}
  .owner {{ text-align: right; margin: 6px 0; }}
  .her {{ text-align: left; margin: 6px 0; }}
  .owner span {{ background: #f0e6c8; padding: 8px 12px; border-radius: 10px; display: inline-block; max-width: 75%; }}
  .her span {{ background: #fff3d6; padding: 8px 12px; border-radius: 10px; display: inline-block; max-width: 75%; white-space: pre-wrap; }}
  .sys {{ text-align: center; color: #9a8c6a; font-size: 11px; margin: 4px 0; }}
  #box {{ display: flex; gap: 8px; padding: 10px 12px; }}
  #input {{ flex: 1; padding: 8px 10px; border: 1px solid #c8b88a; border-radius: 6px;
            font-size: 14px; background: {BG_PANEL}; color: {FG}; }}
  #meta_log, #short {{ flex: 1; overflow-y: auto; background: {BG_PANEL}; border: 1px solid #e0d4b0;
            border-radius: 6px; padding: 8px 10px; font-size: 13px; line-height: 1.6; white-space: pre-wrap; }}
  .empty {{ color: #9a8c6a; }}
</style>
</head>
<body>
  <div id="ctrl">
    <div id="dot"></div><span id="statusText">{checking}</span>
    <button id="btnStart" onclick="doStart()">{btn_start}</button>
    <button id="btnStop" onclick="doStop()">{btn_stop}</button>
    <button onclick="toggleSettings()">{btn_settings}</button>
  </div>
  <div id="startErr"></div>
  <div id="settings" style="display:none">
    <div class="panel">
      <h2>{btn_settings} <span class="ts">{settings_note}</span></h2>
      <div id="settingsForm"></div>
      <div><button onclick="saveSettings()">{btn_save}</button></div>
      <div id="settingsMsg" class="ts"></div>
    </div>
  </div>
  <div id="main">
    <div id="chat">
      <div id="log"></div>
      <div id="box">
        <input id="input" placeholder="{input_ph}" autocomplete="off">
        <button onclick="send()">{btn_send}</button>
      </div>
    </div>
    <div id="right">
      <div class="panel"><h2>{panel_meta} <span class="ts" id="memTs"></span></h2><div id="meta_log" class="empty">{empty}</div></div>
      <div class="panel"><h2>{panel_short} <span class="ts" id="shortTs"></span></h2><div id="short" class="empty">{empty}</div></div>
    </div>
  </div>
<script>
// 文案表由后端按 AIR2_LANG 注入，和页面上静态文案是同一份（见 dashboard.I18N）。
// tpl：把文案里的占位符（花括号包名字）换成具体值——语序交给文案自己定，中英文各写各的。
const T = {T_JSON};
function tpl(s, kv) {{ let r = s; for (const k in kv) r = r.split('{{'+k+'}}').join(kv[k]); return r; }}
let chatRendered = 0;
let stopAt = 0;    // 点「结束」的时刻（0 = 没在等停止）
let startAt = 0;   // 点「开始」的时刻（0 = 没在等她醒）
// tick 计数无限累加（她不会停），显示时缩写，避免一长串数字。
// 进制随语言走：中文万进制（1.2万），英文千进制（12k）——「万」在英文里没有对应单位。
function fmtTick(n) {{
  n = n || 0;
  const base = T.tick_base, unit = T.tick_unit;
  if (n >= base) {{
    const v = n / base;
    return (v % 1 === 0 ? String(v) : v.toFixed(1)) + unit;
  }}
  return String(n);
}}
// 所有请求都带这个头：后端据此拒掉跨站请求。跨站的 CORS simple request 带不上
// 自定义头，一带上就触发预检，而服务端不回应预检 → 浏览器不会发真请求。
// 防的是「创建者浏览的任意网页都能停她、启她、伪造『创建者来信』」，见 Dashboard._local_only。
const AIR_HDR = {{ 'X-Air-Local': '1' }};
async function pollStatus() {{
  try {{
    const s = await (await fetch('/api/status', {{ headers: AIR_HDR }})).json();
    const dot = document.getElementById('dot');
    let mem = '';
    if (s.counts) {{
      const c = s.counts;
      const n = x => String(x || '0').split('/')[0];
      mem = tpl(T.mem, {{l1: n(c.L1), l2: n(c.L2), l3: n(c.L3)}});
    }}
    let txt;
    if (s.alive && s.halt) {{
      // 进程活着、语义服务挂了：她原地挂机（刀断了不切菜），tick 不推进。
      // 灯用红的——继续说「活着」会让人以为什么都没发生，其实她已经停摆了
      dot.className = 'halt';
      stopAt = 0; startAt = 0;
      txt = tpl(T.halt, {{halt: s.halt, tick: fmtTick(s.last_tick),
                          ts: (s.ts||'').slice(5,16), pid: (s.brain_pid || '?')}});
    }} else if (s.alive && s.stop_pending) {{
      // 停止信号已下、大脑还活着 = 她正在做当前这一轮（自然收尾），把等待时长摆出来。
      // 不显示的话创建者只看到绿点，会以为「结束」没生效、反复去点（2026-09-03 接线修正：
      // 原分支在 alive 之后，而停止等待期大脑其实还活着，永远走不到这里）
      dot.className = 'stop';
      if (!stopAt) stopAt = Date.now();
      txt = tpl(T.stopping, {{n: Math.floor((Date.now()-stopAt)/1000)}});
    }} else if (s.alive) {{
      dot.className = 'on';
      stopAt = 0; startAt = 0;
      txt = tpl(T.alive, {{tick: fmtTick(s.last_tick), activity: (s.activity || ''), mem: mem,
                           ts: (s.ts||'').slice(5,16), her: (s.brain_pid || '?'),
                           dash: (s.dash_pid || '?')}});
    }} else if (startAt || (s.last_launch && (Date.now()/1000 - s.last_launch) < 60)) {{
      // 点过「开始」或后端记得刚拉起过（刷新页面后 startAt 会丢，用 last_launch 兜底）：
      // 显示「正在醒来」而不是「休息中」，也不会把启动中的几秒误报成启动失败
      dot.className = 'stop';
      const base = startAt ? startAt : (s.last_launch * 1000);
      txt = tpl(T.waking, {{n: Math.floor((Date.now()-base)/1000)}});
      if (Date.now() - base > 60000) startAt = 0;   // 一分钟还没起来就别空等，看错误提示
    }} else {{
      dot.className = '';
      stopAt = 0;
      // 把进程号摆出来：任务管理器里一次启动就有两个 pythonw（venv 父子两层），
      // 看着像双份，其实只有一份在干活——这里给的就是干活那个
      txt = tpl(T.resting, {{dash: (s.dash_pid || '?')}});
    }}
    document.getElementById('statusText').textContent = txt;
    // 启动失败时把 air.log 尾巴摆出来：pythonw 无窗口，她崩了的话只有日志知道
    const box = document.getElementById('startErr');
    if (s.start_error) {{
      box.textContent = T.start_err + '\\n' + s.start_error;
      box.style.display = '';
    }} else box.style.display = 'none';
    document.getElementById('btnStart').style.display = s.alive ? 'none' : '';
    document.getElementById('btnStop').style.display = (s.alive || s.stop_pending) ? '' : 'none';
  }} catch(e) {{}}
}}
async function pollState() {{
  try {{
    const st = await (await fetch('/api/state', {{ headers: AIR_HDR }})).json();
    const mem = document.getElementById('meta_log');
    const short = document.getElementById('short');
    if (st.meta_log && st.meta_log.length) {{
      // 贴底才跟随滚动（含首次渲染），创建者翻历史时不打扰：
      // 更新前记 scrollTop/clientHeight，若原本贴底或首次 → 渲染后滚到最新（新条目在末尾）
      const stick = !mem.dataset.seen ||
          (mem.scrollHeight - mem.scrollTop - mem.clientHeight < 40);
      mem.className = '';
      mem.textContent = st.meta_log.map(m => {{
        const label = m.status === 'resolved' ? T.meta_resolved
                    : (m.status === 'abandoned' ? T.meta_abandoned : '');
        return '· [' + (m.type || T.meta_type) + '] ' + (m.text || m.content || '') + label;
      }}).join('\\n');
      mem.dataset.seen = '1';
      if (stick) mem.scrollTop = mem.scrollHeight;
      document.getElementById('memTs').textContent = st.mem_ts || '';
    }}
    if (st.narrative) {{
      short.className = '';
      short.textContent = st.narrative;
      document.getElementById('shortTs').textContent = st.short_ts || '';
    }}
  }} catch(e) {{}}
}}
async function pollChat() {{
  try {{
    const d = await (await fetch('/api/chat_history', {{ headers: AIR_HDR }})).json();
    const items = d.items || [];
    for (let i = chatRendered; i < items.length; i++) append(items[i].who, items[i].text);
    chatRendered = items.length;
  }} catch(e) {{}}
}}
function append(who, text) {{
  const log = document.getElementById('log');
  const div = document.createElement('div');
  div.className = who;
  const span = document.createElement('span');
  span.textContent = text;
  div.appendChild(span);
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}}
async function send() {{
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  await fetch('/api/send', {{ method: 'POST',
    headers: {{ 'Content-Type': 'application/json', 'X-Air-Local': '1' }},
    body: JSON.stringify({{text}}) }});
  pollChat();   // 立即刷新，显示创建者刚发的
}}
// 输入框按 Enter 发送（isComposing 排除中文输入法选词时的回车，避免打中文误发）
document.getElementById('input').addEventListener('keydown', function(e) {{
  if (e.key === 'Enter' && !e.isComposing) {{ e.preventDefault(); send(); }}
}});
async function doStart() {{
  const btn = document.getElementById('btnStart');
  btn.disabled = true;   // 防连点
  startAt = Date.now();
  const r = await fetch('/api/start', {{ method: 'POST', headers: AIR_HDR }});
  const d = await r.json();
  if (d.ok === false) {{
    startAt = 0;
    document.getElementById('statusText').textContent = d.err || T.launch_fail;
  }}
  setTimeout(pollStatus, 1500);
  // 大脑 import numpy/torch 要好几秒，这期间锁还没握上，按钮别太快放开
  setTimeout(() => {{ btn.disabled = false; }}, 8000);
}}
async function doStop() {{
  await fetch('/api/stop', {{ method: 'POST', headers: AIR_HDR }});
  stopAt = Date.now();
  pollStatus();
}}
// ---- 设置页：改的是 data/settings.json（不入库），重启她之后生效 ----
function toggleSettings() {{
  const el = document.getElementById('settings');
  const show = (el.style.display === 'none');
  el.style.display = show ? '' : 'none';
  if (show) loadSettings();
}}
async function loadSettings() {{
  try {{
    const d = await (await fetch('/api/settings', {{ headers: AIR_HDR }})).json();
    const form = document.getElementById('settingsForm');
    form.innerHTML = '';
    Object.keys(d.keys || {{}}).forEach(function(k) {{
      const label = d.keys[k];   // 后端已按语言选好标签，直接给字符串
      const saved = (d.saved || {{}})[k] || '';
      const eff = (d.effective || {{}})[k] || '';
      const row = document.createElement('div');
      row.style.margin = '8px 0';
      const lab = document.createElement('div');
      lab.style.fontSize = '12px';
      lab.textContent = label + ' — ' + k;
      const isLong = (k === 'AIR2_CHARTER');
      const isSecret = /KEY$/.test(k);   // 密钥类字段掩码显示（防旁人瞟一眼）
      const inp = document.createElement(isLong ? 'textarea' : 'input');
      inp.id = 'set_' + k;
      // 宪章预填当前生效值：它是要逐句打磨的方向基准，从空白重写太重。
      // 普通字段保持「留空=回落默认」的语义——预填会把环境变量/默认值固化进 settings.json。
      inp.value = isLong ? (saved || eff) : saved;
      if (isSecret) inp.type = 'password';
      inp.style.width = '100%';
      inp.style.boxSizing = 'border-box';
      if (isLong) inp.rows = 12;
      const hint = document.createElement('div');
      hint.className = 'ts';
      // 密钥一律不回显：输入框掩码了、底下这行又明文打出来，等于白设。
      if (isSecret) {{
        hint.textContent = saved ? T.hint_secret_saved
          : (eff ? T.hint_secret_env : T.hint_unset);
      }} else {{
        hint.textContent = saved ? T.hint_saved
          : (T.hint_effective + (eff.length > 70 ? eff.slice(0, 70) + '…' : eff));
      }}
      row.appendChild(lab); row.appendChild(inp); row.appendChild(hint);
      form.appendChild(row);
    }});
  }} catch(e) {{}}
}}
async function saveSettings() {{
  const payload = {{}};
  document.querySelectorAll('[id^="set_"]').forEach(function(el) {{
    payload[el.id.slice(4)] = el.value;
  }});
  try {{
    const r = await fetch('/api/settings', {{ method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'X-Air-Local': '1' }},
      body: JSON.stringify(payload) }});
    const d = await r.json();
    document.getElementById('settingsMsg').textContent =
      d.ok ? T.saved_ok : (T.save_fail + (d.err || ''));
  }} catch(e) {{
    document.getElementById('settingsMsg').textContent = T.save_fail + e;
  }}
}}
setInterval(pollStatus, 3000);
setInterval(pollChat, 3000);
setInterval(pollState, 5000);
pollStatus(); pollChat(); pollState();
</script>
</body>
</html>"""


class Dashboard(BaseHTTPRequestHandler):
    _last_launch = 0.0          # 上次拉起大脑的时刻（进程内共享，防手抖连点）
    LAUNCH_GRACE_SEC = 30       # 拉起后这段时间内不再 Popen：大脑要 import numpy/torch，
                                # 几秒内还没拿到锁是正常的，别在这窗口里再拉一个

    # 本机仪表盘也要防跨站：绑 127.0.0.1 只挡住了外网直连，挡不住创建者浏览器里的
    # 任意网页——它能往 127.0.0.1:9878 发跨站请求。而 /api/stop、/api/start 都不读
    # 请求体，属于 CORS simple request，不触发预检，请求会真实执行（响应被 CORS
    # 拦掉也无妨，副作用已经发生了）。后果有两档：
    #   1. 随便一个网页都能把她停掉、拉起来；
    #   2. <form enctype="text/plain"> 提交 {"text":"..."} 同样是 simple request，
    #      能伪造「创建者来信」注入 inbox——她会把那话当真，写进记忆和决策日志。
    # 所以三道一起上，见 _local_only。
    _ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1")
    _LOCAL_HEADER = "X-Air-Local"

    # 设置页能改的项：{环境变量名: (中文说明, 英文说明, Config 上的属性名)}。
    # 用白名单而不是全量开放——那等于把 config 的几十个字段变成一个随便写文件的口子。
    # 值的优先级是 环境变量 > settings.json > 代码默认值，所以这里写的会被环境变量盖掉。
    SETTING_KEYS = {
        "AIR2_OWNER_NAME": ("她怎么称呼你", "How she addresses you", "owner_name"),
        "AIR2_LANG": ("语言 zh/en", "Language zh/en", "lang"),
        "AIR2_LLM_ENDPOINT": ("模型端点", "Model endpoint", "llm_endpoint"),
        "AIR2_LLM_MODEL": ("模型名", "Model name", "llm_model"),
        "AIR2_LLM_API_KEY": ("模型 API key", "Model API key", "llm_api_key"),
        "AIR2_CHARTER": ("宪章（她的方向基准）", "Charter (her baseline)", "charter"),
    }

    def _local_only(self, require_header: bool = True) -> bool:
        """这个请求是不是本机页面发的。不是就 403，一个字都不执行。

        三道：
        1. Host 必须落在白名单——挡 DNS rebinding（把 evil.com 解析到 127.0.0.1，
           浏览器就视作同源，能读走 /api/chat_history 的完整对话记录）；
        2. Origin 若存在必须在白名单——挡跨站读；
        3. 必须带 X-Air-Local 自定义头——跨站的 simple request 带不上自定义头，
           一带上就触发预检，服务端不回应预检，浏览器就不发真请求。

        require_header=False 时跳过第 3 道，只留前两道。**只给静态页面用**：
        浏览器地址栏直连带不上自定义头，要求它等于把页面本身也锁在门外
        （页面都打不开，页面里的 JS 也就永远没机会带上这个头）。
        放宽的风险只在 DNS rebinding 下被读走页面 HTML——那里面没有她的任何数据，
        她的记忆/对话全在 /api/* 上，那些仍然三道齐全。
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip().lower().strip("[]")
        if host not in self._ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                ohost = (urlparse(origin).hostname or "").lower()
            except Exception:
                return False
            if ohost not in self._ALLOWED_HOSTS:
                return False
        if not require_header:
            return True
        return self.headers.get(self._LOCAL_HEADER) is not None

    def log_message(self, *args):
        pass

    def _send(self, body, status=200, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_settings(self) -> dict:
        """设置页回显：已存的值 + 当前实际生效值 + 每项说明。

        给「生效值」是因为 settings.json 通常只写了少数几项，其余走代码默认值——
        只回显已存的会让人以为那些是空的。
        """
        saved = {}
        try:
            if SETTINGS_FILE.exists():
                d = json.loads(read_text_retry(SETTINGS_FILE))
                if isinstance(d, dict):
                    saved = {str(k): str(v) for k, v in d.items()}
        except Exception:
            saved = {}
        try:
            cfg = Config()
        except Exception:
            cfg = None
        effective = {k: (str(getattr(cfg, attr, "") or "") if cfg else "")
                     for k, (_zh, _en, attr) in self.SETTING_KEYS.items()}
        # 标签按当前语言给字符串（前端直接显示，不再自己取 tuple[0]）
        lang = (cfg.lang if cfg else "zh")
        keys = {k: (v[1] if lang == "en" else v[0])
                for k, v in self.SETTING_KEYS.items()}
        return {"keys": keys, "saved": saved, "effective": effective}

    def _write_settings(self, obj: dict):
        """保存设置到 data/settings.json。返回 (ok, err)。

        只收白名单里的 key；留空 = 删掉那项、回落到环境变量或代码默认值。
        改完要重启她才生效（config 在进程启动时加载一次）——前端会写清楚。
        """
        cur = {}
        try:
            if SETTINGS_FILE.exists():
                d = json.loads(read_text_retry(SETTINGS_FILE))
                if isinstance(d, dict):
                    cur = {str(k): str(v) for k, v in d.items()}
        except Exception:
            cur = {}
        for k, v in obj.items():
            if k in self.SETTING_KEYS:
                cur[k] = str(v or "").strip()
        cur = {k: v for k, v in cur.items() if v}      # 空值 = 删除该项
        try:
            atomic_write_json(SETTINGS_FILE, cur)
            return True, ""
        except Exception as e:
            return False, str(e)

    def _read_status(self) -> dict:
        if not STATUS_FILE.exists():
            return {"alive": False}
        try:
            # read_text_retry：大脑写 status.json 走 atomic_write（os.replace），Windows 上
            # 读的瞬间撞上 replace 会 WinError 5，裸 read_text 会把活着的她误判成"休息"。
            return json.loads(read_text_retry(STATUS_FILE))
        except Exception:
            return {"alive": False}

    @staticmethod
    def _tail_log(n=6) -> str:
        """air.log 最后 n 行（启动失败时给前端看原因，pythonw 无窗口，崩了谁都看不见）。"""
        try:
            p = DATA / "air.log"
            if not p.exists():
                return ""
            return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
        except Exception:
            return ""

    def _brain_running(self) -> bool:
        """大脑真在跑 = brain.pid 上的内核锁还被某个进程握着。

        旧方案是"读 PID 文件 + OpenProcess 探针"，有两个洞：PID 会被系统复用
        （拿别的进程误判成她）、以及 pid 文件残留时要特判"文件在但进程没了"。
        现在大脑持的是内核字节锁，判定退化成二值——活着一定握着锁，
        进程一死锁立刻消失，连进程探针都不需要了（core/lock.py）。
        """
        return InstanceLock(DATA / "brain.pid").held()

    def _read_chat_history(self) -> list:
        """合并 inbox（创建者）+ outbox（她）为按时间排序的完整对话记录。"""
        items = []
        for who, p in (("owner", DATA / "mailbox" / "inbox.jsonl"),
                       ("her", DATA / "mailbox" / "outbox.jsonl")):
            if not p.exists():
                continue
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                    items.append({"who": who, "text": d.get("text", ""), "ts": d.get("ts", "")})
                except Exception:
                    items.append({"who": who, "text": ln, "ts": ""})
        items.sort(key=lambda x: x.get("ts", ""))
        return items

    _meta_cache: tuple = (None, [])   # 类级缓存（mtime+size, meta_log）
    # 注意缓存放类属性而不是实例属性：ThreadingHTTPServer 每个请求都 new 一个 handler
    # 实例，放 self 上每次请求都是新的、缓存永远失效（实测会每 5 秒全量解析 memory.json）。

    def _read_meta_log(self) -> list:
        """读元认知自省记录（memory.json）。带 mtime+size 缓存：文件没变就不重新 json.loads。

        之前每 5 秒全量解析整个 memory.json（L5 只增不减，文件越来越大），
        浪费 CPU。现在只在文件真正变化时才读。
        """
        if not MEMORY_FILE.exists():
            Dashboard._meta_cache = (None, [])
            return []
        try:
            st = MEMORY_FILE.stat()
            key = (st.st_mtime_ns, st.st_size)
            if Dashboard._meta_cache and Dashboard._meta_cache[0] == key:
                return Dashboard._meta_cache[1]
            d = json.loads(read_text_retry(MEMORY_FILE))
            meta = d.get("meta_log") or []
            Dashboard._meta_cache = (key, meta)
            return meta
        except Exception:
            return []

    def do_GET(self):
        path = urlparse(self.path).path
        # 静态页面不要求自定义头（地址栏直连带不上，见 _local_only）；API 一律三道齐全。
        is_page = path in ("/", "/index.html")
        if not self._local_only(require_header=not is_page):
            self._send("{}", status=403)
            return
        if path == "/" or path == "/index.html":
            self._send(_render_page(), ctype="text/html; charset=utf-8")
        elif path == "/api/status":
            st = self._read_status()
            # 进程没了但 status 陈旧 alive=true（大脑异常退出没写休息状态）→ 修正为休息，
            # 否则前端显示"活着"、开始按钮被误判"她已经在跑"（创建者遇到的"卡住"）
            st["alive"] = bool(st.get("alive")) and self._brain_running()
            # 停止信号还挂着且大脑活着 = 她正在做当前这一轮（自然收尾），前端显示"停止中"；
            # 大脑已死却残留 stop.flag（异常退出没清）不算停止中——否则前端永远卡在等它收尾，
            # 创建者看到的是"停止中"其实她早没了（2026-09-03 接线修正）
            st["stop_pending"] = STOP_FILE.exists() and self._brain_running()
            # 进程号摆给创建者看：venv 的 pythonw.exe 是父子两层进程（父进程只是启动器），
            # 任务管理器里一次启动就出现两个 pythonw，很容易误看成"起了双份"。
            # 这里给的永远是真正干活的那个——os.getpid() 在子层，锁文件记的也是子层。
            st["dash_pid"] = os.getpid()
            st["brain_pid"] = InstanceLock(DATA / "brain.pid").pid() if st["alive"] else 0
            # 刚拉起的时间戳：刷新页面后前端 startAt 归零，靠这个判断"其实点过开始、还在等醒"，
            # 不会把启动中的几秒误显示成"休息中"+ 启动失败（创建者刷新一下就看到"她没醒过来"）。
            st["last_launch"] = Dashboard._last_launch
            # 刚拉起过却没活起来 = 多半是启动就崩了（pythonw 无窗口，错误全沉在 air.log 里）。
            # 但要给足她醒来时间（import numpy/torch 要好些秒），30 秒内先不报错；
            # 超过 60 秒还没活 = 真没起来，把日志尾巴带回去告诉创建者为什么。
            # 前端在 last_launch 窗口内显示「正在醒来」，这里只在窗口结束才给错误尾巴。
            st["start_error"] = ""
            if not st["alive"] and not st["stop_pending"] \
                    and Dashboard._last_launch and time.time() - Dashboard._last_launch > 60 \
                    and time.time() - Dashboard._last_launch < 600:
                st["start_error"] = self._tail_log(6)
            self._send(json.dumps(st, ensure_ascii=False))
        elif path == "/api/state":
            st = self._read_status()
            mem = self._read_meta_log()
            self._send(json.dumps({
                "meta_log": mem,
                "mem_ts": (mem[-1].get("updated") or mem[-1].get("created")) if mem else "",
                "narrative": st.get("narrative", ""),
                "short_ts": st.get("ts", ""),
            }, ensure_ascii=False))
        elif path == "/api/settings":
            self._send(json.dumps(self._read_settings(), ensure_ascii=False))
        elif path == "/api/chat_history":
            self._send(json.dumps({"items": self._read_chat_history()}, ensure_ascii=False))
        else:
            self._send("{}", status=404)

    def do_POST(self):
        if not self._local_only():
            self._send("{}", status=403)
            return
        path = urlparse(self.path).path
        if path == "/api/send":
            # 只收 application/json。X-Air-Local 那道已经挡住了 simple request，
            # 这里再加一层是纵深防御——伪造「创建者来信」是最不能出事的一条路。
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._send(json.dumps({"ok": False, "err": "只收 application/json"}),
                           status=415)
                return
            ln = int(self.headers.get("Content-Length", 0))
            try:
                text = (json.loads(self.rfile.read(ln).decode("utf-8")).get("text") or "").strip()
            except Exception:
                text = ""
            if text:
                inbox = DATA / "mailbox" / "inbox.jsonl"
                inbox.parent.mkdir(parents=True, exist_ok=True)
                with open(inbox, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"text": text, "ts": _now()}, ensure_ascii=False) + "\n")
            self._send(json.dumps({"ok": True}, ensure_ascii=False))
        elif path == "/api/settings":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._send(json.dumps({"ok": False, "err": "只收 application/json"}),
                           status=415)
                return
            try:
                ln = int(self.headers.get("Content-Length", 0))
                obj = json.loads(self.rfile.read(ln).decode("utf-8"))
            except Exception as e:
                self._send(json.dumps({"ok": False, "err": f"请求体不是合法 JSON：{e}"},
                                      ensure_ascii=False), status=400)
                return
            ok, err = self._write_settings(obj if isinstance(obj, dict) else {})
            self._send(json.dumps({"ok": ok, "err": err}, ensure_ascii=False))
        elif path == "/api/start":
            # 防双进程（1.0 同款教训）：以「锁是不是被握着」为准——status.alive 可能陈旧
            # （大脑异常退出没写休息状态），不能当拒因，否则卡在"她已经在跑"
            if self._brain_running():
                self._send(json.dumps({"ok": False, "err": "她已经在跑"}), status=409)
                return
            # 启动窗口：大脑起得慢（import numpy/torch 要好几秒），这期间它还没握上锁，
            # 此时再 Popen 一次就会白拉一个（虽然那个会被锁挡下，但日志里会多一次「拒绝」）
            if time.time() - Dashboard._last_launch < Dashboard.LAUNCH_GRACE_SEC:
                self._send(json.dumps({"ok": True, "note": "刚拉起过，等她醒"}, ensure_ascii=False))
                return
            try:
                STOP_FILE.unlink(missing_ok=True)   # 清旧停止信号，避免一启动就撞上停止
                # 大脑输出重定向到 data/air.log（诊断用，不会弹黑窗）
                logf = (DATA / "air.log").open("a", encoding="utf-8")
                try:
                    subprocess.Popen([PYTHON, str(MAIN)], cwd=str(ROOT), stdout=logf,
                                     stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                finally:
                    logf.close()   # 父进程不持有句柄（旧版每点一次「开始」泄漏一个）
                Dashboard._last_launch = time.time()
                self._send(json.dumps({"ok": True}, ensure_ascii=False))
            except Exception as e:
                self._send(json.dumps({"ok": False, "err": str(e)}, ensure_ascii=False))
        elif path == "/api/stop":
            # 优雅停止：写信号，大脑当前 tick 做完自己退出（不直接杀进程）
            try:
                DATA.mkdir(parents=True, exist_ok=True)
                STOP_FILE.write_text("stop", encoding="utf-8")
                self._send(json.dumps({"ok": True}, ensure_ascii=False))
            except Exception as e:
                self._send(json.dumps({"ok": False, "err": str(e)}, ensure_ascii=False))
        else:
            self._send("{}", status=404)


def _now() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M")


def _port_in_use(port) -> bool:
    """这个端口上有没有人正在监听。

    拿锁之后再看：如果端口被占但仪表盘的锁没人持着，说明占端口的是别的程序
    （或上一版留下的僵尸仪表盘）。这时别去启动——Windows 上 SO_REUSEADDR 等价于
    SO_REUSEPORT，第二个进程能静默绑上同一端口劫持连接，结果就是两个实例
    都活着、请求随机落一个，前端状态灯和按钮各说各话。宁可明确报错。
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    # 仪表盘单实例锁：内核字节锁，拿不到就是已有仪表盘在跑，直接退。
    # 旧方案是 O_EXCL 创建 dash.pid + PID 探针，有竞态窗口：os.open 成功到 os.write(pid)
    # 之间文件是空的，并发启动时第二个进程读到空文件，int("") 抛异常被 except 吞掉
    # → 判定"旧进程死了"→ 接管 → 双开（实测抓到过两个 dashboard 同一秒启动、都活着）。
    dash_lock = InstanceLock(DATA / "dash.pid")
    if not dash_lock.acquire():
        print(f"仪表盘已在运行（PID {dash_lock.pid()}），不重复启动")
        return
    try:
        if _port_in_use(PORT):
            print(f"端口 {PORT} 已被别的程序占用（仪表盘的锁没人持着，所以不是 air），不启动。")
            print("  多半是旧版残留的 pythonw 进程，任务管理器里结束掉所有 pythonw 再试。")
            return
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Dashboard)
        print(f"air 仪表盘: http://127.0.0.1:{PORT}")
        print(f"  Python: {PYTHON}")
        server.serve_forever()
    finally:
        dash_lock.release()


if __name__ == "__main__":
    main()
