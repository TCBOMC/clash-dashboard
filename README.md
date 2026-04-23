# Clash Dashboard

基于 Docker 的 Clash 代理管理面板，UI 参考 Clash Verge Windows 版，开箱即用。

## 功能特性

| 页面 | 功能 |
:|------|------|
| 🏠 概览 | 实时流量图表、运行状态、代理模式切换（规则/全局/直连） |
| 🌐 代理节点 | 按代理组浏览节点、可折叠分组、一键切换、延迟测速 |
| 🔗 连接 | 实时刷新活跃连接，搜索过滤，断开单条/全部 |
| 📋 规则 | 查看当前生效规则（支持搜索/类型过滤/分页） |
| 📄 日志 | SSE 实时日志流，级别过滤，暂停/清空 |
| 📦 订阅管理 | 添加/更新/激活/删除订阅，自动下载节点；无 URL 订阅支持本地上传配置文件；当前激活订阅更新后自动重新应用 |
| ⚙️ 规则配置 | 可视化编辑规则 + 编辑原始 YAML 配置 |
| 🔧 设置 | 代理端口模式（混合/分离）、HTTP 端口、SOCKS5 端口、LAN 访问、IPv6、日志级别、API 密钥 |

**UI 交互增强：**
- 菜单导航状态持久化（刷新页面保持当前页）
- 代理组折叠状态持久化
- 订阅卡片区分激活状态（绿色边框）

---

## 快速部署

### 方式一：Docker Compose

在任意目录创建 `docker-compose.yml`，内容如下：

```yaml
services:
  clash-dashboard:
    image: trseimc/clash-dashboard:latest
    container_name: clash-dashboard
    restart: unless-stopped
    ports:
      - "8080:8080"
      - "7890:7890"
      - "7891:7891"
    volumes:
      - ./clash-config:/app/clash-config:rw
      - ./logs:/app/logs:rw
    cap_add:
      - NET_ADMIN
    network_mode: bridge
```

```bash
# 创建配置目录并放入 config.yaml
mkdir -p clash-config && vim clash-config/config.yaml

# 启动
docker compose up -d

# 访问 http://localhost:8080
```

### 方式二：本地免 Docker（Windows）

```bat
# 双击运行启动脚本（项目根目录）
start.bat
```

访问 http://localhost:8080 即可。

### 端口说明

| 端口 | 用途 |
:|------|------|
| 8080 | WebUI 管理界面 |
| 9090 | Clash API（容器内部） |
| 7890 | HTTP 代理 / 混合代理（取决于端口模式） |
| 7891 | SOCKS5 代理（只在分离模式下生效） |

---

## 本地开发 / 自定义构建

```bash
git clone https://github.com/<你的用户名>/clash-dashboard.git
cd clash-dashboard

# 编辑你的 config.yaml
# vim clash-config/config.yaml

# 方式A: 用已发布的镜像（不需要构建）
docker compose up -d

# 方式B: 本地构建（开发调试用）
docker compose -f docker-compose.local.yml up -d --build
```

---

## 镜像发布指南（维护者）

### 前置条件

- GitHub 账号（ghcr.io 免费，无需申请）
- （可选）Docker Hub 账号（用于发布到 Docker Hub）

### 步骤一：Fork 并配置仓库

1. Fork 本仓库到你的 GitHub 账号
2. 进入仓库 **Settings → Actions → General**，确认 "Workflow permissions" 选 **"Read and write permissions"**

### 步骤二：发布到 ghcr.io（自动）

每次 push 到 `main` 分支，GitHub Actions 会自动：
1. 构建 multi-platform 镜像（amd64 + arm64）
2. 推送到 `ghcr.io/<你的用户名>/clash-dashboard`

查看包：GitHub 仓库首页 → **Packages** → 找到 `clash-dashboard`

### 步骤三：发布到 Docker Hub（可选）

在仓库 **Settings → Variables** 中添加：
- `DOCKER_HUB_USER` = 你的 Docker Hub 用户名
- `DOCKER_HUB_TOKEN` = 你的 Docker Hub Access Token（在 Docker Hub → Account Settings → Security 创建）

添加后，每次 push 也会自动推送到 Docker Hub。

### 步骤四：更新 docker-compose.yml 中的镜像地址

构建成功后，在你的仓库中将：

```yaml
# 原来（示例地址）
image: ghcr.io/<你的用户名>/clash-dashboard:latest

# 改成你实际的镜像地址
image: ghcr.io/你的实际用户名/clash-dashboard:latest
```

然后用户就可以直接 `docker compose up -d` 部署了。

---

## 目录结构

```
clash-dashboard/
├── backend/
│   ├── main.py              # FastAPI 后端服务
│   └── requirements.txt     # Python 依赖
├── frontend/
│   └── index.html            # 单页前端应用
├── clash-config/             # Clash 配置文件（用户自行准备）
│   └── config.yaml
├── bin/                      # Bundled mihomo 二进制（本地模式）
├── .github/workflows/
│   └── docker-publish.yml   # 自动构建 + 推送镜像
├── Dockerfile               # Dashboard 镜像构建文件
├── docker-compose.yml        # 发布版（引用已发布镜像）
├── docker-compose.local.yml  # 本地开发版（本地构建 + bundled mihomo）
├── docker-compose.prebuilt.yml  # Docker 内嵌 mihomo 版
├── docker-compose.deploy.yml    # 部署版（仅 Dashboard，无 mihomo）
├── start.bat                 # Windows 一键启动脚本
├── .dockerignore
├── .env.example
└── README.md
```

## 订阅管理使用流程

1. 进入"订阅管理" → 点击"添加订阅"
2. 填写名称和订阅 URL
3. 点击"更新"下载节点列表
4. 点击"激活"设为当前配置 → Clash 自动重载
