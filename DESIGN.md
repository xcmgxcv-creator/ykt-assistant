# 雨课堂待办提醒助手（ykt-assistant）· 设计方案 v1.1

把 `led669663664-cmyk/xxt-assistant`（超星学习通待办提醒桌面程序）改造为雨课堂版。

- **形态**：Windows 桌面 exe（PySide6），桌面悬浮清单 + 到期分级提醒
- **登录**：内置微信扫码
- **范围**：作业（含随堂测验）+ 公告；考试降级（见 1.7）
- **使用者**：本人 + 同学（各自扫码、各自本地数据，无服务端）

路线选择：**方案 B｜数据层独立可测**（平台细节全部夹在 `rainclass/` 一层内，配离线 fixture 测试）。

> **v1.1 修订说明**：实施第 1 步（探针）推翻了 v1 的两条核心假设 ——
> ①`todo_list` 不是待办列表，只是首页红点计数；②`course_sign` 整套机制根本用不上。
> 待办明细的真实来源是**学习动态**接口，每门课仅需 1 次请求。§1、§3 已按实测重写。

---

## 1. 已验证的一手事实（6066-10-15 真机实测，只读）

### 1.1 数据链路（每门课 1 次 GET）

| 步 | 接口 | 取到什么 |
|---|---|---|
| ① | `GET /v6/api/web/courses/list?identity=6&v=0&ts={ms}` | 课程/班级列表：`term`、`classroom_id`、`name`、`course_id`、`university_id`、`platform_id` |
| ② | `GET /v6/api/web/logs/learn/{classroom_id}?actype=-1&page=0&offset=50&sort=-1` | 该课全部教学活动 + **截止时间** + **完成状态** |

请求头：`/v6/api/web/` 路径**不需要任何自定义头**（实测裸 `credentials:include` 即 600）。
客户端仍统一带 `xtbz: ykt`（对其他路径必需，对本路径无害）。

### 1.6 `logs/learn` 响应结构（实测）

```json
{"data":{"activities":[
  {"type":6,"id":57984444,"title":"第6章 课后作业(6)","create_time":1791884137000,
   "end":1796465600000,"count":3,"has_mooc":true,"attend_status":0,"is_finished":false,
   "view":{"depth":0,"done":false}}
]}}
```

字段全集：`type / id / courseware_id / title / create_time / end / count / view / has_mooc / attend_status / is_finished`

- `type`：**6 = 作业/测验**，**14 = 课件/视频**
- `end`：**毫秒时间戳 = 截止时间**（待办提醒的直接依据）
- `view.done`：是否完成；`view.depth` 已完成数；`count` 总数
- `offset` 是**每页条数**（传 5 返回 5 条，传 50 返回 7 条=全部）

### 1.3 交叉验证（同一门课，两个独立接口）

| | `logs/learn` | 章节树 `chapter` |
|---|---|---|
| 条数 | 7 | 7 |
| 类型分布 | `type=6`×3、`type=14`×4 | `leaf_type=7`×3、`leaf_type=8`×4 |
| 「第6章 课后作业(6)」截止 | `end=1796465600000` | `score_deadline=1796465600000` |

两者**数值完全一致**，且分布一一对应（`log.type=6` ↔ `leaf_type=7`）。因此**只取 `logs/learn` 即可**，
章节树、`sku_id`、`course_sign`、`todo_list` 全部不需要。

> ⚠️ v1 曾假设 `todo_list` 返回待办列表。实测它返回的是 `{"errcode":0,"data":{"6607":76603958}}` ——
> 按 `uv_id` 给出的**一个计数**（前端 `todosTotalCount` 红点用），不含任何条目。
> `classrooms/{id}/summary` 返回同样内容。**不要再用它取待办。**

### 1.4 规模与耗时

