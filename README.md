# 明日方舟 · 自动公招（Arknights Auto Recruit）

> 读屏 → OCR 认词条 → 决策 → 挂 9 小时 → 加急领取 → 记账/推送，全自动跑公招。

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)](#)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

支持**官方 PC 客户端**（推荐）和**安卓模拟器 / 手机（ADB）**两种后端，同一套流程代码，靠 `config.yaml` 切换。

---

## 它做什么

一轮完整流程：

1. 进「公开招募」列表页，读 4 个槽位的状态（空位 / 招募中 / 待领取）；
2. 空位逐个开面板，读 5 条词条 → 决策引擎算最优组合 → 选词条 → 设 9 小时 → 确认；
3. 四格挂满后逐个用**加急许可**立即完成 → 点「聘用候选人」→ 跳过动画 → 读出货星级/名字；
4. 记账（`logs/history.jsonl`）+ 达到星级阈值就发邮件；
5. 按配置继续下一轮，直到没许可 / 没空位 / 你点停止。

### 决策规则（默认 `basic`）

| 词条 | 选择 |
|---|---|
| 高级资深干员 | 只选它 → 6★ |
| 资深干员 | 只选它 → 5★ |
| 其余组合 | 算池子，选出**能保底 4★** 的最优组合（如「召唤」「削弱」「狙击干员+削弱」） |
| 都没有 / 只剩降星词条（新手/元素/支援机械） | **一条都不选**，9h 直接招募 → 3★~4★ 满池随机 |

> 「一条都不选」是刻意设计：选普通词条只会把池子收窄，最好的情况是星级不变、最坏是把上限压到 1~2★。

---

## 主要特性

- **两种后端**：官方 PC 客户端（mss 抓屏 7~19ms、SendInput 点击）/ 模拟器・手机（ADB）。
- **双速路径**：固定裁块单行识别（≈60ms）为主，识别不出才退回整屏 OCR（4~6s）。
- **两种打法**：`batch` 四格先全挂满再逐个领（默认，省面板切换）/ `standard` 一槽到底。
- **自愈**：ADB 掉线重连、游戏掉到后台自动拉起、误入寻访/基建自动退出、弹窗逐个清掉。
- **安全闸**：点之前必须确认在公招列表页；认不出界面就不点；「停止招募」确认框只会点「取消」。
- **离线可测**：`--dry-run` 走内置模拟屏幕，42 个离线测试用例，不碰真机。
- **数据可更新**：词条/干员库来自 MAA，自带 `tools/import_maa.py` 一键同步。

---

## 快速开始

### 环境要求

- Windows 10/11
- Python **3.10+**（源码运行）
- 官方 PC 客户端，或 MuMu / 雷电 / 夜神 等模拟器（Android 7+）

### 方式一：直接下 exe

```text
1. 从 Releases 下载「明日方舟公招面板」压缩包（没有 Release 就自己打包，见下）
2. 解压到任意目录，双击 明日方舟公招面板.exe
3. 首次启动会在 exe 旁边自动生成 config.yaml / data/ / logs/
4. 游戏里把窗口设成 1600x900（窗口化或无边框都可以）
```

自己打包（Windows，需 Python 3.10+）：

```bat
pip install -r requirements-dev.txt
打包exe.bat
:: 产物在 dist\明日方舟公招面板```

> 游戏客户端通常是**管理员权限**启动的。若面板点不动、抢不到焦点，点面板左下角「以管理员重启」。

### 方式二：源码运行

```bash
git clone https://github.com/rainyao15/arknights-auto-recruit.git
cd arknights-auto-recruit
pip install -r requirements.txt
python gui.py            # 面板
python main.py           # 命令行
```

### 自检（先跑这个，别一上来就跑主流程）

```bash
python doctor.py                 # 环境体检：adb / 设备 / 分辨率 / 依赖
python pc_selftest.py            # PC 端：开个窗口按屏幕坐标点 4 个按钮，验证坐标与注入方式
python pc_probe.py --flow-check  # PC 端：只读跑一轮（进公招→开槽→读词条→决策→取消，不消耗）
python pc_wait_flow.py 12        # PC 端：守候式，等你回大厅再自动跑上面那条
python main.py --dry-run --once  # 离线模拟，完全不碰游戏
```

---

## 分辨率支持

**不是只能 1600×900。** 程序里所有坐标都是**比例（0~1）**，运行时乘实际分辨率，所以任意分辨率都能跑；影响的是「快不快」和「要不要重标」。

| 分辨率 | 能否运行 | 极速路径（每格 1.5s） | 说明 |
|---|---|---|---|
| **1600×900**（16:9） | ✅ | ✅ 默认直接可用 | 所有配置、裁剪读带都是按这个尺寸标定的，推荐 |
| 1280×720 / 1920×1080 / 2560×1440（16:9） | ✅ | 需显式开启 | 等比缩放，比例坐标仍对得上；把 `ui.fast.allow_any_16_9: true` 打开即可尝试（个别客户端会加黑边，先跑 `pc_probe.py --flow-check` 验证） |
| 16:10 / 21:9 / 4:3 / 竖屏 | ✅ | ❌ | 画面比例变了，元素位置不再等比 → 必须用 `calibrate.py` 重标 `ui.slots` 等坐标；极速读带也要重标 |
| 模拟器 / 手机任意分辨率 | ✅ | 视情况 | 同上：同 16:9 可开 `allow_any_16_9`，否则走慢速 |

两条开关：

```yaml
ui:
  fast:
    at: [1600, 900]          # 极速路径的标定分辨率
    allow_any_16_9: false    # true = 允许其它 16:9 分辨率也走极速路径（默认关）
screen:
  auto_resolution: true      # 运行时用截图尺寸自动判定，不用手填
```

不开极速路径时**功能完全一样**，只是每格从约 1.5 秒变成 5~10 秒（退回整屏 OCR）。PC 端最省事的做法仍然是把**客户区**设成 1600×900（窗口化或无边框都行）。

## 配置

所有行为集中在 `config.yaml`，常用几项：

```yaml
backend: auto            # auto | pc | adb

game:
  max_slots: 4           # 一轮处理几个公招位
  flow_mode: batch        # batch=先挂满再领；standard=一槽到底
  recruit_time_hours: 9   # 固定 9 小时（星级区间最高档）
  use_expedite: true      # 用加急许可立即完成
  on_expedite_exhausted: natural   # 加急用完：stop 停止 / natural 转自然招募
  loop_interval_min: 0    # 轮间隔分钟，0=跑完立刻下一轮

decision:
  mode: basic             # basic=保底优先；lock=锁定特定干员
  fallback: none          # 无保底组合时：none 不选词条 / best_tag 选一个不降星的

ocr:
  engine: rapidocr
  match_threshold: 0.7    # 词条模糊匹配阈值

notify:
  enabled: true
  smtp_user: "your_qq_number@qq.com"   # QQ 邮箱
  smtp_password: ""                    # QQ 邮箱 SMTP 授权码（不是登录密码）
  notify_min_rarity: 4                 # 只在 4★ 及以上推送
```

其余坐标类配置（`ui.*`）是给 1600x900 标定好的，换分辨率请用 `calibrate.py` 重标。

---

## 使用

### 面板（GUI）

详细操作、按钮含义、常见坑都在 **[操作须知.md](操作须知.md)**。

### 命令行

```bash
python main.py                    # 默认：批量挂单，按 config 循环
python main.py --once             # 只跑一轮
python main.py --cycles 3         # 最多 3 轮
python main.py --slots 2          # 只处理前 2 个公招位
python main.py --mode standard    # 一槽到底
python main.py --interval 5       # 轮间隔 5 分钟
python main.py --dry-run          # 离线模拟（不连设备、不发邮件）
```

### 更新词条库

```bash
python tools/import_maa.py --check     # 体检本地数据
python tools/import_maa.py             # 从 MAA 拉最新（有差异才写，自动备份）
python tools/import_maa.py --dry-run   # 只比对不写
```

数据是运行时读取的，**更新后重开面板即可生效，不用重新打包**。

---

## 工作原理

```
              ┌─────────────────────────── core/runner.py ───────────────────────────┐
 配置/设备 →  │ run() → run_cycle() → place_slot() / claim_slot() → 记账 / 推送        │
              └───────────────┬───────────────────────────────┬──────────────────────┘
                              │                               │
                     core/ui.py（感知 + 点击）        core/decision.py（纯函数）
                              │                               │
              ┌───────────────┴───────────────┐       词条 → 组合 + 人话理由
        core/ocr.py                      core/device.py
   FastOcr 60ms / 全屏 4~6s          adb_client / pc_client
```

- `screen_kind()` 用小裁块把界面分成 `list / panel / dialog / result / lobby / base / anim / headhunt / other`；
- **认不出来就返回 `other`，绝不伪造结论**；每个动作前都先确认界面，宁可停下也不乱点；
- 出货卡识别用「右侧奖励栏 / 职业英文行 / 卡面名牌 / XX的信物」多证据，避免把卡点穿。

更细的踩坑记录（PC 迁移、坐标偏差、弹窗、性能对照）见 [docs/开发记录.md](docs/开发记录.md) 和 [流程与点击次数.md](流程与点击次数.md)。

---

## 目录结构

```
core/            # 核心逻辑
  ui.py          #   界面识别与点击（所有像素/鼠标行为都在这）
  runner.py      #   单格/单轮/整局的编排、失败分级、自愈
  decision.py    #   词条决策（纯函数，可单测）
  tag_recognizer.py # 词条模糊匹配（含两字词条容错）
  ocr.py         #   两档 OCR：FastOcr（60ms）/ 整屏（4~6s）
  adb_client.py  #   模拟器/手机后端
  pc_client.py   #   官方 PC 客户端后端（抓屏 + SendInput + 焦点管理）
  device.py      #   后端工厂（auto/pc/adb）
  stats.py       #   出货记账、票据折算
  notifier.py    #   邮件推送
  simulator.py   #   离线模拟屏幕（dry-run 用）
tools/import_maa.py   # 词条库同步工具
tests/                # 42 个离线用例（不碰真机）
config.yaml           # 全部配置
gui.py / main.py      # 面板 / 命令行入口
```

---

## 常见问题

| 症状 | 原因 / 处理 |
|---|---|
| 点了没反应、日志却正常 | 游戏是管理员启动的 → 面板点「以管理员重启」 |
| 截图全黑 / 拍到别的窗口 | PC 端用 mss 抓**屏幕**，游戏必须在前台，别被其它窗口盖住 |
| 一直走慢速、很卡 | 客户区不是 1600x900，`ui.fast.at` 对不上 → 调窗口、开 `allow_any_16_9`，或重标 |
| 坐标全变 -32000 | 窗口被最小化，客户区不存在 → 还原窗口 |
| 出货卡没读到名字 | 多为动画/介绍页挡住，脚本会点 SKIP/快进；连续失败会在 `logs/` 留截图 |
| 邮件不发 | `notify.smtp_password` 是空 → 填 QQ 邮箱 SMTP 授权码 |

---

## 开发与测试

```bash
python -m pytest tests -q        # 42 个离线用例（决策 / 词条 / 流程 / 领取 / 数据体检）
python main.py --dry-run --once  # 端到端跑一遍模拟
打包exe.bat                      # PyInstaller 打包（产物在 dist/）
```

---

## 免责声明

- 本项目仅用于个人学习与自动化研究，**与游戏官方无关**。
- 使用自动化脚本可能违反游戏用户协议，存在账号风险，**请自行评估并承担后果**。
- 所有决策逻辑基于公开数据推导，不修改游戏文件、不读写游戏内存。

## License

[MIT](LICENSE)
