# ghs — 会告诉你「这个仓库到底能不能用」的 GitHub 搜索工具

在 GitHub 上找到仓库很容易，判断它**能不能真的用起来**很难：没写 license、三年没推送、README 里从头到尾没提过 Windows。

`ghs` 是一个单文件、纯标准库的命令行工具。搜索之后，它给每个结果打一个 0–100 的健康分，依据是三件你在 `git clone` 之前就该知道的事：

- **它还活着吗** —— 推送时间、是否归档、有无 license、是否 fork、issue 积压
- **能在我的机器上跑吗** —— 从 README 里扒平台线索（有 winget / scoop / .exe，还是写着 "macOS only"）
- **作者还认这个项目吗** —— README 里明写"不再维护"会压过最近的提交时间，因为作者确实会顺手改文档

零依赖，不需要 API key，不需要 pip 安装。

## 安装

```bash
curl -O https://raw.githubusercontent.com/<you>/gh-search-cli/main/ghs.py
python ghs.py search "json diff" --limit 5
```

需要 Python 3.8+，Windows / macOS / Linux 都行。

## 用法

```bash
# 搜索
ghs search "pdf table extract"
ghs search "epub to markdown" --lang python --min-stars 500 --pushed-after 2025-01-01
ghs search "screen annotation" --limit 20 --json

# 看单个仓库：统计、license、README 摘要、安装线索
ghs repo owner/name

# token（匿名只有约 10 次/分钟）
ghs token set
ghs token show
ghs token clear
```

搜索没结果时，工具会自动放宽查询（先去掉限定符，再去掉停用词）重试，而不是直接打印零条。

token 存放位置：Windows 是 `%APPDATA%\gh-search\token`，其他系统是 `~/.config/gh-search/token`，权限 `0600`。**token 只发给 `api.github.com`。**

## 打分规则

故意做得简单、可审计 —— 这是启发式，不是判决。

| 项目 | 分值 |
|---|---|
| 热度 | `log10(stars) * 15`，上限 45 |
| 90 天内有推送 | +30 |
| 365 天内 | +18 |
| 730 天内 | +6 |
| 已归档 | -25 |
| 有明确 license | +12 |
| 是 fork | -15 |

README 里出现"不再维护 / deprecated / no longer maintained"，分数封顶 45，不管最近提交多新。

## 已知限制

- **只做关键字搜索**，走的是 repository search API，不覆盖代码搜索
- **分数是启发式**：标了 `no-license` 的仓库自己用可能完全没问题，具体去看 `ghs repo` 里的 license 段
- **平台线索是从 README 正则猜的**，项目支持 Windows 但没写，就会漏判
- **查询太窄时注意 `--no-relax`**：自动放宽可能带回不相关的结果
- **速率限制是真的**：匿名约 10 次搜索/分钟，批量用请配 token
- **没有缓存**，每次调用都走 API

## 许可证

MIT。