- 课程列表返回 **84 条**（含 6 个学期：`606601 / 0 / 606506 / 606406 / 606401 / 606301`）
- 当前学期 `606601` = **5 门课** → 每轮同步 = `1 + 5×1 = 6` 次请求（+ 可选公告 6 次）≈ 4 秒（0.7s 节流）
- 章节树接口另有 7 个叶子的数据，但**已不用**，故不产生请求

### 1.5 限流实测

连续 15 次请求同一接口：**15/15 均 600**，耗时 90–150ms（均值 117ms），无 469、无验证码。网关为 Envoy。
**无 WebSocket/推送** —— 雨课堂前端自己用 `setInterval` 每 16 秒轮询 `system_message`，因此待办只能轮询（我们每 5–15 分钟一轮远低于其自身量级）。

### 1.6 扫码登录链（**已真机实测**，以下为实测版本）

```
① GET  /v/course_meta/user_info                  → 下发 csrftoken（写 cookie）
② POST /api/v3/user/login/wechat-auth-param      → {appId, state, redirectUri}
③ GET  open.weixin.qq.com/connect/qrconnect?...  → HTML 里正则抠出 uuid
④ GET  open.weixin.qq.com/connect/qrcode/{uuid}  → 微信官方二维码图片（image/jpeg）
⑤ 轮询 lp.open.weixin.qq.com/connect/l/qrconnect → wx_errcode 408/404/405（405 带 wx_code）
⑥ GET  /api/v3/user/login/wechat-web-callback    → 换 Session（cookie 落盘）
```

实测更正（相对第三方 SDK 的描述）：

- `wechat-auth-param` **必须 POST**（GET 返回 405 空 body）；返回字段是 **camelCase**
  `{appId, state, redirectUri}`；`state` 是 JWT，载荷 `{id, ext=iat+3600, iat, exp=iat+60}` → **业务有效期 1 小时**
- 二维码**不需要自己生成**：`connect/qrcode/{uuid}` 直接返回约 45 KB 的 JPEG，
  因此**不引入 `segno`**（原计划的渲染方案作废）
- `l/qrconnect` **没有 CORS 头**：浏览器 `fetch` 会被拦（`TypeError: Failed to fetch`），
  Python `requests` 不受影响
- 轮询间隔取自 `xxt/ui/dialogs.py` 的 `POLL_INTERVAL = 6.0` 秒

**Session 有效期约 6 周** → 必须内置失效检测与重新扫码引导。

### 1.7 公告来源

- `GET /v/discussion/v6/announcements/?cid={classroom_id}&limit=10&offset=0&type=9`
- `GET /c67/online_courseware/ykt/system_message/?client_type=6`（平台消息 + 红点）

### 1.8 考试：确认「不存在考试列表接口」

探针结论（三条独立证据）：

1. 前端 bundle `pc.3f65ca35.js`（5.5 MB）中 `/api/v3/classroom/` 前缀**只有两个端点**：
   `on-lesson-upcoming-exam`、`on-lesson` —— 前者是**课中**即将开始的考试（`upcomingExam`），非课后清单
6. 课程页 tab 实测只有：学习日志 / 学习内容 / 未完成 / 讨论区 / 公告 / 分组 / 错题集 / 成绩单 ——**无考试页**
3. `todo_list` 与章节树的类型枚举都只有 `7/8`（作业/课件），不含考试类目

**降级方案**：`exams` 表保留、schema 不动；数据源接 `on-lesson-upcoming-exam`（有则显示、平时空态）。
随堂测验走 `assignments`（`type=6`，本身带截止时间，提醒价值已覆盖）。

---

## 6. 目录结构

