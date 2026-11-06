# AI_CONTEXT — 面向 AI / 后续开发者的上下文

这份文件给"接手这个代码库的 AI 或工程师"：不讲故事，只讲**结构、不变量、判据、扩展点和禁区**。
人类向的说明见 `README.md`；状态机耗时表见 `docs/STATE_MACHINE.md`。

---

## 0. 一句话

用"截图 + Windows 自带 OCR + 合成键鼠"驱动 Edge 完成 MOOC 视频与随堂测验的机器人；
**没有** WebDriver、没有网站接口、没有私有 token。

## 1. 运行前提（不满足则一切自动化静默失败）

> 面向人的完整"使用前准备"清单见 `README.md` 第 3 节。下面只列**会影响代码行为**的那些前提。

- Windows 10/11；Python ≥3.10；**仅 Pillow**（无 numpy）。
- 进程必须是**普通用户完整性（Medium）**。低完整性进程的 `SendInput` 会被 UIPI 静默丢弃；
  `injectable_at()` 会检测并在 `verified_click()` 里打印 `BLOCKED` 拒绝继续。
- Edge 必须存在且可被激活（`browser_window()` 按进程名 `msedge` + 窗口类找；`BROWSER_PROC` 可改）。
- **页面必须是 `studentstudy` 学习页**（左播放器 + 右目录），且三个页面标记可读
  （`PAGE_MARKERS`：章节详情 / 返回课程 / 任务点 / 倍数 / 目录）。
- **只开一个课程标签**：多标签会让"喇叭图标"槽位竞争，`audio_playing()` 会退化成 None
  （安全但失去主要判据，只剩时钟判据）。
- **关闭 Edge 的"睡眠标签页"**：挂起标签会冻结媒体，PLAY 会反复"续播"但任务点不前进。
- **运行期间不得锁屏/休眠**：截图会变成黑屏，`shot_is_browser()` 与所有 OCR 判据全部失效。
- 屏幕：任意分辨率，但所有几何均在运行时实测；`screen_calib.json` 是实测缓存（可删，会重测）。
- 一次只允许一个 runner（独占鼠标）。

## 2. 模块地图

| 文件 | 职责 | 关键符号 |
|---|---|---|
| `auto_course.py` | 主流程：会话、页面读取、几何测量、点击、`run()` | `capture_page()` `read_screen()` `video_rect()` `ensure_playing()` `advance_to()` `resolve_next()` `run()` |
| `runner_states.py` | 状态枚举 + 上下文 `Ctx` | `State` `Ctx`（`poll` `stuck_after` `help_timeout` `rejected` `play_attempts`） |
| `runner_states_impl.py` | 各状态实现 | `st_init` `st_observe` `st_play` `st_quiz` `st_verify` `st_advance` `st_stuck` |
| `supervise.py` | 监督 runner：崩溃重启、停滞检测、单实例锁、孤儿清理 | `pid_alive()` `kill_pid()`（**不要让两个 supervisor/runner 并存**） |
| `keeper.py` | 监督 supervisor（最外层） | `alive()` |
| `control.py` | 人机开关 | `status/stop/start/pending/reply`；`start` 会清 `stopped` 刹车 |
| `status.py` | 进度看板（读日志算速率/ETA/parked） | — |
| `verify_all.py` | **独立核验**：逐节读平台徽章 → `verify_all.json` | `--from 5.2` 断点续跑 |
| `grab.py` | GDI 截图、DPI 感知 | `grab(path, box)` |
| `ocr_win.ps1` / `ocr_click.py` | Windows.Media.Ocr 封装 | `run_ocr(img, out)` → `{"words":[{text,x,y,w,h}]}` |
| `mouse_click.py` / `keyboard_type.py` / `win_focus.py` | 合成输入与窗口管理 | `click()` `scroll()` `chord()` `type_text()` `paste()` `activate()` |
| `option_discs.py` | 选项圆点定位（像素扫描） | `option_discs()` |
| `answer_feedback.py` | 平台"回答正确/错误"像素判定 | `verdict_for(img, qu..., box)` |
| `llm_answer.py` | 答题模型（DeepSeek，可换 base/model） | `ask()` `api_key()`（`DEEPSEEK_API_KEY` 或 `~/.mooc_llm_key`） |
| `add_answer.py` / `answer_now.py` / `help_channel.py` / `waiter.py` | 答案缓存、手动补答、求助通道 | `remember()` `pending()` |
| `calibrate.py` / `ocr_find.py` | 布局标定 / OCR 调试 | — |

## 3. 状态机不变量（改代码前必读）

