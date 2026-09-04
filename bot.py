# -*- coding: utf-8 -*-
"""凡人修仙模拟器 · 自动刷局脚本

原理：用 Playwright 驱动真实 Edge，反复点击游戏自身的
「开始游戏 →（游戏全自动逐年模拟）→ 结算页 → 再来一局」。

只操作游戏 UI（按钮/滑块/音效）并在开局前给页面套一个极速补丁：
把 window.setTimeout 延迟钳到 ≤1ms、滑块置 0.0001s/年 —— 仅压缩渲染等待节奏，
每一年的修炼仍由游戏自身 rollYear() 真实随机模拟。

⚠️ 成仙拉满补丁（configure 第 5 步，默认开启）：包一层 window.Sim.rollYear，渡劫前
把修为顶到 ≥50w（原规则此区间 100% 成功）、成仙后最终修为 ×1000。这会让凡是能撑到
渡劫时刻的局都成仙，并大幅抬高成仙修为 —— 会真实累加成仙次数 / 最高修为到 B 站榜与
成就，属有意放大结算数值，非原版正常结算。不需要时删掉 configure 的第 5 步即可。
其余不修改任何存档数据。

用法：
    python bot.py --runs 200 --tabs 5      # 5 个标签并行刷 200 局后退出
    python bot.py --minutes 60 --tabs 3    # 3 个标签并行刷 60 分钟
    python bot.py --wait-login             # 先等 bilibili 扫码登录成功再开始刷
    python bot.py                          # 一直刷，Ctrl+C 停止
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "profile"      # Edge 用户数据目录（登录态持久化）
SHOT_DIR = BASE_DIR / "shots"
CSV_PATH = BASE_DIR / "runs.csv"

GAME_URL = "https://www.bilibili.com/toy/fanren/index.html"
FRAME_URL_FRAGMENT = "bilibilitoy.com/toy/fanren/"

# ---- 帧内 DOM 状态一次性快照 ----
STATE_JS = """
() => {
  const out = {};
  const text = (id) => {
    const el = document.getElementById(id);
    return el ? (el.textContent || '').trim() : '';
  };
  const shown = (id) => {
    const el = document.getElementById(id);
    return !!el && !el.hidden;
  };
  for (const id of ['view-home', 'view-game', 'view-settle', 'view-rank', 'view-ach',
                     'view-settings', 'view-about', 'pause-mask', 'follow-mask', 'review-mask']) {
    out[id] = shown(id);
  }
  out.texts = {};
  for (const id of ['attr-lvl', 'attr-apt', 'attr-cult', 'attr-title',
                    'settle-title', 'settle-wuhun', 'settle-lvl', 'settle-cult',
                    'settle-exp', 'settle-age', 'home-lv', 'home-lv-exp']) {
    out.texts[id] = text(id);
  }
  return out;
}
"""

CLOSE_MAP = {  # 子页/遮罩 → 关闭按钮 id
    "view-rank": "btn-close-rank",
    "view-ach": "btn-close-ach",
    "view-settings": "btn-close-settings",
    "view-about": "btn-close-about",
    "pause-mask": "btn-pause-resume",
    "follow-mask": "btn-follow-close",
    "review-mask": "btn-review-close",
}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="凡人修仙模拟器自动刷局")
    p.add_argument("--runs", type=int, default=None, help="刷多少局后退出（默认不限）")
    p.add_argument("--minutes", type=float, default=None, help="刷多少分钟后退出")
    p.add_argument("--run-timeout", type=float, default=900,
                   help="单局最长等待秒数（默认 900）")
    p.add_argument("--headless", action="store_true", help="无头模式（登录后可用）")
    p.add_argument("--wait-login", action="store_true",
                   help="先等 bilibili 扫码登录成功再开始刷局")
    p.add_argument("--tabs", type=int, default=1,
                   help="并行游戏标签页数（默认 1；同账号共享进度，可多开提速）")
    return p.parse_args(argv)


# ---------------- 页面状态 ----------------
def classify_result(title: str) -> str:
    if "飞升成仙" in title:
        return "god"
    if "成仙劫失败" in title:
        return "fail"
    if "提前结算" in title:
        return "pause"
    return "dead"


def parse_int(text: str) -> int:
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 0


def parse_innate(text: str) -> str:
    """把「灵根 杂灵根 · 先天灵根 2 级」解析成「杂灵根(2级)」"""
    import re
    m = re.search(r"先天灵根\s*(\d+)\s*级", text or "")
    lv = m.group(1) if m else "?"
    m2 = re.search(r"灵根\s*([\u4e00-\u9fff]+?)\s*·", text or "")
    name = m2.group(1) if m2 else "?"
    return f"{name}({lv}级)"


async def snapshot(frame) -> dict:
    return await frame.evaluate(STATE_JS)


async def click_btn(frame, element_id: str) -> None:
    """直接派发 DOM click（游戏只用 addEventListener('click')，不依赖命中测试，
    比 Playwright 带 actionability 等待的点击快得多且不怕遮罩遮挡）。"""
    await frame.evaluate(
        "(id) => { const el = document.getElementById(id); if (el) el.click(); }",
        element_id,
    )


async def iframe_ready(page) -> object | None:
    """等待游戏 iframe 与首页按钮就绪，返回 frame。"""
    deadline = time.time() + 30
    while time.time() < deadline:
        for f in page.frames:
            if FRAME_URL_FRAGMENT in f.url:
                try:
                    await f.wait_for_selector("#btn-start", timeout=3000)
                    return f
                except PlaywrightTimeoutError:
                    pass
        await asyncio.sleep(0.3)
    return None


async def ensure_logged_frame(page) -> object | None:
    """确保顶部页面仍指向游戏并拿到 iframe（页面被踢/跳转时重载一次）。"""
    if GAME_URL in page.url or page.url.startswith("https://www.bilibili.com/toy"):
        f = await iframe_ready(page)
        if f:
            return f
    await page.goto(GAME_URL, wait_until="domcontentloaded")
    return await iframe_ready(page)


# ---------------- 配置 ----------------
async def configure(frame) -> dict:
    """极速配置。游戏的连破 FAST_MS=60ms、成就 toast 2.2s 等延迟是闭包内写死
    够不到的，开局前直接 evaluate 把 window.setTimeout 统一钳到 ≤1ms（纯压渲染节奏，
    每一年仍由游戏 rollYear() 真实模拟）；速度滑块置 0.0001 让 TICK_MS 归零；
    关音效；成就 toast 不再拦截结算按钮。返回应用结果便于核对。"""
    return await frame.evaluate(
        """() => {
      // 1) setTimeout 延迟钳到 ≤1ms（必须在点开始前 patch，覆盖 FAST_MS / toast）
      const nativeSetTimeout = window.setTimeout.bind(window);
      window.setTimeout = function (fn, delay, ...rest) {
        const d = Number(delay);
        return nativeSetTimeout(fn, (Number.isFinite(d) && d > 0) ? 1 : 0, ...rest);
      };
      // 2) 速度滑块：min 降 0、value 0.0001 → setSpeed 算 TICK_MS 四舍五入 = 0
      const range = document.getElementById('speed-range');
      if (range) {
        range.min = '0';
        range.step = 'any';
        range.value = '0.0001';
        range.dispatchEvent(new Event('input', { bubbles: true }));
        range.dispatchEvent(new Event('change', { bubbles: true }));
      }
      // 3) 关音效
      const sound = document.getElementById('home-sound');
      if (sound && sound.checked) {
        sound.checked = false;
        sound.dispatchEvent(new Event('change', { bubbles: true }));
      }
      // 4) 成就 toast 不再拦截结算页按钮的点击
      const style = document.createElement('style');
      style.textContent = '#ach-toast{pointer-events:none !important;}';
      document.head.appendChild(style);
      // 5) 拉满成仙：包一层引擎 rollYear。game.js 每帧经 window.Sim.rollYear 驱动一局每一
      //    年，包在这里即可拦截所有真实刷局。渡劫时刻前把修为顶到 ≥50w（原规则此区间渡劫
      //    必成），成仙后再把最终修为 ×1000 放大（修为增幅拉满）。只在首次包裹时生效。
      let ascBoosted = false;
      try {
        const S = window.Sim;
        if (S && S.rollYear && !S.__frAscBoosted) {
          S.__frAscBoosted = true;
          const origRoll = S.rollYear.bind(S);
          S.rollYear = function (g, log) {
            if (g && g.lvl >= 100 && !g.ascended && g.cult < 500000) {
              g.cult = 500000;   /* 顶到必成区间：原规则 修为≥50w 渡劫 100% 成功 */
            }
            const res = origRoll(g, log);
            if (g && g.ascended && g.lvl === 101 && !g.__frAscOnce) {
              g.__frAscOnce = true;
              g.cult = Math.round(g.cult * 1000);   /* 修为增幅拉满 ×1000 */
            }
            return res;
          };
          ascBoosted = true;
        }
      } catch (e) { /* 引擎结构变化时静默跳过，不影响原刷局 */ }
      const lbl = document.getElementById('speed-val');
      return {
        setTimeoutPatched: window.setTimeout.toString().indexOf('[native code]') === -1,
        rangeValue: range ? range.value : null,
        speedLabel: lbl ? lbl.textContent : null,
        ascBoosted: ascBoosted,
      };
    }"""
    )


async def reload_and_ready(page) -> object | None:
    """重载页面并重新就绪 + 重设极速（重载会把 TICK_MS 重置回 0.3s 默认）。"""
    await page.reload(wait_until="domcontentloaded")
    frame = await ensure_logged_frame(page)
    if frame is not None:
        await configure(frame)
    return frame


# ---------------- 刷局核心 ----------------
async def wait_for_settle(frame, timeout_s: float, stop_event: asyncio.Event):
    """等结算页出现。用 CDP 原生可见性等待（不再每 50ms 打全量快照），
    超时窗口才做一次快照顺带关掉可能弹出的遮罩。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline and not stop_event.is_set():
        try:
            await frame.wait_for_selector("#btn-settle-again", state="visible", timeout=250)
            return await snapshot(frame)
        except PlaywrightTimeoutError:
            st = await snapshot(frame)
            for key, close_id in CLOSE_MAP.items():
                if st.get(key):
                    try:
                        await frame.locator("#" + close_id).click(timeout=1500)
                    except Exception:
                        pass
                    break
    return None


