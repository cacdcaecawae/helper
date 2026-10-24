# 此 fork 的界面修改

本 fork 保留原项目的原生消息桥接、账号隔离、插件、配置及 API，按 Claude 风格的聊天前端重新组织界面。

- 默认进入聊天页：左侧会话、右侧气泡与输入区，手机上采用列表／详情切换。
- 使用 helper 的真实消息缓存、会话 ID、发送 session 和任务状态，不接入模拟联系人或模拟回复。
- 原账号管理、消息监控、插件、群配置、模型及日志页面保留；管理页使用一致的灰白／微信绿配色。
- 发送前确认目标与正文；确认时复核账号、会话、连接 session、正文及艾特列表，取消不会提交请求。
- 网页草稿只在内存中按账号和会话分开保存，不写入服务器配置。
- 仅展示本次连接的已接收消息，不宣称完整聊天历史。头像为本地生成的标识。
- 原生层没有改动，界面修改不降低非官方注入或账号风险。

上游为 [Prometheus-first/helper](https://github.com/Prometheus-first/helper)。原项目 `LICENSE`、版权及第三方声明原样保留，本修改版继续受其非商业使用条款约束。

前端验收仅使用合成账号、消息及发送任务；不会启动微信或机器人。启动仍使用原有 `start_web.bat`，请先退出其他注入工具。

## 本机模型

- 大模型「接入方式」可选「本机 Codex 登录」（`llm_config.json` 中 `"provider": "codex"`）：每次调用读取 `~/.codex/auth.json`（或 `CODEX_HOME`）的 ChatGPT 登录，请求 Codex 后端，无需 API 密钥，接口地址和密钥栏会自动隐藏。模型从下拉框选择：Codex 读取本机 `models_cache.json`（Codex 自动维护），兼容接口请求标准 `/models`；取不到列表时可选「手动填写…」。不自行刷新 token，长期未用 Codex 导致 401 时运行一次 `codex` 即可。
- 语音默认使用本机 Qwen3-TTS（`F:\本地模型\Qwen3-TTS-12Hz-1.7B-CustomVoice`），默认音色 Vivian，固定随机种子保证同句同音。`tools/local_tts_server.py` 只监听 `127.0.0.1:8766`，实现 helper 用到的 Fish Audio 接口子集；按 `requirements-tts.txt` 建好 `.venv-tts` 后，`start_web` 会自动后台启动。RTX 4070 SUPER 上约 2.5 倍实时，故每段 80 字、超时 120 秒。

## 界面文件与验收

- `webui/index.html`：页面结构；`webui/style.css`：配色、布局与响应式；`webui/app.js`：现有 API 对接及聊天交互。
- 浏览器回归脚本：`python tools/check_chat_ui.py --screenshots`。需在开发环境安装 `playwright` Python 包并安装 Microsoft Edge，不是运行管理台所需依赖。
- 脚本在随机本地端口提供合成 API，检查确认／取消发送、真实艾特参数结构、会话草稿隔离、过期确认拦截、消息转义、管理页导航及手机／暗色布局。截图写入已忽略的 `.runtime/ui-check/`。
