# 部署与发布指南

本文档记录"把项目传到 GitHub、构建镜像、发布到 Docker Hub"的完整步骤。
本地自检随时可跑：

```bash
python selfcheck.py     # 确认没有敏感文件、关键文件齐全
```

---

## 一、先配好 git 身份（只需一次）

```bash
git config --global user.name "你的名字"
git config --global user.email "你的邮箱"
```

## 二、在 GitHub 建一个空仓库

1. 打开 https://github.com/new
2. **Repository name** 填 `autocheckin`
3. **不要**勾选 "Add a README file"（否则首次推送会冲突）
4. 点 **Create repository**

## 三、推送代码

```bash
cd autocheckin
python push_to_github.py --repo https://github.com/你的用户名/autocheckin.git
```

脚本会自动：检查敏感文件没被提交 → 初始化仓库 → 提交 → 推送。
**推送过程中还会二次确认 `secret.key` / `config.json` 之类没被暂存，有就直接中止。**

> 首次推送若要求登录：用户名填 GitHub 用户名，**密码位置填 Personal Access Token**
> （GitHub 已不支持账号密码推送）。
> 生成：头像 → Settings → Developer settings → Personal access tokens →
> Tokens (classic) → Generate new token (classic) → 勾 `repo`。

---

## 四、生成 Docker Hub 访问令牌

**不能用登录密码**，必须用 token：

1. 打开 https://hub.docker.com/ ，登录
2. 右上角**头像** → **Account Settings**
3. 左侧 **Personal access tokens** → **Generate new token**
4. Description 随便填（如 `github-actions`）
5. **Access permissions** 选 **Read & Write**（必须，否则推不上去）
6. **Generate** → **立刻复制**（只显示一次）

同时记下你的 **Docker Hub 用户名**（不是邮箱）。

## 五、配置 GitHub 仓库的 Secrets 与 Variables

打开（替换用户名）：

```
https://github.com/你的用户名/autocheckin/settings/secrets/actions
```

或点着走：**仓库页 → Settings → 左侧 Secrets and variables → Actions**

### 5.1 Secrets 标签 → New repository secret

| Name（区分大小写，必须一致） | Secret |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub 用户名 |
| `DOCKERHUB_TOKEN` | 上一步复制的 token |

### 5.2 Variables 标签 → New repository variable

| Name | Value |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub 用户名（明文） |

> **为什么还要配 Variables**：工作流里 `docker/metadata-action` 的 `images`
> 字段取不到 secrets（GitHub 的限制），所以镜像名从变量取，secret 只用于登录。
> 工作流已做回退（变量缺失时用同名 secret），但配上更稳。

## 六、触发构建

**方式一：推 tag（推荐）**

```bash
git tag v1.0.0
git push origin v1.0.0
```

**方式二：手动触发**
GitHub → **Actions** 标签 → 左侧选 `build-and-publish` → 右侧 **Run workflow**

构建成功后镜像地址：

```
docker.io/你的用户名/autocheckin:latest
```

## 七、在极空间上用这个镜像

把 [docker-compose.yml](../docker-compose.yml) 里的 `image` 换成上面的地址即可。
该 NAS **没有** `docker compose` 子命令，需用它自带的 Docker 界面里的「编排/Compose」。

---

## 附：工作流做了什么

`.github/workflows/build.yml` 分两个 job：

1. **test** — 装依赖、跑 **410 项单测**、跑构建前自检。测试不过就不会构建镜像。
2. **build** — 检查凭据是否配置 → 登录 Docker Hub → 生成标签（tag / latest / sha）
   → 构建 `linux/amd64` 并推送。

标签策略：

| 触发 | 产出标签 |
|---|---|
| 推 `v1.0.0` | `v1.0.0`、`latest`（默认分支）、`sha-xxxxxxx` |
| 手动运行 | `latest`、`sha-xxxxxxx` |