def count_csv_rows() -> int:
    if not CSV_PATH.exists():
        return 0
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        return max(0, sum(1 for _ in f) - 1)


def count_gods_from_csv() -> int:
    """从 runs.csv 统计历史成仙(god)次数，供会话启动时初始化累计值。"""
    if not CSV_PATH.exists():
        return 0
    gods = 0
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)  # 表头
        for row in reader:
            if len(row) > 8 and row[8] == "god":
                gods += 1
    return gods


async def bili_logged_in(context) -> bool:
    for cookie in await context.cookies("https://www.bilibili.com"):
        if cookie["name"] == "SESSDATA" and cookie.get("value"):
            return True
    return False


async def ensure_login(context, stop_event) -> bool:
    """未登录则打开 bilibili 扫码页，轮询到 SESSDATA 出现即算登录成功。"""
    if await bili_logged_in(context):
        return True
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        await page.goto("https://passport.bilibili.com/login", wait_until="domcontentloaded")
    except Exception as exc:
        print(f"[login] 打开登录页失败({exc})，未登录继续（进度仍存本地）")
        return True
    print("[login] 请在弹出的窗口扫码登录 bilibili，登录成功后自动开始刷局…")
    while not stop_event.is_set():
        if await bili_logged_in(context):
            print("[login] 检测到已登录，开始刷局")
            return True
        await asyncio.sleep(2)
    return False


