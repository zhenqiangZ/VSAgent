from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vsagent.core.agent.report import build, read_trace
from vsagent.core.agent.trace import log_directory_for
from vsagent.core.session_manager import SessionManager, default_state_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把一个会话的 trace 与状态文件汇成一张静态分析页。"
                    "页面不联网、不读其它文件，双击就能看。",
    )
    parser.add_argument("--session", default="", help="会话 id，默认取最近改动的 trace")
    parser.add_argument("--state", default="", help="状态文件，默认 ~/.vsagent/state.toml")
    parser.add_argument("--logs", default="", help="trace 目录，默认取状态文件旁边的 logs/")
    parser.add_argument("--out", default="", help="输出文件，默认 .probe/report-<会话>.html")
    parser.add_argument("--list", action="store_true", help="只列出有 trace 的会话")
    return parser.parse_args()


def latest_trace(logs: Path) -> Path | None:
    files = sorted(logs.glob("ses_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def render(data: dict) -> str:
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    title = data["session"]["title"] or data["session"]["id"]
    return TEMPLATE.replace("__TITLE__", title.replace("<", "&lt;")).replace("__DATA__", blob)


TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>会话分析 · __TITLE__</title>
<style>
:root{
  --bg:#f6f7f9; --card:#ffffff; --text:#1d2430; --muted:#66707e; --line:#e2e6ed;
  --accent:#2f6feb; --warn:#c9821a; --error:#c0392b; --ok:#2e7d4f;
  --cached:#a9b8ca; --uncached:#2f6feb; --output:#e08a3c;
  --l-prefix:#4a6fa5; --l-memory:#7a9e7e; --l-task:#c9a227; --l-event:#8a6fb0;
  --l-recent:#2f6feb; --l-evidence:#d0715e;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#14171c; --card:#1c2028; --text:#e6e9ef; --muted:#98a2b1; --line:#2b313b;
    --accent:#5b8def; --warn:#e0a03a; --error:#e0705f; --ok:#5cb37f;
    --cached:#4b5768; --uncached:#5b8def; --output:#e0a06a;
    --l-prefix:#6d8fc4; --l-memory:#8fb894; --l-task:#d4b34a; --l-event:#a88bc9;
    --l-recent:#5b8def; --l-evidence:#e08a7a;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
header{padding:28px 32px 20px;background:var(--card);border-bottom:1px solid var(--line)}
h1{margin:0 0 6px;font-size:22px;font-weight:600}
h2{margin:0 0 14px;font-size:17px;font-weight:600}
h3{margin:0 0 10px;font-size:14px;font-weight:600;color:var(--muted)}
.sub{color:var(--muted);font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{color:var(--muted);font-size:12px}
.card .v{font-size:22px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
.card .n{color:var(--muted);font-size:12px;margin-top:2px}
nav{position:sticky;top:0;z-index:5;display:flex;gap:18px;padding:10px 32px;
  background:var(--bg);border-bottom:1px solid var(--line)}
nav a{color:var(--muted);text-decoration:none;font-size:13px}
nav a:hover{color:var(--accent)}
main{padding:24px 32px 60px;max-width:1280px}
section{margin-bottom:36px}
table{width:100%;border-collapse:collapse;background:var(--card);
  border:1px solid var(--line);border-radius:10px;overflow:hidden;
  display:block;overflow-x:auto;white-space:normal}
@media (max-width: 1100px){ .grid2{grid-template-columns:1fr} }
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top}
th{color:var(--muted);font-weight:500;background:var(--card);position:sticky;top:41px}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
tbody tr:hover{background:rgba(47,111,235,.06)}
tbody tr.click{cursor:pointer}
tbody tr.active{background:rgba(47,111,235,.12)}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:12px;
  border:1px solid currentColor;line-height:18px}
.tag.ok{color:var(--ok)} .tag.warn{color:var(--warn)} .tag.error{color:var(--error)}
.tag.info{color:var(--muted)} .tag.muted{color:var(--muted)}
.turnbar{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px}
.turnbar button{background:var(--card);border:1px solid var(--line);color:var(--text);
  border-radius:8px;padding:5px 11px;cursor:pointer;font-size:13px}
.turnbar button:hover{border-color:var(--accent)}
.turnbar button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.q{font-size:15px;padding:12px 14px;background:var(--card);border:1px solid var(--line);
  border-left:3px solid var(--accent);border-radius:8px;margin-bottom:14px;white-space:pre-wrap}
.kv{display:flex;flex-wrap:wrap;gap:8px 22px;color:var(--muted);font-size:13px;margin-bottom:18px}
.kv b{color:var(--text);font-weight:600;font-variant-numeric:tabular-nums}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}
.chart{width:100%;height:auto;display:block}
.ax{fill:var(--muted);font-size:10px}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin-top:8px;color:var(--muted);font-size:12px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
.empty{color:var(--muted);padding:16px;text-align:center}
.note{color:var(--muted);font-size:12px;line-height:1.8}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.bar{height:6px;border-radius:3px;background:var(--accent);display:inline-block;vertical-align:middle}
</style>
</head>
<body>
<header>
  <h1>会话分析 · __TITLE__</h1>
  <div class="sub" id="sub"></div>
  <div class="cards" id="cards"></div>
</header>
<nav>
  <a href="#turns">逐轮总览</a>
  <a href="#turn">轮次详情</a>
  <a href="#topics">主题</a>
  <a href="#issues">异常</a>
</nav>
<main>
  <section id="turns">
    <h2>逐轮总览</h2>
    <table>
      <thead><tr>
        <th class="num">轮</th><th>用户消息</th><th class="num">耗时</th>
        <th class="num">步</th><th class="num">工具</th><th class="num">失败</th>
        <th class="num">推理字</th><th class="num">输入</th><th class="num">缓存</th>
        <th class="num">新增</th><th class="num">输出</th><th class="num">命中</th>
        <th class="num">费用</th><th>结束</th>
      </tr></thead>
      <tbody id="turnrows"></tbody>
    </table>
  </section>

  <section id="turn">
    <h2>轮次详情</h2>
    <div class="turnbar" id="turnbar"></div>
    <div id="turndetail"></div>
  </section>

  <section id="topics">
    <h2>主题</h2>
    <table>
      <thead><tr>
        <th>编号</th><th>标题</th><th class="num">步区间</th><th>状态</th>
        <th class="num">产物</th><th>动作</th><th>问题</th><th>降级</th>
      </tr></thead>
      <tbody id="topicrows"></tbody>
    </table>
  </section>

  <section id="issues">
    <h2>异常</h2>
    <div id="issuelist"></div>
  </section>

  <section>
    <h2>口径</h2>
    <div class="note">
      耗时取 trace 时间戳之差（用户消息写入到 turn.done），不含两次输入之间的空闲。<br>
      token 与费用取状态文件里助手消息自带的 tokens / cost；逐步的输入、缓存、输出取 trace 的 context.metrics。<br>
      步按助手消息里的 STEP_START 数；装配记录与步按顺序一一对应，条数不同时只取两边都有的那一段。<br>
      缓存命中 = cached / input，逐步取该步的读数，整轮取各步算术平均。<br>
      分层占用取 context.metrics 的 usage，history 为负值时按 0 画。<br>
      事件层 degraded 的判据是「层里的主题数少于已封存数」，长会话里它每一步都为真，与这一步是否真的裁掉东西无关。
    </div>
  </section>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
const data = JSON.parse(document.getElementById('data').textContent);
const C = getComputedStyle(document.documentElement);
const color = (name) => C.getPropertyValue(name).trim();

const fmt = (n) => (n === null || n === undefined || isNaN(n)) ? '—' : Number(n).toLocaleString('zh-CN');
const pct = (x) => (x || 0).toFixed(3);
const secs = (s) => s >= 120 ? (s / 60).toFixed(1) + ' 分' : s.toFixed(1) + ' 秒';
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const money = (x) => x ? '$' + x.toFixed(4) : '—';

function statusTag(status) {
  const map = {done: ['ok', '完成'], running: ['warn', '进行中'], failed: ['error', '失败'],
               cancelled: ['error', '中断'], 'no-reply': ['info', '无回复'], unknown: ['info', '未知']};
  const [cls, label] = map[status] || ['info', status || '未知'];
  return `<span class="tag ${cls}">${esc(label)}</span>`;
}
function eventTag(status) {
  const map = {completed: ['ok', '完成'], failed: ['error', '失败'], cancelled: ['error', '中断']};
  const [cls, label] = map[status] || ['info', status || '—'];
  return `<span class="tag ${cls}">${esc(label)}</span>`;
}

function renderHead() {
  const s = data.session;
  const when = new Date(data.generated_at * 1000).toLocaleString('zh-CN');
  document.getElementById('sub').textContent =
    `${s.id} · ${s.model || '未知模型'} · ${s.messages} 条消息 · ${s.topics} 个主题 · 生成于 ${when}`;
  const cards = [
    ['轮数', fmt(s.turns), `${s.steps} 步`],
    ['工具调用', fmt(s.tools), `失败 ${s.failed} 次${s.refused ? `（收束拒绝 ${s.refused}）` : ''}`],
    ['输入 token', fmt(s.tokens.input), `缓存复用 ${(s.cache_reuse * 100).toFixed(1)}%`],
    ['输出 token', fmt(s.tokens.output), `新增输入 ${fmt(s.tokens.uncached)}`],
    ['费用', money(s.cost), s.model || ''],
    ['活跃时长', secs(s.active_seconds), s.last_exit_reason ? '上一轮停在 ' + s.last_exit_reason : ''],
  ];
  document.getElementById('cards').innerHTML = cards.map(
    ([k, v, n]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div><div class="n">${esc(n)}</div></div>`
  ).join('');
}

function renderTurns() {
  const body = document.getElementById('turnrows');
  body.innerHTML = data.turns.map((t) => `
    <tr class="click" data-turn="${t.index}">
      <td class="num">${t.index}</td>
      <td>${esc((t.user_text || '（空）').slice(0, 60))}${t.user_text.length > 60 ? '…' : ''}</td>
      <td class="num">${secs(t.seconds)}</td>
      <td class="num">${t.steps.length}</td>
      <td class="num">${t.tools.length}</td>
      <td class="num">${t.failed || ''}</td>
      <td class="num">${fmt(t.reasoning_chars)}</td>
      <td class="num">${fmt(t.tokens.input)}</td>
      <td class="num">${fmt(t.tokens.cached)}</td>
      <td class="num">${fmt(t.tokens.uncached)}</td>
      <td class="num">${fmt(t.tokens.output)}</td>
      <td class="num">${pct(t.cache_hit)}</td>
      <td class="num">${money(t.cost)}</td>
      <td>${statusTag(t.status)}</td>
    </tr>`).join('');
  body.querySelectorAll('tr').forEach((row) => {
    row.addEventListener('click', () => selectTurn(Number(row.dataset.turn)));
  });
}

function chartStack(el, metrics, series, colors, names) {
  const W = 720, H = 220, P = {l: 54, r: 12, t: 12, b: 26};
  const n = Math.max(metrics.length, 1);
  const max = Math.max(1, ...metrics.map((m) => series.reduce((s, k) => s + (m[k] || 0), 0)));
  const bw = (W - P.l - P.r) / n;
  let svg = '';
  for (let i = 0; i <= 4; i++) {
    const y = P.t + (H - P.t - P.b) * i / 4;
    const v = Math.round(max * (1 - i / 4));
    svg += `<line x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}" stroke="${color('--line')}"/>`;
    svg += `<text x="${P.l - 6}" y="${y + 4}" text-anchor="end" class="ax">${fmt(v)}</text>`;
  }
  metrics.forEach((m, i) => {
    const x = P.l + i * bw + bw * 0.16;
    const w = Math.max(bw * 0.68, 1);
    let y = H - P.b;
    series.forEach((k) => {
      const v = m[k] || 0;
      if (v <= 0) return;
      const hh = (H - P.t - P.b) * v / max;
      y -= hh;
      svg += `<rect x="${x}" y="${y}" width="${w}" height="${hh}" fill="${color(colors[k])}">`
           + `<title>第 ${m.step} 步 ${names[k]} ${fmt(v)}</title></rect>`;
    });
    if (n <= 18 || i % Math.ceil(n / 18) === 0) {
      svg += `<text x="${x + w / 2}" y="${H - 8}" text-anchor="middle" class="ax">${m.step}</text>`;
    }
  });
  const legend = series.map(
    (k) => `<span><i style="background:${color(colors[k])}"></i>${names[k]}</span>`
  ).join('');
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="chart">${svg}</svg><div class="legend">${legend}</div>`;
}

function chartLine(el, metrics, key, label, colorName) {
  const W = 720, H = 220, P = {l: 54, r: 12, t: 12, b: 26};
  const n = Math.max(metrics.length, 1);
  const bw = (W - P.l - P.r) / n;
  let svg = '';
  for (let i = 0; i <= 4; i++) {
    const y = P.t + (H - P.t - P.b) * i / 4;
    const v = (1 - i / 4);
    svg += `<line x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}" stroke="${color('--line')}"/>`;
    svg += `<text x="${P.l - 6}" y="${y + 4}" text-anchor="end" class="ax">${v.toFixed(2)}</text>`;
  }
  const pts = metrics.map((m, i) => {
    const x = P.l + i * bw + bw / 2;
    const y = P.t + (H - P.t - P.b) * (1 - Math.min(Math.max(m[key] || 0, 0), 1));
    return [x, y];
  });
  if (pts.length) {
    svg += `<polyline points="${pts.map((p) => p.join(',')).join(' ')}" fill="none" stroke="${color(colorName)}" stroke-width="2"/>`;
    pts.forEach((p, i) => {
      svg += `<circle cx="${p[0]}" cy="${p[1]}" r="3" fill="${color(colorName)}">`
           + `<title>第 ${metrics[i].step} 步 ${label} ${pct(metrics[i][key])}</title></circle>`;
      if (n <= 18 || i % Math.ceil(n / 18) === 0) {
        svg += `<text x="${p[0]}" y="${H - 8}" text-anchor="middle" class="ax">${metrics[i].step}</text>`;
      }
    });
  }
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="chart">${svg}</svg>`;
}

function chartBars(el, metrics, key, label, colorName) {
  const W = 720, H = 220, P = {l: 54, r: 12, t: 12, b: 26};
  const n = Math.max(metrics.length, 1);
  const max = Math.max(1, ...metrics.map((m) => m[key] || 0));
  const bw = (W - P.l - P.r) / n;
  let svg = '';
  for (let i = 0; i <= 4; i++) {
    const y = P.t + (H - P.t - P.b) * i / 4;
    svg += `<line x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}" stroke="${color('--line')}"/>`;
    svg += `<text x="${P.l - 6}" y="${y + 4}" text-anchor="end" class="ax">${(max * (1 - i / 4)).toFixed(0)}</text>`;
  }
  metrics.forEach((m, i) => {
    const v = m[key] || 0;
    const x = P.l + i * bw + bw * 0.18;
    const w = Math.max(bw * 0.64, 1);
    const hh = (H - P.t - P.b) * v / max;
    svg += `<rect x="${x}" y="${H - P.b - hh}" width="${w}" height="${hh}" fill="${color(colorName)}" rx="2">`
         + `<title>第 ${m.step} 步 ${label} ${v}</title></rect>`;
    if (n <= 18 || i % Math.ceil(n / 18) === 0) {
      svg += `<text x="${x + w / 2}" y="${H - 8}" text-anchor="middle" class="ax">${m.step}</text>`;
    }
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="chart">${svg}</svg>`;
}

function stepTable(turn) {
  if (!turn.steps.length) return '<div class="empty">这一轮没有记下步</div>';
  const rows = turn.steps.map((s) => {
    const m = s.metric;
    return `<tr>
      <td class="num">${s.no}</td>
      <td class="num">${s.seconds === null ? '—' : s.seconds.toFixed(1)}</td>
      <td>${s.tools.length ? esc(s.tools.join('、')) : '<span class="tag muted">无工具</span>'}</td>
      <td class="num">${fmt(s.reasoning)}</td>
      <td class="num">${m ? fmt(m.input) : '—'}</td>
      <td class="num">${m ? fmt(m.cached) : '—'}</td>
      <td class="num">${m ? fmt(m.uncached) : '—'}</td>
      <td class="num">${m ? fmt(m.output) : '—'}</td>
      <td class="num">${m ? pct(m.hit) : '—'}</td>
      <td>${m && m.degraded.length ? esc(m.degraded.join('、')) : ''}</td>
    </tr>`;
  }).join('');
  return `<table><thead><tr>
    <th class="num">步</th><th class="num">耗时</th><th>工具</th><th class="num">推理字</th>
    <th class="num">输入</th><th class="num">缓存</th><th class="num">新增</th>
    <th class="num">输出</th><th class="num">命中</th><th>降级</th>
  </tr></thead><tbody>${rows}</tbody></table>`;
}

function toolTable(turn) {
  if (!turn.tools.length) return '<div class="empty">这一轮没有调用工具</div>';
  const rows = turn.tools.map((t) => `<tr>
    <td class="mono">${esc(t.name)}</td>
    <td>${t.status === 'failed' ? '<span class="tag error">失败</span>' : '<span class="tag ok">成功</span>'}</td>
    <td class="num">${fmt(t.chars)}</td>
    <td class="mono">${esc(t.keys.join(', '))}</td>
    <td>${esc(t.error || t.permission)}</td>
  </tr>`).join('');
  return `<table><thead><tr>
    <th>工具</th><th>状态</th><th class="num">输出字符</th><th>参数键</th><th>失败原因 / 许可</th>
  </tr></thead><tbody>${rows}</tbody></table>`;
}

function turnTopics(turn) {
  const items = data.events.filter((e) => e.created_at >= turn.started_at - 0.5 && e.created_at <= turn.ended_at + 0.5);
  if (!items.length) return '<div class="empty">这一轮没有封存主题</div>';
  return `<table><thead><tr><th>编号</th><th>标题</th><th class="num">步</th><th>状态</th><th>产物</th></tr></thead><tbody>`
    + items.map((e) => `<tr>
        <td class="mono">${esc(e.id)}</td>
        <td>${esc(e.title)}</td>
        <td class="num">${e.steps}</td>
        <td>${eventTag(e.status)}</td>
        <td class="mono">${esc(e.artifacts.slice(0, 6).join('、'))}</td>
      </tr>`).join('')
    + '</tbody></table>';
}

function selectTurn(index) {
  const turn = data.turns.find((t) => t.index === index);
  if (!turn) return;
  document.querySelectorAll('#turnbar button').forEach((b) => {
    b.classList.toggle('on', Number(b.dataset.turn) === index);
  });
  document.querySelectorAll('#turnrows tr').forEach((r) => {
    r.classList.toggle('active', Number(r.dataset.turn) === index);
  });
  const box = document.getElementById('turndetail');
  const tokens = turn.tokens;
  box.innerHTML = `
    <div class="q">${esc(turn.user_text || '（空消息）')}</div>
    <div class="kv">
      <span>耗时 <b>${secs(turn.seconds)}</b></span>
      <span>步 <b>${turn.steps.length}</b></span>
      <span>工具 <b>${turn.tools.length}</b>（失败 ${turn.failed}）</span>
      <span>推理 <b>${fmt(turn.reasoning_chars)}</b> 字</span>
      <span>正文 <b>${fmt(turn.text_chars)}</b> 字</span>
      <span>输入 <b>${fmt(tokens.input)}</b></span>
      <span>缓存 <b>${fmt(tokens.cached)}</b>（${(tokens.input ? tokens.cached / tokens.input * 100 : 0).toFixed(1)}%）</span>
      <span>输出 <b>${fmt(tokens.output)}</b></span>
      <span>费用 <b>${money(turn.cost)}</b></span>
      <span>结束 ${statusTag(turn.status)}</span>
    </div>
    ${turn.answer_text ? `<div class="q" style="border-left-color:var(--ok)">${esc(turn.answer_text.slice(0, 600))}${turn.answer_text.length > 600 ? '…' : ''}</div>` : ''}
    <div class="grid2">
      <div class="card"><h3>每步 token</h3><div id="c-tokens"></div></div>
      <div class="card"><h3>缓存命中</h3><div id="c-cache"></div></div>
      <div class="card"><h3>上下文分层</h3><div id="c-layers"></div></div>
      <div class="card"><h3>每步耗时（秒）</h3><div id="c-time"></div></div>
    </div>
    <h3 style="margin-top:24px">逐步明细</h3>
    ${stepTable(turn)}
    <h3 style="margin-top:24px">工具调用</h3>
    ${toolTable(turn)}
    <h3 style="margin-top:24px">本轮主题</h3>
    ${turnTopics(turn)}
  `;
  const metrics = turn.metrics;
  if (metrics.length) {
    chartStack(document.getElementById('c-tokens'), metrics, ['cached', 'uncached'],
      {cached: '--cached', uncached: '--uncached'}, {cached: '缓存命中', uncached: '新增输入'});
    chartLine(document.getElementById('c-cache'), metrics, 'hit', '命中', '--accent');
    // 分层占用嵌在 usage 里，摊平之后再画
    const layerRows = metrics.map((m) => ({...m, ...m.usage}));
    chartStack(document.getElementById('c-layers'), layerRows,
      ['prefix', 'memory', 'task', 'event', 'recent', 'evidence'],
      {prefix: '--l-prefix', memory: '--l-memory', task: '--l-task', event: '--l-event',
       recent: '--l-recent', evidence: '--l-evidence'},
      {prefix: '前缀', memory: '记忆', task: '任务', event: '事件', recent: '最近', evidence: '证据'});
    // 耗时不在 metrics 里，用步表补上
    const withTime = metrics.map((m, i) => ({...m, seconds: turn.steps[i] ? turn.steps[i].seconds : 0}));
    chartBars(document.getElementById('c-time'), withTime, 'seconds', '耗时', '--l-evidence');
  } else {
    ['c-tokens', 'c-cache', 'c-layers', 'c-time'].forEach((id) => {
      document.getElementById(id).innerHTML = '<div class="empty">这一轮没有装配记录</div>';
    });
  }
}

function renderTopics() {
  document.getElementById('topicrows').innerHTML = data.events.map((e) => `
    <tr>
      <td class="mono">${esc(e.id)}</td>
      <td>${esc(e.title)}</td>
      <td class="num">${e.step_start}–${e.step_end}</td>
      <td>${eventTag(e.status)}</td>
      <td class="num">${e.artifacts.length}</td>
      <td class="mono">${esc(e.actions.slice(0, 8).join('、'))}</td>
      <td>${e.issues.length ? esc(e.issues.join('；').slice(0, 120)) : ''}</td>
      <td>${e.degraded ? '<span class="tag warn">本地拼</span>' : ''}</td>
    </tr>`).join('');
}

function renderIssues() {
  const box = document.getElementById('issuelist');
  if (!data.issues.length) {
    box.innerHTML = '<div class="empty">没有发现异常</div>';
    return;
  }
  const order = {error: 0, warn: 1, info: 2};
  const items = [...data.issues].sort((a, b) => (order[a.level] || 3) - (order[b.level] || 3));
  box.innerHTML = `<table><thead><tr><th>级别</th><th>说明</th></tr></thead><tbody>`
    + items.map((it) => {
      const cls = it.level === 'error' ? 'error' : it.level === 'warn' ? 'warn' : 'info';
      const label = it.level === 'error' ? '错误' : it.level === 'warn' ? '警告' : '提示';
      return `<tr><td><span class="tag ${cls}">${label}</span></td><td>${esc(it.text)}</td></tr>`;
    }).join('')
    + '</tbody></table>';
}

renderHead();
renderTurns();
renderTopics();
renderIssues();
const bar = document.getElementById('turnbar');
bar.innerHTML = data.turns.map((t) =>
  `<button data-turn="${t.index}">第 ${t.index} 轮</button>`).join('');
bar.querySelectorAll('button').forEach((b) => {
  b.addEventListener('click', () => selectTurn(Number(b.dataset.turn)));
});
if (data.turns.length) selectTurn(data.turns[data.turns.length - 1].index);
</script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    state_path = Path(args.state).expanduser() if args.state else default_state_path()
    # 默认跟着状态文件走：判据把状态文件指到 `.probe/` 时，读的也是那一份旁边的日志，
    # 不会去翻用户家目录里同名的会话。
    logs = Path(args.logs).expanduser() if args.logs else log_directory_for(state_path)

    manager = SessionManager(state_path)
    manager.load_from_toml()
    sessions = {session.id: session for session in manager.list_sessions()}

    if args.list:
        for session in sorted(sessions.values(), key=lambda s: s.created_at, reverse=True):
            trace = logs / f"{session.id}.jsonl"
            size = f"{trace.stat().st_size / 1024 / 1024:.1f} MB" if trace.exists() else "无 trace"
            print(f"{session.id}  {size:>8}  {session.title}")
        return 0

    session_id = args.session
    if not session_id:
        latest = latest_trace(logs)
        if latest is None:
            print(f"没有找到任何 trace：{logs}", file=sys.stderr)
            return 1
        session_id = latest.stem

    session = sessions.get(session_id)
    if session is None:
        print(f"状态文件里没有会话 {session_id}：{state_path}", file=sys.stderr)
        return 1

    trace_path = logs / f"{session_id}.jsonl"
    trace = read_trace(trace_path, session_id) if trace_path.exists() else []
    if not trace:
        print(f"警告：{trace_path} 不存在或为空，逐步数据会缺失", file=sys.stderr)

    data = build(session, trace)
    out = Path(args.out) if args.out else PROJECT_ROOT / ".probe" / f"report-{session_id}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8")

    summary = data["session"]
    print(f"会话 {summary['id']}：{summary['turns']} 轮 / {summary['steps']} 步 / "
          f"{summary['tools']} 次工具（失败 {summary['failed']}）/ "
          f"{summary['tokens']['input'] + summary['tokens']['output']} token / "
          f"{summary['cost']:.4f} 美元")
    print(f"异常 {len(data['issues'])} 条")
    print(f"页面：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