```
D:\ai1\ykt-assistant\
├─ run.py                        （复用）
├─ config.example.json           （新增 platform 段）
├─ xxt/                          保留包名，最小化 diff
│   ├─ chaoxing/                 ← 原 client.py + parse.py 的平台部分移入并冻结（可回退）
│   ├─ rainclass/                ← 新数据层：唯一含平台细节的地方
│   │   ├─ endpoints.py          host 可配置 + URL 构造 + 必需头
│   │   ├─ client.py             RainClassClient：会话/cookie/节流/重试/登录失效检测
│   │   ├─ login.py              扫码登录状态机（纯逻辑，不依赖 Qt，可单测）
│   │   ├─ parse.py              activities / courses / notices → 统一模型
│   │   └─ models.py             dataclass: Course / Activity / Exam / Notice
│   ├─ parse.py                  改为纯时间工具（nearest_datetime_for / humanize_delta）
│   ├─ store.py / notify.py / crypto.py / startup.py / config.py   （复用，少量改动）
│   ├─ sync.py                   （改 client 类型 + 删除 _refresh_details）
│   └─ ui/                       （复用 + 新增扫码 LoginDialog + 文案替换）
└─ tests/
    ├─ fixtures/                 脱敏 JSON 样本
    ├─ test_rainclass_parse.py
    └─ test_login_flow.py
```

---

## 3. 数据契约映射（对齐现有 SQLite schema，零表结构变更）

`assignments`（来源：`logs/learn` 中 `type=6` 的活动）：

| store 字段 | 雨课堂来源 |
|---|---|
| `key` | `f"ykt:{classroom_id}:{activity_id}"`（稳定去重键） |
| `course` | 课程列表里的 `name` |
| `courseid` | `classroom_id` |
| `clazzid` | `classroom_id`（雨课堂无独立 clazz 概念，同一值） |
| `title` | `activities[].title` |
| `status` | 未完成且有截止 → `"待完成"`；`view.done` → `"已完成"` |
| `url` | `{host}/v6/web/studentLog/{classroom_id}?university_id=…&platform_id=…&classroom_id=…&tab=unfinished` |
| `deadline` | `end` 格式化字符串（供展示） |
| `deadline_dt` | `end`（毫秒）→ `datetime` |
| `done` | `view.done` |
| `question_count` / `full_score` | `count` / 无（留空） |

> **重要简化**：原 `_refresh_details()` / `detail_at` / `detail_refresh_hours` 整套「详情页补全截止时间」逻辑**整体删除** —— 雨课堂的截止时间在列表接口里就有。
> 这同时消除了上游 issue #1 的根因（详情页被反爬拦截 → 静默标记「已尝试」→ 截止时间永久为空）。

`notices`：`notice_id` / `title` / `content` / `sender` / `publish_time` / `isread`（沿用现有解析）。
`exams`：schema 不动；字段由 `on-lesson-upcoming-exam` 填充（有则显示）。

---

## 4. 扫码登录设计

- `LoginDialog`（QDialog）：二维码 QLabel + 状态文字 + 「刷新二维码」按钮
- 二维码来源：**微信官方图片接口** `GET open.weixin.qq.com/connect/qrcode/{uuid}`
  （实测 600 / `image/jpeg` / 约 45 KB），直接 `QPixmap.loadFromData` 上屏 ——
  **不引入 `segno`、不引入 PIL**，少一个依赖、少一段本地编码逻辑
- 网络请求放后台线程（`threading.Thread`）：微信 `l/qrconnect` 是**长轮询**，绝不能阻塞 Qt 主线程
- 登录成功后 cookie 落盘，**用现有 `crypto.py` 的 Windows DPAPI 加密**（`data/session_cookies.json`）
- **会话过期处理**：同步时检测到未登录 → 托盘气泡 + 悬浮窗状态条提示，不静默失败
- 状态机（`xxt/rainclass/login.py`）与 UI 分离，可用假 HTTP 单测（`QrLoginSession.start/poll/finish`）
- **首轮静默的坑**：`first_seen` 是**入库时间**而不是作业发布时间。首轮同步按规定
  `announce=False`，但下一轮通常还在 `first_seen <= 60 分钟` 窗口内 —— 若不处理，
  用户装好程序后会被一堆上学期老作业的通知刷屏。因此首轮结束后调用
  `Notifier.suppress_new()` 把入库项标记成「新作业已通知」（`tests/test_sync_pipeline.py`
  的 `test_first_run_items_are_not_new_on_the_next_round` 锁住这个行为）