class TabDead(Exception):
    """某标签页彻底不可恢复（由协调器结束整个会话，外层重启浏览器）。"""


class SessionState:
    """跨标签共享的进度与写盘状态（单事件循环，写盘用 lock 串行化）。"""

    def __init__(self, prev_runs: int, fcsv, writer):
        self.prev_runs = prev_runs
        self.fcsv = fcsv
        self.writer = writer
        self.lock = asyncio.Lock()
        self.done = 0
        self.gods = count_gods_from_csv()  # 历史累计成仙次数（含本会话前）

    async def record_run(self, texts: dict, dur: float) -> int:
        """登记一局：锁内分配全局唯一局号并落盘；成仙高亮 + 每 100 局汇总。返回局号。"""
        result = classify_result(texts.get("settle-title", ""))
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            0,  # run_no 占位，锁内填
            parse_innate(texts.get("settle-wuhun", "")),
            parse_int(texts.get("attr-lvl", "")),
            texts.get("settle-lvl", ""),
            texts.get("settle-cult", ""),
            parse_int(texts.get("settle-exp", "")),
            texts.get("settle-age", ""),
            result,
            texts.get("settle-title", ""),
            f"{dur:.1f}",
            texts.get("home-lv", ""),
            texts.get("home-lv-exp", ""),
        ]
        async with self.lock:
            self.done += 1
            run_no = self.prev_runs + self.done
            row[1] = run_no
            self.writer.writerow(row)
            self.fcsv.flush()
            if result == "god":
                self.gods += 1
                print(f"\n[✨ 成仙] 第 {run_no} 局飞升成仙！累计成仙 {self.gods} 次\n")
            if run_no % 100 == 0:
                print(f"[统计] 累计 {run_no} 局 · 成仙 {self.gods} 次 · 成仙率 {self.gods / run_no:.2%}")
        return run_no


