# 月夜宠物 · tsukiyo_pet

把 AstrBot 接成一只住在 Windows 桌面上的透明桌宠：**桌宠是身体，AstrBot 是大脑**。
QQ 聊天的回复会同步成气泡，戳它、拖它、让它做动作都由模型决定。可选接 GPT-SoVITS，让它**用爱弥斯的声音说话**。

- 跨平台解耦：桌宠客户端（PySide6）与插件（AstrBot）只通过 HTTP 通信，**AstrBot 跑 Docker 也能用**
- 平台无关：`aiocqhttp`（NapCat）和 `qq_official`（官方机器人）都支持，主人 ID 用字符串比对（openid 也行）
- 零 subprocess：插件不再托管 GUI 进程，客户端自己连上来
- 可选 TTS：文本 → GPT-SoVITS → 气泡 + 语音同步

## 当前状态（2026-11-03 暂停）

**已经能用**：气泡同步（含机器人自己发语音的情况）、动作/情绪控制、TTS 语音、呼吸/眨眼/阴影/拖动惯性的程序化动效。

**还没做**（按优先级）：
1. **分发**：客户端打包成单文件 exe（PyInstaller）+ `--autostart` 开机自启 + 插件 `/pet client` 给下载地址。完成后用户是"装插件 → 拿 exe → 双击"，不需要 Python 环境
2. **素材升级（路线 B）**：用 ComfyUI 生成同一角色的表情差分（开心/难过/惊讶/生气/困）和 2~3 条循环动作视频，客户端按状态切换。有了 `sprites/闭眼_187.png` 眨眼会自动用真图
3. **真 Live2D（路线 C）**：需要 `.moc3 + motions + exp3` 模型（美术建模，必须外部完成）。环境已验证 QtWebEngine 可用，接入不是问题
4. 情绪 → 采样参数联动（把 SoVITS 那套 10 情绪表接到气泡/语音上）

**已知限制**：Docker 部署下"装插件即出现桌宠"物理上做不到（容器没有桌面）；只有 AstrBot 跑 Windows 原生时才能用 `auto_launch` 直接拉起。


## 组成

```
astrbot_plugin_tsukiyo_pet/
├─ main.py              AstrBot 插件（命令 / 回复同步 / LLM 工具 / 启 HTTP 桥）
├─ core/                与 AstrBot 无关的纯逻辑（可单测）
│   ├─ protocol.py      动作白名单、事件与指令的校验
│   ├─ hub.py           客户端注册表 + 指令队列 + 事件环形缓冲
│   ├─ owner.py         主人判定（平台无关）
│   ├─ httpapi.py       /pet/* HTTP 接口（0.0.0.0，容器可映射）
│   └─ tts.py           GPT-SoVITS 客户端（可选）
├─ client/              桌面端（PySide6）
│   ├─ pet.py           透明置顶窗口 / 拖拽 / 气泡 / 动作 / 轮询
│   ├─ config.json      指向插件桥的地址
│   └─ sprites/         三视图素材
└─ tests/               纯逻辑单测（不依赖 AstrBot / PySide6）
```

## 快速开始

### 1. 装插件

把本仓库放到 AstrBot 的 `data/plugins/astrbot_plugin_tsukiyo_pet/`，重载插件。
插件启动后会监听 `0.0.0.0:18790`（可在配置里改）。

**Docker 部署还要做两件事**：

```yaml
# compose.yaml 的 astrbot 服务加端口映射
ports:
  - "127.0.0.1:6185:6185"
  - "127.0.0.1:18790:18790"
```

```jsonc
// 插件配置里把 public_base 填成宿主能访问到的地址
"bridge": { "host": "0.0.0.0", "port": 18790, "public_base": "http://127.0.0.1:18790" }
```

### 2. 装客户端

```powershell
pip install PySide6 httpx
cd client
python pet.py
```

桌宠会出现在桌面右下角，并自动注册到插件。Windows 上若要免终端，用 `pythonw pet.py`。

### 3. 配置主人

插件配置 `owner_id` 填你的发送者 ID：

| 平台 | 填什么 | 从哪拿 |
|---|---|---|
| `aiocqhttp`（NapCat） | 数字 QQ 号 | 你的 QQ 号 |
| `qq_official`（官方机器人） | openid 那串 | AstrBot 日志里 `[default] [大肥鱼(qq_official)] 1C1F...:` 冒号前那串 |

留空时会绑定本次运行**第一个私聊的人**。

## 命令（主人私聊）

| 命令 | 作用 |
|---|---|
| `/pet status` | 桌宠是否在线、最近事件 |
| `/pet say <文本>` | 让桌宠冒一个气泡 |
| `/pet action <动作>` | 跳一下 / 摇一摇 / 伸懒腰 / 待着 / 散步 / 左右上下移动 |
| `/pet mode <模式>` | `idle`（待着）/ `wander`（散步）/ `follow`（跟鼠标） |

## LLM 工具

| 工具 | 说明 |
|---|---|
| `pet_action` | 让桌宠做动作（跳/摇/伸懒腰/八方向移动/切模式），模型自己决定什么时候用 |
| `pet_state` | 查桌宠在线状态、位置、模式、最近互动 |

需要在人格里放行这两个工具。

## 语音（可选）

插件配置：

```jsonc
"tts": {
  "enabled": true,
  "endpoint": "http://host.docker.internal:9880/tts",
  "ref_audio_path": "D:\\...\\ref_66_20_温柔.wav",
  "prompt_text": "你一直在为被悲鸣侵扰的索拉里斯奔波。这是你想做的事，我觉得很好。",
  "prompt_lang": "zh",
  "speed_factor": 0.9
}
```

开启后，`/pet say` 和 QQ 回复同步都会**先合成语音**，客户端收到音频 URL 后播放（气泡同时显示）。

> 容器里访问宿主机的 GPT-SoVITS 用 `host.docker.internal`；参考音频路径必须是**插件所在环境**能读到的路径，
> 所以 Docker 部署时建议把参考音频放进 `data/plugin_data/astrbot_plugin_tsukiyo_pet/` 再填容器内路径。

## 排错

| 现象 | 先看什么 |
|---|---|
| 桌宠不出现 | 客户端窗口起来没有：看 `client/client.log`；没日志说明 Python 崩在启动阶段，命令行手跑 `python client/pet.py` 看报错 |
| 桌宠在但没反应 | 浏览器/终端访问 `http://127.0.0.1:18790/pet/health`，`online` 应为 1；不是 1 说明客户端没连上（检查 `client/config.json` 的 `base_url`） |
| 有气泡没声音 | 看 `client/client.log` 里的 `speak:` 行；再确认 `tts.enabled=true`、SoVITS 在 9880 跑着、`ref_audio_path` 是**SoVITS 所在机器**能读到的路径 |
| 改了配置不生效 | 先 `/pet stop`（或关客户端），保存配置并重载插件，再 `/pet start` |
| 抖音式提示"仅限主人" | `owner_id` 没填或填错；官方机器人要填 openid 那串，不是 QQ 号 |

调试可以直接往桥推一条（不需要 QQ）：

```powershell
POST http://127.0.0.1:18790/pet/push
{"text": "测试气泡"}
{"action": "jump"}
{"audio": "hello.wav", "text": "带声音的气泡"}   # hello.wav 放在插件音频目录
```

`/pet/health` 里的 `audio_hits` 会随客户端取声音而增加，可用来确认声音链路通不通。

## 许可

MIT。三视图素材来自 [1190fasheqi/dafeiyu-pet](https://github.com/1190fasheqi/dafeiyu-pet)（MIT），署名见 `CREDITS.md`。
