# 操作手册（醒来先看这一份）

三层进程，互不依赖，任何一层死了都由上一层补齐：

| 层 | 作用 | 什么时候会退出 |
|---|---|---|
| `auto_course.py`（runner） | 干活：看状态 → 播视频 → 答题 → 验证 → 进下一节 | 到 `LAST_SECTION` 判 DONE（退出码 0）；或停在 STUCK 等回复 |
| `supervise.py`（supervisor） | runner 死了就重启；求助等待期间不杀它 | 状态文件出现 `stopped`（done / stuck） |
| `keeper.py` | supervisor 死了就拉起来（默认 14 小时、最多 24 次） | 看到 `stopped=done` |

## 一条命令看进度

```powershell
python status.py
```

输出示例（含速率和 ETA，都是从这个日志算出来的真实值）：

```
last state : PLAY
advanced   : 7.2, ..., 8.3, 8.4
answers    : 39 verdicts, 7 wrong
rate       : 17 sections logged, 26 min/section (5.3 .. 8.4)
to 20.5    : ~62 sections -> ETA ~27h
state file : cursor='8.4' stopped=None
```

`python status.py --tail 30` 会附上最近的日志行。

## 进程与启停

```powershell
python control.py status     # 看 supervisor / runner / waiter 的 pid 和游标
python control.py stop       # 干净停止（写 stopped，supervisor 随之退出）
python control.py start      # 重新开始（从状态文件里的游标继续）
```

看护层的重启（keeper 默认只跑 14 小时，课程要一天多，所以要手动续上）：

```powershell
python keeper.py --interval 300 --max-hours 26      # supervisor 死了就拉起来，跑 26 小时
```

## 它停下来了怎么办

1. `python control.py pending` —— 看它卡在什么原因（会打印完整的求助请求，含状态、游标、截图路径）。
2. 截图在请求里给的路径（`_dbg/run-<pid>/sm.png`），先看图再动手。
3. 处理完让它继续：

```powershell
python control.py reply continue        # 直接继续（它会重新读页面）
python control.py reply click x=1480 y=1328   # 替它点一下
python control.py reply skip            # 跳过当前小节
python control.py reply abort           # 结束整个运行
```

（`reply` 可用的动作：`continue` / `click x= y=` / `scroll n=` / `skip` / `abort`，见
`auto_course.apply_help_reply`。）

## 想改终点

`auto_course.py` 顶部一行：

```python
LAST_SECTION = "20.5"      # ← 改成 "9.5" 就只刷到第 9 章第 5 节
```

改完重启 runner 生效（`control.py stop` 再 `start`，或直接杀掉 runner 让 supervisor 拉起）。

## 日志在哪

| 文件 | 内容 |
|---|---|
| `auto_course.log` | 主日志：每个状态迁移、每次点击、每次求助 |
| `keeper.log` | 看护层做过什么（拉起 supervisor 之类） |
| `sup.out` / `sup.err` | supervisor 的 stdout/stderr |
| `auto_course_state.json` | 游标与 `stopped` 标记（判断"是否结束"的权威来源） |
| `auto_help/request.json` | 未处理的求助请求 |
| `_dbg/run-<pid>/` | 每次运行的截图（每次启动会清理旧的） |

## 已知的物理成本（不是 bug）

* 每节视频约 12 分钟，且平台要求 **90% 观看时长**，所以 20 分钟/节是下限；
  中途出错、答题重试、跨章导航会把它推到 ~26 分钟/节。
* 整门课到 20.5 还需要 **20 小时以上**（见 `status.py` 的 ETA）。
* 你在电脑上操作会让 DSH 抢前台 → 平台暂停视频；runner 在一个轮询周期内自动点中央续播，
  但这段暂停时间不计入观看时长。