async def grind_tab(page, tab_id: int, args, session: SessionState, start_wall, stop_event):
    """单个游戏标签页的无限刷局循环。多标签共享同 origin localStorage（fc_player/
    fc_ach 天然合并），各页真实模拟并行喂同一账号。每局异常自动 reload 续刷；
    页面彻底挂掉抛 TabDead 让协调器整会话重启。"""
    label = f"T{tab_id}"
    frame = await ensure_logged_frame(page)
    if frame is None:
        raise TabDead(f"{label}: 30s 未等到游戏 iframe")
    conf = await configure(frame)
    print(f"[init] {label} 就绪 · setTimeout钳制={conf.get('setTimeoutPatched')} · "
          f"滑块={conf.get('rangeValue')}s/年（显示 {conf.get('speedLabel')}）"
          f" · 成仙拉满注入={conf.get('ascBoosted')}")

    while True:
        if stop_event.is_set():
            return
        if args.runs is not None and (session.prev_runs + session.done) >= args.runs:
            return
        if args.minutes is not None and (time.time() - start_wall) >= args.minutes * 60:
            return

        t0 = time.time()
        try:
            st = await snapshot(frame)

            # 1) 开新局
            if st["view-settle"]:
                await click_btn(frame, "btn-settle-again")
            elif st["view-home"]:
                await click_btn(frame, "btn-start")
            elif st["view-game"]:
                pass  # 已在本局运行中
            else:
                # 打开了设置/榜单等子页：点对应关闭回首页
                closed = False
                for key, close_id in CLOSE_MAP.items():
                    if st.get(key):
                        try:
                            await frame.locator("#" + close_id).click(timeout=1500)
                        except Exception:
                            pass
                        closed = True
                        break
                if not closed:
                    frame = await reload_and_ready(page)
                continue

            # 2) 等结算
            st = await wait_for_settle(frame, args.run_timeout, stop_event)
            if st is None:
                if stop_event.is_set():
                    return
                await page.screenshot(path=str(SHOT_DIR / f"timeout_{int(t0)}.png"))
                print(f"[warn] {label} 单局超 {args.run_timeout}s，重载重试")
                frame = await reload_and_ready(page)
                continue

            # 3) 记录本局（全局唯一局号 + 落盘）
            t = st["texts"]
            run_no = await session.record_run(t, time.time() - t0)
            print(
                f"[{label}#{run_no}] {parse_innate(t.get('settle-wuhun',''))} | "
                f"{t.get('settle-lvl','?')} (Lv{parse_int(t.get('attr-lvl',''))}) | "
                f"修为 {t.get('settle-cult','?')} | {t.get('settle-exp','?')}exp | "
                f"{t.get('settle-age','?')} | {t.get('settle-title','')} | "
                f"{time.time()-t0:.1f}s | {t.get('home-lv','')} {t.get('home-lv-exp','')}"
            )
        except TabDead:
            raise
        except Exception as exc:
            # 单局内任意异常（如 iframe 被重建 detached）：截图 + reload 续刷
            print(f"[warn] {label} 单局异常({type(exc).__name__}): {exc}")
            try:
                await page.screenshot(path=str(SHOT_DIR / f"err_{int(t0)}.png"))
            except Exception:
                pass
            try:
                frame = await reload_and_ready(page)
                print(f"[recover] {label} 已重载，继续刷局")
            except Exception as exc2:
                raise TabDead(f"{label}: {exc2}") from exc
            continue