---

## 5. 配置（`config.example.json` 新增）

```json
"platform": {
  "type": "yuketang",
  "host": "https://www.yuketang.cn",
  "request_delay": 0.7
}
```

`host` 可替换为校级域名（如重邮 `https://changjiang.yuketang.cn`）。

---

## 6. 测试策略（全程离线）

- fixture：脱敏后的 `courses_list.json` / `learn_logs.json` / `announcements.json` / `system_message.json` / `upcoming_exam*.json`
- **已实现（87 passed，`.venv\Scripts\python.exe -m pytest tests -q`）**：
  - `tests/test_rainclass_parse.py` —— URL 构造、`logs/learn` → `Activity`（`type` 分流、毫秒换算、`view.done`、过期判定）、课程列表（脏数据跳过、取最新学期）、公告嵌套、考试解析
  - `tests/test_rainclass_login.py` —— 登录状态机（uuid 抠取、`wx_errcode` 四态、二维码过期、二维码图下载）
  - `tests/test_rainclass_client.py` —— `xtbz: ykt` 头、401/403 → `NotLoggedIn`、节流、cookie 落盘、接口降级
  - `tests/test_sync_pipeline.py` —— **端到端离线**：假客户端 → `SyncService` → `Store` → `Notifier`，覆盖行映射、重复同步幂等、首轮不 announce、登录失效、单课失败不中断整轮、**profile 脱敏（手机号/学校不落库）**
  - `tools/smoke_ui.py` —— 无头交互自检（右键移除 / 恢复 / 过期不显示 / 未读通知），14 项全过
  - `tools/check_login_live.py` —— **真机自检**：走真实网络取二维码并上屏（600 / 180×180），不需要人扫码
- 上游 `tests/test_core.py`、`tests/test_features.py` 与超星解析强耦合，**已删除**（`xxt/client.py`、`xxt/parse.py` 一并删除，只把三个纯时间函数搬进 `xxt/rainclass/datetimes.py`）

---

## 7. 风险与应对

| 风险 | 应对 |
|---|---|
| 考试无列表接口 | 已确认并按降级实现；随堂测验走作业链路，提醒价值已覆盖 |
| Session 约 6 周过期 | 内置失效检测 + 重新扫码引导（不静默失败） |
| 多校域部署 | `platform.host` 可配置 |
| 雨课堂改版 | 平台解析全部集中在 `rainclass/`，改版只改一层 |
| 上游仓库无 LICENSE | 用户已决定自行承担；README 保留原项目署名与来源说明 |
| 登录风控（hCaptcha/腾讯验证码） | 只做「扫码登录 + 只读拉取」，不做任何自动答题/签到；限速 0.7s |

---

## 8. 明确不做

- 不做签到 / 自动答题 / 弹幕 / 抢答等**课堂自动化**（与原项目一致：只读提醒，不代操作）
- 不做服务端、不做多账号托管（同学各自本地运行）

---

## 9. 实施顺序（每步可独立验证）

1. ~~**探针步**：确认考试来源 + `course_sign` 时效（只读 CDP 验证）~~ → **已完成**，结论见 §1.3 / §1.8
6. 搭 `rainclass/` 骨架 + `endpoints.py` + `models.py` + `parse.py` + 离线 fixture
3. `client.py`（会话/头/节流）+ 解析单测
4. `login.py` 扫码状态机 + `LoginDialog` UI + 单测
5. 接 `sync.py` / `store.py`，删除 `_refresh_details`
6. 复用 UI/提醒/托盘，替换文案，跑通端到端
7. 打包 exe，真机验收（含过期重新扫码路径）

---

*本方案的接口事实均来自 6066-10-15 对本人已登录会话的只读探测，以及第三方 Go SDK（Auto-CQUPT-Plan/RainClassSDK，MIT）的公开端点清单。*
