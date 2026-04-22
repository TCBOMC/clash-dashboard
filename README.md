# Clash Dashboard

基于 Docker 的 Clash 代理管理面板，UI 参考 Clash Verge Windows 版，开箱即用。

## 功能特性

| 页面 | 功能 |
|------|------|
| 🏠 概览 | 实时流量图表、运行状态、代理模式切换 |
| 🌐 代理节点 | 按代理组浏览节点、一键切换、延迟测速 |
| 🔗 连接 | 实时刷新活跃连接，搜索过滤，断开单条/全部 |
| 📋 规则 | 查看当前生效规则（支持搜索/类型过滤/分页） |
| 📄 日志 | SSE 实时日志流，级别过滤，暂停/清空 |
| 📦 订阅管理 | 添加/更新/激活/删除订阅，自动下载节点 |
| ⚙️ 规则配置 | 可视化编辑规则 + 编辑原始 YAML 配置 |
| 🔧 设置 | 混合端口、LAN 访问、IPv6、日志级别、API 密钥 |

---

## 快速部署（开箱即用）

> 适用于已经有可用 Clash 配置文件的用户，无需 clone 代码。

### 第一步：创建目录

```bash
# 在任意目录创建 clash 配置文件夹
mkdir clash-dashboard && cd clash-dashboard
```

### 第二步：下载 docker-compose.yml 和 .env

```bash
# 下载 docker-compose.yml
curl -O https://raw.githubusercontent.com/<你的用户名>/clash-dashboard/main/docker-compose.yml

# 下载 .env 模板
curl -O https://raw.githubusercontent.com/<你的用户名>/clash-dashboard/main/.env.example
cp .env.example .env
```

### 第三步：放入 Clash 配置文件

```bash
# 在 clash-dashboard/ 目录下创建 clash-config 子目录，放入你的 config.yaml
mkdir -p clash-config
# 将你的 Clash 配置命名为 config.yaml 放入 clash-config/
```

### 第四步：编辑 docker-compose.yml 中的镜像地址

```yaml
# 找到这一行，改成你实际发布的镜像地址
image: ghcr.io/<你的用户名>/clash-dashboard:latest
```

### 第五步：启动

```bash
docker compose up -d
# 访问 http://localhost:8080
```

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
├── .github/workflows/
│   └── docker-publish.yml   # 自动构建 + 推送镜像
├── Dockerfile               # Dashboard 镜像构建文件
├── docker-compose.yml        # 发布版（引用已发布镜像）
├── docker-compose.local.yml  # 本地开发版（本地构建）
├── .dockerignore
├── .env.example
└── README.md
```

## 端口说明

| 端口 | 用途 |
|------|------|
| 8080 | WebUI 管理界面 |
| 9090 | Clash API（容器内部） |
| 7890 | HTTP/SOCKS5 混合代理 |
| 7891 | SOCKS5 独立端口 |
| 7892 | 透明代理（Linux） |

## 订阅管理使用流程

1. 进入"订阅管理" → 点击"添加订阅"
2. 填写名称和订阅 URL
3. 点击"更新"下载节点列表
4. 点击"激活"设为当前配置 → Clash 自动重载
