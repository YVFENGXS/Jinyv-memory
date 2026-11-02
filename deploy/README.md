# 部署脚本

| 脚本 | 用途 |
|---|---|
| `install.sh` | 主仓库安装到 `/opt/jinyv-memory`，创建 venv、安装 extras、注册 systemd |
| `install_plugins.sh` | 克隆/更新单一插件仓库到 `/opt/jinyv-plugins`，写入 `JINYV_PLUGIN_PATHS` |
| `install_desktop_adapter.sh` | 安装桌宠服务端依赖并生成 `JINYV_DESKTOP_TOKEN`；参数 `embedded / local / server` |
| `install_call_adapter.sh` | 克隆上游通话服务、安装依赖并生成 `JINYV_VIDEO_CALL_TOKEN`；参数 `embedded / local / server` |
| `jinyv-memory.service` | systemd 单元 |
| `jinyv-memory.env.example` | 生产环境变量模板（不含真实密钥） |

> 第三方分镜 CLI 仍使用 `scripts/install_storyboard.sh`，不随主仓库分发源码。
> 桌宠客户端 `YVFENGXS/Jinyu-pet` 与通话服务 `YVFENGXS/jinyu-video-call` 保持独立仓库；
> 本仓库只部署适配层，不复制其源码。

## 桌宠 / 通话双模

```bash
# 与核心同进程（默认）
sudo bash deploy/install_desktop_adapter.sh embedded
# 连本机核心 host-api
sudo bash deploy/install_desktop_adapter.sh local
# 连远程核心
sudo bash deploy/install_desktop_adapter.sh server https://core.example

sudo bash deploy/install_call_adapter.sh local
sudo bash deploy/install_call_adapter.sh server https://core.example
```

`local`/`server` 模式会确保 `JINYV_HOST_API_TOKEN` 存在，并写入
`JINYV_PLUGIN_DESKTOP_PET_CORE_MODE` / `_CORE_URL`、`JINYV_PLUGIN_VIDEO_CALL_CORE_MODE`
/ `_CORE_URL`。桌宠独立进程可用 `desktop-pet/standalone.py` 直接连核心。
