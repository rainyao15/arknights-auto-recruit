# 明日方舟 自动公招脚本（ak-recruit-bot）

全自动公招：读屏 → OCR 识别词条 → 决策选最优词条 → 选 9 小时 → 确认招募 → 加急许可立即完成 → 记录/推送出货，循环直到资源耗尽。

## 当前进度（2026-11-03 10:15 实测）

| 环节 | 状态 | 证据 |
|------|------|------|
| 模拟器 + 游戏起得来 | ✅ | MuMu 15.x / Android 15 / 游戏 2.7.91，15GB 资源已下完，能进大厅 |
| 进公招页 → 点槽位 → 读 5 词条 | ✅ | 日志 `识别词条(5): 狙击干员、辅助干员…` |
| 决策（无特殊词条不选） | ✅ | 14 个单测 + 真机多轮一致 |
| 设 9 小时 | ✅ | `时长已设为 9:00` |
| 开始招募 → 二次确认 | ✅ | 点 ✓ @(1205,727) |
| 加急（立即招募 → 弹窗 ✓） | ✅ | 点 ✓ @(1206,634) |
| 聘用候选人 → 黑箱动画 SKIP → 出货卡 | ✅ | 10:09 / 10:13 / 10:15 三次真机出货：3★空爆、4★猎蜂、3★炎熔 |
| 统计 / 票据折算 / 最高星 | ✅ | `logs/history.jsonl`、`本次 3 次 \| 4★×1、3★×2` |
| 离线回归测试 | ✅ | `tests/test_core.py`(14) + `tests/test_flow.py`(3) |
| 邮件推送 | ⏳ 待授权码 | `notify.smtp_password` 空 → 只记本地日志 |
| 长时间挂机（loop + 槽位刷新） | ⏳ 待跑 | 加急许可耗尽后按 `on_expedite_exhausted` 处理 |

### 不再弹黑框（2026-11-03 修）

打包成 windowed exe 后，**每一次 adb 调用都会弹一个黑色控制台窗口再消失**
（一顿招募几十上百次调用，看起来就是"窗口一直弹"）。已在 `core/adb_client.py`
给所有 `subprocess.run` 加：

```python
creationflags = 0x08000000   # CREATE_NO_WINDOW
startupinfo.dwFlags |= STARTF_USESHOWWINDOW
startupinfo.wShowWindow = 0  # SW_HIDE
```

效果：全程零黑框、不抢焦点，面板最小化也不会打扰你正常用电脑。

### 时长直接就位（不是一小时一小时等）

游戏里**没有**"直接 9 小时"的入口，招募时限上限就是 `09:00:00`（点过头会绕回 01:00），
所以只能点「+」。优化到极限的做法：

1. 先读一次当前时长（FastOcr 60ms）；
2. 算出还差几格，**一条 adb 命令连点**（`input tap` 串联，8 下 ≈0.5s，实测 20ms 间隔 100% 命中）；
3. `need == 0` 时一下都不点（防止 9:00 被点回 1:00）；
4. 每轮只校验一次（60ms），点漏了就地补；之后三格直接跳过校验。

### 弹窗（一直弹的那个已治）

根因不是"弹窗多"，是**联络次数 0/3 时还在点卡片**，游戏就一直弹
`是否消耗1次联络机会？`。现在的处理：

| 场景 | 处理 |
|------|------|
| 任何弹窗 | `kill_popups_fast()` 用 60ms 的 rec 认出 `是否/确定/取消/不足/上限` → 点 ✗ |
| 挂单没提交成功 | 清弹窗 → 记为失败；**连续 3 格失败**才收工 |
| 招募预算 0/1 | 面板上读到「招募预算 0/1」→ 直接收工并通知 |
| 收工 | 先把面板/弹窗清干净再退出，游戏留在列表页 |