```
INIT -> OBSERVE -> {PLAY|QUIZ|ADVANCE|STUCK}
QUIZ -> VERIFY -> {OBSERVE|QUIZ|STUCK}
ADVANCE -> {OBSERVE|DONE|STUCK}
DONE / ABORT = 终止
```

1. **INIT 必刷新页面**（Ctrl+R）。刷新有两个已被实测的作用：把目录**重锚定**到当前小节；
   解开"点了播放也不动"的卡死播放器。但**视频进行中不要刷**（会重置播放器）——`st_advance`
   只在 `taskpoint_done()` 为真时才刷。
2. **OBSERVE 的判定顺序**：先看有没有题（结构性判据），再看任务点徽章，最后才决定播放。
   有题且已完成时**先答题**（这个平台每节 2 个任务点，题可能在视频结束时才弹出）。
3. **PLAY 的判定顺序**（顺序本身是安全属性，不要随意调换）：
   ① 标签喇叭说"在播" → 结束（绝不点击）；② 时间码可读 → **连读两次**，前进即在播；
   ③ 有播放按钮/时间码/喇叭说"暂停" → **点视频正中央**；④ 都判不出来 → 悬停底部条读时钟。
   点击目标永远是 `play_click_target(rect)`（播放器中心），**不要瞄准播放按钮**。
4. **QUIZ 的预算按"题"计**：`st_observe` 在题目文字变化时清零 `quiz_attempts` 和 `rejected`；
   不要在 `st_quiz` 里再加一个清零点。被平台判错的选项必须进 `rejected` 并传给模型。
   只有判定 correct 才允许写缓存。
5. **ADVANCE 的成功判据只有 `chapterId`**（配合 `chapter_ids.json`）。
   不要引入"标题匹配"之类的替代判据：`…（1）/（2）/（3）` 这类标题会让它把三节当成同一节。
6. **DONE 只由常量 `LAST_SECTION` 决定**。"目录里没有下一行"= 读不全 → STUCK，不是 DONE。
7. **STUCK 必须写清原因**（`ctx.stuck_reason`），并写求助请求 + 截图，否则没人能诊断。

## 4. 关键算法与判据

### 4.1 播放器几何
`video_rect(img)` → `measure_video_rect(im)`：把截图降采样成 8px 网格，找"非背景色的最大实心块"，
要求密度 ≥0.35、尺寸合理；与上一次的已知方框做**一致性校验**（比例 1.59x、inside=False 的候选
会被拒）。已知坑：页面滚动后测到的是**可见部分**（宽高比 ≠ 16:9），但中心仍在视频内，因此点中心安全。

`centre_play_button(img, rect)`：以方框中心为圆心，比较小圆盘与环形背景的亮度差与圆盘内对比度，
判据 `disc_mean - ring_mean > 10 且 disc_std > 8`（配色无关，实测覆盖白底讲义/灰底黑底）。
三条调阈值失败的历史写在函数注释里——**不要按调色板写死阈值**。

`find_timecode(words, within)`：先找含 `:` 的 token；找不到时把**同一视觉行**的数字碎片拼起来
（OCR 会把 `8:38/11:1` 拆成 `'8' '：' '38' '/' '11' '．' '1'`）。必须用 `within=<播放器方框>`
限制，否则会读到任务栏时钟。

### 4.2 喇叭图标（是否在播的直接信号）
`audio_playing()`：① 截图必须是浏览器（`shot_is_browser()` 读地址栏找 http/chaoxing）；
② 在标签栏找**强红像素**（`r>170, r-g>55, r-b>25`）的连续列，取宽度 ≥12px 的 run；
③ 从 run 的左缘按实测图标宽度（`0.0128 * 屏宽`）推出图标右缘，槽位取右缘 +2；
④ 槽位与两个真实参照（`testdata/tab_idle.png` / `tab_playing.png`）比平均绝对差，
   `best <= 24 且 (second-best) >= 8` 才算数；"idle" 结论必须 `d <= 12`（宁可返回 None）。
   返回 `True/False/None`；None 时调用方回退到时钟判据。

### 4.3 目录解析
`catalog_rows(words)`：先用 `catalog_num_column()` 找编号列（词云的 x 密集带），
再在带内取每行的编号 + 标题。`_merge_digits()` 处理 OCR 的四种形态：
`'1' '．' '4'`、`'31'`（无分隔符）、`'10'` + `'1'`（章号两位）、
`'1' '0' '，' '2'`（章号被拆成两个 token + 全角逗号 + 分隔符基线低 19px）。
`NUM_RE = ^(\d{1,2})\.(\d{1,2})$`。**章标题行（"10"）会被误读成 1.0**，属良性噪声
（都小于游标，不会被选中）。

