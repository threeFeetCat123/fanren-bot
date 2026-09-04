# 凡人修仙模拟器 · 自动刷局脚本

用 Playwright 驱动真实 Edge，反复自动玩 B站《凡人修仙模拟器》：
`开始游戏 →（游戏自身全自动模拟一年年修炼）→ 结算 → 再来一局`，循环攒玩家等级经验与成就。

## 它做什么 / 不做什么

- ✅ 只点游戏 UI 自己的按钮：`开始游戏` / `再来一局`
- ✅ **极速补丁**：开局前把页面内 `setTimeout` 延迟钳到 ≤1ms、速度滑块置 `0.0001s/年`——
  只压缩游戏的“渲染/等待节奏”（原连破日志 60ms/条、成就 toast 2.2s、单年 100ms 全砍掉），
  每一年的修炼仍由游戏自己的 `rollYear()` 真实随机模拟
- ✅ 每一局都由游戏自身代码真实跑完（灵根、突破、事件、死亡/飞升全由游戏逻辑决定）
- ✅ 玩家经验、成就、灵根概率加成全部走游戏正常结算逻辑累积
- ❌ 不改游戏存档 / localStorage / 云存档，不伪造成就、玩家等级、排行榜分数，不注入假局
- ❌ 不替你做三连/关注等真实社交操作

> ⚠️ **可选注入补丁（`configure()` 内置，默认开启）**：除上面的极速补丁外，脚本还会包一层
> `window.Sim.rollYear` —— 渡劫时刻前把角色修为顶到 ≥50w（游戏原规则此区间渡劫 100% 成功）、
> 成仙后把最终修为 ×1000。效果：凡是能撑到渡劫十层(Lv100)的局都稳定成仙（Lv101 仙人、修为约
> 20 亿、+1000 成仙经验），成仙次数/最高修为会真实结算放大到账号。这属**有意放大结算数值**，
> 非原版正常结算；不需要时删掉 `configure()` 里注释为「成仙拉满」的一段即可还原。

## 环境准备

```powershell
cd C:\Users\74770\Desktop\fanren-bot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install playwright
```

> 无需 `playwright install` 下载浏览器：脚本用 `channel="msedge"` 驱动本机已装的 Edge。

## 运行

首次运行会弹出一个独立 Edge 窗口（专用 profile，目录 `profile\`，登录态会保留）。

```powershell
# 等扫码登录成功后再开始刷（推荐首次用）
.\\.venv\Scripts\python.exe bot.py --wait-login --runs 3000

# 刷到累计 3000 局后退出（局数按 runs.csv 累计，可多次运行续刷）
.\\.venv\Scripts\python.exe bot.py --runs 3000

# 5 个标签页并行刷（同账号共享进度，实测 ~350 局/分）
.\\.venv\Scripts\python.exe bot.py --runs 20000 --tabs 5

# 刷 60 分钟
.\\.venv\Scripts\python.exe bot.py --minutes 60

# 一直刷，Ctrl+C 停止
.\\.venv\Scripts\python.exe bot.py

# 已登录过之后可用无头模式（不弹窗）
.\\.venv\Scripts\python.exe bot.py --minutes 60 --headless
```

> 中途 iframe/浏览器异常会自动重载或重启续刷（进度实时落盘，重启后续号接续）；
> 单局/会话都带超时与截图，`shots\` 里可查。

每局结束打印一行摘要（多标签时前缀 `T0`~`T4` 标识来源标签）；明细存 `runs.csv`
（utf-8-sig，Excel 可直接开）。

> 实测极速档单局约 0.2~2s；单标签约 **60 局/分**，5 标签并行约 **350 局/分**。
> 多标签原理：同一账号所有标签共享同 origin 的 localStorage，每局真实模拟的
> exp/成就都累进同一份玩家状态，相当于给一个账号开 N 条并行真实模拟线。

## 登录（可选但推荐）

不登录也能刷，进度存在本地 profile（换设备/清 profile 会丢）。要跨设备同步玩家等级/成就、
上 B站榜，需要让 **bot 自己的专用 profile** 处于登录态：

```powershell
.\\.venv\Scripts\python.exe bot.py --wait-login
```

会先弹出 bilibili 扫码页，扫一次码后自动开始刷（登录态存入 `profile\`，之后一直有效）。
注意：在你日常浏览器里登录**不会**带到 bot 的 profile，必须在 bot 弹出的窗口里登录。

## 关于「三连+关注」+2.5% 高阶灵根增益

该增益需要真实账号对作者的视频做点赞/收藏/投币/关注，脚本**不会**替你做这些社交操作。
建议自己手动做一次，属永久增益。

## 常见问题

- 单局超时（默认 900s）会自动截图到 `shots\` 并重载重试
- 意外打开设置/榜单页时会自动点关闭回到首页
- 想停就 `Ctrl+C`，随时可再跑，进度都在

## 文件

| 文件           | 说明                               |
| -------------- | ---------------------------------- |
| `bot.py`       | 主脚本                             |
| `runs.csv`     | 每局结果明细（自动生成）           |
| `profile\`     | Edge 用户数据（登录态）            |
| `shots\`       | 异常截图                           |
| `_game_files\` | 游戏源码留档（分析用，与运行无关） |