⚠️ **勘误（2026-11-03 晚）**：之前把「联络次数 0/3」当成"今天不能招募了"是**错的**。
联络次数指的是人脉联络/刷新标签的额度，和能不能开公招位**无关**。
实测：联络次数一直 0/3，照样连开 4 个位，招募预算从 839 → 835。
真正的限制是面板上的「招募预算 N/1」，所以现在只在 `N == 0` 时停。

### 极速路径（2026-11-03 晚重构）

瓶颈不是点击，是 OCR + 截图：

| 动作 | 耗时 |
|------|------|
| 全屏 RapidOCR | 4000~6000 ms |
| 裁区域 + 检测 | 1000~1500 ms |
| **FastOcr（只识别不检测，固定小裁块）** | **50~70 ms** |
| `exec-out screencap`（raw） | 660 ms（PNG 774 ms） |

所以改成：**一帧多次复用 + 只在小裁块上做 rec**。真机实测：

| 阶段 | 改前 | 改后 |
|------|------|------|
| 挂单（读词条→选词条→9h→开始招募） | 30~60s | **2~4s** |
| 领取（加急→聘用→SKIP→读出货） | 20~30s | **3.5~4s** |
| 一轮四格 | 4~5 分钟 | **约 25~30 秒** |

坐标全在 `config.ui.fast`（time / header / list_head / name / tag_grid / cards），
按 1600x900 标定；分辨率不符时 `UI.fast_ready()` 会自动退回慢速全屏 OCR。

## 核心原理（为什么这么决策）

公招的底层规则（来自官方拆包数据 `gacha_table.json`）：

- **只有 2 个词条影响星级**：
  - `高级资深干员` → 必得 **6★**
  - `资深干员` → 必得 **5★**
- 其余 29 个词条只决定"同星级内抽到谁"，**不改变星级**。
- 因此当你**不看人物价值、干员全解锁、纯刷黄绿票**时，同星级干员收益相同，决策退化成三条规则：

```
有「高级资深干员」→ 单独选它                       → 6★
否则有「资深干员」 → 单独选它                       → 5★
否则 → 枚举 1~3 个词条的组合，找「池内最低星最高」的：
        召唤        → 池里只剩 豆苗(4★)            → 保底 4★   ← 必须点
        输出+群攻   → 池里只剩 4★                  → 保底 4★
        只有降星词条（新手/元素/支援机械）→ 一个都不选 → 3★~4★ 随机
```

⚠️ 初版写的是「任选一个词条」，这是**错的**：普通词条只收窄池子，不提升星级；
但当某个词条（或组合）的池子里**最弱的干员都 ≥4★** 时，选它就是把「3★~4★ 随机」
变成「保底 4★」。`召唤` 就是最典型的例子（池：豆苗 4★ / 梅尔 5★ / 凯尔希 6★，
没选资深时 5★/6★ 进不了池，所以只剩豆苗 → 百分百 4★）。现在由
`decision.guarantee_best()` 自动枚举组合找这种保底，找到了就点。

这就是"刷黄绿票"目标下的数学最优，比 MAA 的决策简单一个数量级（MAA 复杂是因为要帮普通玩家锁特定干员）。