async def grind_session(args, start_wall, stop_event) -> bool:
    """一次浏览器会话：args.tabs 个游戏标签页并行真实模拟刷局（同账号共享
    localStorage，进度自然合并）。正常刷到目标 → True；标签彻底挂掉/会话异常 →
    False，由外层 run_bot 重启浏览器续刷（进度实时落盘，重启后续号接续）。"""
    profile_dir = str(PROFILE_DIR)
    prev_runs = count_csv_rows()
    try:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                profile_dir,
                channel="msedge",
                headless=args.headless,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
                args=[
                    "--lang=zh-CN",
                    # 隐藏标签/窗口被遮挡时也不节流计时器（多标签并行必需）
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                ],
            )
            page0 = context.pages[0] if context.pages else await context.new_page()
            try:
                if args.wait_login:
                    if not await ensure_login(context, stop_event):
                        return True

                print(f"[init] 打开 {GAME_URL} × {args.tabs} 个标签")
                pages = []
                for i in range(args.tabs):
                    page = page0 if i == 0 else await context.new_page()
                    await page.goto(GAME_URL, wait_until="domcontentloaded")
                    pages.append(page)

                csv_new = not CSV_PATH.exists()
                with CSV_PATH.open("a", newline="", encoding="utf-8-sig") as fcsv:
                    writer = csv.writer(fcsv)
                    if csv_new:
                        writer.writerow([
                            "time", "run", "innate", "final_lvl", "final_title", "cult",
                            "exp", "age", "result", "settle_title", "dur_s", "player_lv", "player_exp",
                        ])
                    session = SessionState(prev_runs, fcsv, writer)
                    tasks = [
                        asyncio.create_task(
                            grind_tab(page, i, args, session, start_wall, stop_event)
                        )
                        for i, page in enumerate(pages)
                    ]
                    try:
                        await asyncio.gather(*tasks)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                return True
            finally:
                try:
                    await context.close()
                except Exception:
                    pass
    except Exception as exc:
        print(f"[err] 浏览器会话异常: {exc}")
        return False
    return True


async def run_bot(args) -> int:
    PROFILE_DIR.mkdir(exist_ok=True)
    SHOT_DIR.mkdir(exist_ok=True)
    start_wall = time.time()
    stop_event = asyncio.Event()
    wait_s = 5

    while True:
        if stop_event.is_set():
            break
        if args.runs is not None and count_csv_rows() >= args.runs:
            break
        if args.minutes is not None and (time.time() - start_wall) >= args.minutes * 60:
            break

        before = count_csv_rows()
        finished = await grind_session(args, start_wall, stop_event)
        if finished or stop_event.is_set():
            break
        after = count_csv_rows()
        if after > before:
            wait_s = 5          # 有进展说明是偶发崩溃，快速重启
        else:
            wait_s = min(wait_s * 2, 60)  # 连续无进展则退避，防无意义刷屏
        print(f"[warn] 会话异常退出，{wait_s}s 后自动重启续刷（当前累计 {after} 局）…")
        await asyncio.sleep(wait_s)

    total_min = (time.time() - start_wall) / 60
    print(f"[done] 累计 {count_csv_rows()} 局，用时 {total_min:.1f} 分钟，明细见 {CSV_PATH}")
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        raise SystemExit(asyncio.run(run_bot(args)))
    except KeyboardInterrupt:
        print("\n[stop] 收到 Ctrl+C，已停止（进度与登录态已保存）")
    except BaseException:
        import traceback
        crash_log = BASE_DIR / "bot_crash.log"
        try:
            with crash_log.open("a", encoding="utf-8") as fh:
                fh.write("\n==== %s ====\n" % datetime.now().isoformat())
                traceback.print_exc(file=fh)
        except Exception:
            pass
        print(f"[fatal] 未捕获异常，详情已写入 {crash_log}")
        raise


if __name__ == "__main__":
    main()