### 4.4 导航校验
`click_catalog_row(row, label)`：`x = row["x0"] - 60px`（落在编号列左侧的行内区域），
并用 `verified_click()` 校验局部像素变化。注意：**局部无变化不代表没导航**（实测多次 0px 但
chapterId 已变），所以成功与否只看 `chapterId`。

`read_url_from_clipboard()`：`Ctrl+L` → `Ctrl+C` → `Get-Clipboard`，拿到**字节级精确**的 URL。
比 OCR 可靠（OCR 把 `chapterId` 读成 `chapterld`、`=` 读成全角 `：`）。

## 5. 数据文件（运行时生成，随机器/课程变化，**不要随包分发**）

| 文件 | 内容 | 备注 |
|---|---|---|
| `last_url.txt` | 课程 URL 模板（含 `enc`/`openc` 会话令牌） | **等同凭据**；`remember_url()` 只在通过 `_sanitize_url()` 校验时才覆盖 |
| `chapter_ids.json` | 小节号 → chapterId | 由导航成功时学习；跑 `verify_all.py` 会用剪贴板精确 id 修正它 |
| `answers_known.json` | 题目 → 平台认可的答案 | 只写 correct 的 |
| `screen_calib.json` | 本机布局实测 | 可删 |
| `auto_course_state.json` | `{cursor, section, stopped}` | `stopped` 是刹车：supervisor/keeper 见到就退出 |
| `auto_help/` | `request.json` / `waiting` / `reply.json` | 求助通道 |
| `_dbg/run-<pid>/` | 每次运行的截图 | 每次启动清空；单次可达上百 MB |

## 6. 测试

- `test_offline.py`：59 项回归。**策略：用真实失败帧做 fixture**（`testdata/`），
  不要用合成图替代像素判据。缺少 fixture 时打印 SKIP（不是 PASS）。
- `test_catalog.py`：目录编号解析的纯合成用例。
- `test_states.py` / `test_answer_priority.py`：需要真实页面的集成性检查（会动鼠标）。

改动的验收标准：`python test_offline.py` 全绿，**并且**在真实页面上观察到对应行为变化
（不要只看单测）。

## 7. 扩展点（换平台/换课程）

1. `LAST_SECTION`：这门课最后一节（唯一 DONE 判据）。
2. `BROWSER_PROC`：浏览器进程名。
3. 页面标记：`PAGE_MARKERS` / `DSH_MARKERS`（判断截图是哪个窗口的文本特征）。
4. `catalog_rows()` / `_merge_digits()`：编号提取规则。
5. `quiz_banner_y()` / `quiz_option_rows()` / `submit_point()`：题目结构与提交位置。
6. `video_rect()` / `centre_play_button()` / `find_timecode()` / `audio_playing()`：播放器几何与播放判据。
7. `taskpoint_done()`：平台"已完成"徽章的文本——**必须准**，它是唯一的完成判据。

## 8. 禁区（会直接造成事故）

1. **不要同时跑两个 runner**（会互抢鼠标、抢状态文件）。`supervise.py` 有启动锁，
   `control.py start` 要求无其它进程。
2. **不要用 `Get-Process python | Stop-Process` 清理进程**——这会把同机上别人的 Python 任务
   一起杀掉（本项目真实发生过）。按命令行过滤：

   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
     Where-Object { $_.CommandLine -match '\\(auto_course|supervise|keeper|waiter)\.py' } |
     ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
   ```
3. 不要拖进度条、不要倍速（平台禁止；而且违反作业目标）。
4. 不要用规则答题（"绝对化表述就是错"这类规则会答错）；答不出就求助。
5. 不要把 OCR 读到的 URL 当成真值（会被截断/误读），也不要让未经校验的 URL 覆盖 `last_url.txt`。
6. 不要在视频播放中途刷新页面（会重置播放器）。
7. 不要在低完整性进程里跑（点击会被 UIPI 静默丢弃）。

## 9. 排障顺序

1. `python status.py` —— 看 `last state`、`cursor`、`parked:`、速率/ETA、日志年龄。
2. `python control.py pending` —— 卡住时看求助原因（含截图路径）。
3. 看 `auto_course.log` 的**行号顺序**（该日志跨天追加且**没有日期**，`[04:55]` 可能是前一天）。
4. 看 `_dbg/run-<pid>/` 的截图（`sm.png` 是卡住那一帧）。
5. 要确认"到底有几节真完成"，跑 `python verify_all.py --from 5.2`，**不要**只看日志里的 DONE。