数据来源：[MAA 的 recruitment.json](https://github.com/MaaAssistantArknights/MaaAssistantArknights)（官方拆包生成，随版本更新），已缓存到 `data/recruitment.json`。

## 环境要求

- Windows + **MuMu 模拟器**（其他模拟器改 ADB 端口即可）
- Python 3.10+（已在 3.13 验证）
- adb（MuMu 自带，或装 [platform-tools](https://developer.android.com/tools/releases/platform-tools)）

## 安装

```bash
cd ak-recruit-bot
python -m pip install -r requirements.txt
```

## 配置（config.yaml）

必改的只有一处：

1. **SMTP 授权码**（QQ 邮箱推送用）：
   - 去 QQ 邮箱网页版 → 设置 → 账户 → 开启 SMTP 服务 → 生成授权码
   - 把授权码填进 `config.yaml` 的 `notify.smtp_password`
   - `notify.smtp_user` 和 `notify.to` 填你自己的 QQ 邮箱

2. **ADB 路径**（如 adb 不在 PATH）：
   ```yaml
   adb:
     adb_path: "C:/Program Files/Netease/MuMu Player 12/shell/adb.exe"
   ```

3. **模拟器端口**（MuMu12 默认 `16384`，一般不用改）：
   ```yaml
   adb:
     ports: [16384, 7555, 5555, 62001, 21503]   # 自动探测
   ```

## 校准公招位坐标

脚本大部分按钮是 **OCR 驱动**（按文字找按钮，自动适配分辨率），只有"点开 4 个公招位"需要坐标。

1. 启动 MuMu，打开明日方舟，进入**公开招募界面**
2. 运行校准工具：
   ```bash
   python calibrate.py
   ```
3. 在弹窗里依次点击 4 个公招位的中心
4. 把打印的坐标填进 `config.yaml` 的 `ui.slots`

## 运行

```bash
python main.py
```

首次跑建议开着游戏窗口观察，确认每一步点击正确；识别失败会自动截图存到 `logs/`，方便排查。

## 流程说明

```
连接模拟器 → 加载词条数据 → 进入公招
→ 逐槽位：
   点开槽位 → OCR 读 5 个词条 → 决策 → 点选词条 → 设 9 小时
   → 确认招募 → 加急许可 → 读出货干员 → 记录/推送 → 关闭结果
→ 汇总
```

## 配置要点

| 配置 | 说明 |
|---|---|
| `game.use_expedite` | `true` 用加急许可立即完成 |
| `game.on_expedite_exhausted` | `stop` 加急用完后停止+通知；`natural` 挂 9h 自然招募 |
| `decision.mode` | `basic` 保底优先（默认）；`lock` 锁定特定干员（将来用） |
| `notify.notify_min_rarity` | 出货 ≥4★ 才推送（默认） |
| `data.update_on_start` | `true` 每次启动拉最新数据，`false` 用本地缓存 |

## 目录结构

```
ak-recruit-bot/
├── config.yaml          # 配置
├── requirements.txt
├── main.py              # 入口（主循环）
├── calibrate.py         # 坐标校准工具
├── core/
│   ├── adb_client.py    # ADB 封装（截图/点击/端口探测）
│   ├── ocr.py           # OCR（rapidocr）
│   ├── tag_recognizer.py# 词条白名单匹配
│   ├── decision.py      # 决策引擎
│   ├── data_loader.py   # 词条数据加载
│   ├── ui.py            # OCR 驱动的 UI 自动化
│   ├── notifier.py      # 日志 + QQ邮箱推送
│   ├── config.py        # 配置加载
│   └── logger.py        # 日志
├── data/recruitment.json# 词条数据缓存
├── logs/                # 运行日志 + 截图
└── tests/test_core.py   # 核心逻辑单测
```

## 常见问题

- **连不上模拟器**：确认 MuMu 已启动，且 `adb_path`/端口正确；运行 `adb devices` 看有没有设备。
- **找不到"公开招募"入口 / 词条识别不全**：UI 文字因版本不同可能有差异，`core/ui.py` 里的关键词可自行调整；失败截图在 `logs/`。
- **OCR 首次运行慢**：rapidocr 首次会加载模型，属正常。

---

## 模拟器排障（2026-11 实测：MuMu 15.x / Android 15 / AMD Vega8 / 游戏 2.7.91）

这一节是踩过的坑，按"症状 → 根因 → 修法"记，下次直接照抄。

| # | 症状 | 根因 | 修法 |
|---|------|------|------|
| 1 | `adb devices` 一会儿 `device` 一会儿 `offline`，`logcat` 卡在 `waiting for device` | 机器上装了两个版本的 adb：MuMu15 自带 `36.0.0`、i4Tools 自带 `29.0.4`，抢 5037 端口互相 kill server | 只用一个 adb：`config.yaml → adb.adb_path` 写死 MuMu 的 `D:/Program Files/Netease/MuMu/nx_main/adb.exe`，别的 adb.exe 别调 |
| 2 | 玩游戏 20~40 秒后**整个模拟器**直接退出（不是游戏崩，是模拟器没了） | 渲染后端是 Vulkan（`renderer_mode: vk`），宿主 `amdvlk64.dll` 崩：日志 `Renderer::error 901 / crashModule=amdvlk64.dll / Graphics Driver crash` | `renderer_mode: dx`（DirectX）。`MuMuManager.exe setting -v 0 -k renderer_mode -val dx` |
| 3 | 模拟器活着，但游戏起来 3 秒 SIGSEGV（`libunity.so` 空指针，`fault addr 0xfc`） | `force_discrete_graphics: true`，机器只有核显（Vega 8），硬走独显路径 | 设 `false` |
| 4 | 同上，游 3 秒 SIGSEGV | `renderer_strategy: auto`（或 `perf`）在该组合下同样触发空指针 | 设 `dis` |
| 5 | 启动游戏后又跳回桌面，前台变成 `cn.i4.mobile`（爱思助手） | 爱思助手随机自启抢前台 | `adb shell pm disable-user --user 0 cn.i4.mobile` |
| 6 | 游戏停在"正在释放神经递质(NNNMB/15077MB)"，看起来像卡死 | 2.7.91 首次安装要拉 ~15GB 资源（含语音包），本机约 3MB/s ≈ 1.5 小时 | 不是卡死，等；可以用 `adb shell cat /proc/net/dev` 看 wlan0 有没有在涨 |

### 出货读取（2026-11-03 定稿）

点完「聘用候选人」**不是**直接出货卡，中间还有一段黑箱装证件动画，整屏 OCR 只会拿到
`['SKIP','HUMAN','RESOURCE','DEPARTMENT']` —— 这就是 11-03 00:09「未识别出货干员」的根因。

现在的处理（`core/ui.py`）：

1. `claim_candidates()` 点「聘用候选人」后立刻返回，不再一次性点 SKIP；
2. `read_result(timeout=45)` **边点 SKIP 边轮询**：SKIP 还在 = 动画没完，点掉它；名牌出现就认名；
3. 整屏 OCR 漏字时，裁 `ui.result_name` 区域放大 2 倍再 OCR（艺术字名牌兜底）；
4. 干员名匹配只认中文文本，先把 `SKIP/HUMAN/123456` 这类噪声滤掉；
5. `close_result()` 用「公招列表页关键字」判定是否真的回到了列表，没回去就再点一次。

`tests/test_flow.py` 把这套状态链（`claim → anim(SKIP) → result`）固化成了回归测试，
`--dry-run` 的模拟器也照着真机改成三屏，改坏了会立刻红。

一条命令体检 + 自动写回推荐值：

```bash
python doctor.py                  # 第 8 节会直接报 renderer_mode / force_discrete_graphics / renderer_strategy
python doctor.py --fix-emulator   # 写回推荐值（写完要重启模拟器：MuMuManager control -v 0 shutdown / launch）
```

重启模拟器只在命令行做，别点 MuMu 窗口的关闭按钮（弹确认框会卡住脚本）：

```bash
MM="D:/Program Files/Netease/MuMu/nx_main/MuMuManager.exe"
"$MM" control -v 0 shutdown && "$MM" control -v 0 launch
"$MM" info -v 0            # 看 is_android_started=true 再连 adb
```

崩溃取证（游戏崩了先抓这段，比瞎猜快）：

```bash
ADB="D:/Program Files/Netease/MuMu/nx_main/adb.exe"
"$ADB" -s 127.0.0.1:16384 logcat -d -b crash | grep -E "signal 11|Cause|#0[0-3] pc"
"$ADB" -s 127.0.0.1:16384 logcat -d -b events | grep -Ei "am_proc_died|am_crash"
```
