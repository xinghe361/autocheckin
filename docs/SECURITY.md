# 安全设计说明

本文档说明本项目在安全方面的**设计与使用方法**。

---

## 一、凭据是怎么存的

| 内容 | 存放位置 | 保护方式 |
|---|---|---|
| 站点密码、DeepSeek Key、WebDAV 密码、访问口令 | `config.json` | Fernet 加密（AES-128-CBC + HMAC），文件权限 600 |
| 加密密钥 | `AUTOCHECKIN_KEY` 环境变量，或 `DATA_DIR/secret.key` | 权限 600；**单独存放** |
| 会话令牌 | 只存 SHA-256 哈希 | 配置里没有令牌明文 |
| 运行状态 | `state.json` | 不含凭据，可安全备份 |

**密钥为什么单独一个文件**：备份里存的是**密文**。若把密钥与密文放进同一个
备份对象，等于把锁和钥匙捆在一起，加密就失去了意义。所以：

- `config.json` / 备份文件里只有密文
- `encryption-key.txt` 是独立文件，**是否上传由你显式勾选**（默认关闭）
- 界面显示密钥指纹（如 `ue6A62…1Jw=`），便于核对两台机器是否同一密钥，
  又不泄露密钥本身

**密钥丢了会怎样**：已保存的站点密码与 API Key 解不开。此时会得到明确报错
（提示"无法解密"并让你重新填写），而不是笼统的"登录失败"。

## 二、访问控制

网页界面默认需要登录。首次访问会引导你设置一个访问口令（至少 6 位）。

| 措施 | 说明 |
|---|---|
| 口令存储 | Fernet 加密后写入配置，不存明文 |
| 会话令牌 | 只存 SHA-256 哈希，不存令牌本身 |
| 令牌传递 | HttpOnly + SameSite=Lax Cookie；**不放在 URL 里** |
| 口令比较 | 使用常数时间比较函数 |
| 登录限速 | 5 分钟内失败 10 次即拒绝 |
| 改口令 | 所有旧会话立即失效 |
| 匿名可访问 | 仅 `/healthz`（容器健康检查）与 `/api/auth/state`（登录页） |

口令只用于进入本界面，**与你的各站点账号无关**。

### 首次启动请立刻完成初始化

容器第一次启动时还没有口令。请**立即打开网页完成初始化设置**，
不要让它长时间停留在这个状态。

### 忘记口令怎么办

在 NAS 上编辑数据目录下的 `config.json`：

```bash
# 方式一：关闭访问控制（改完重启容器生效）
"auth_required": false

# 方式二：清空已设口令，重启后会重新引导设置
"auth_password_enc": ""
```

## 三、网络安全

- **全站统一走代理**（你的代理已做分流，填代理地址即可）
- **每个通知渠道可单独选择走不走代理**
- 通知渠道的凭据同样加密存储
- **不要把本服务的端口暴露到公网**。它设计为在内网使用；
  公网暴露会显著放大风险，且与"关闭沙箱"叠加后后果更重

## 四、容器加固

`docker-compose.yml` 默认已带：

```yaml
security_opt:
  - no-new-privileges:true   # 容器内进程无法通过 setuid 等方式提权
shm_size: "256m"             # Chromium 需要足够的 /dev/shm，否则会崩溃
```

**为什么容器内关闭了 Chromium 自带沙箱**：Playwright 的 Chromium 在容器里
开启沙箱时常因缺少内核特性（`CAP_SYS_ADMIN`、seccomp 限制、或宿主关闭了
`unprivileged_userns_clone`）而无法启动，这是容器环境下运行 Playwright 的
常见做法。本镜像在此基础上用 `no-new-privileges` 做了补偿。

**如果想进一步收紧**，可以改为非 root 运行：

```yaml
user: "1000:1000"
security_opt:
  - no-new-privileges:true
```

配套需要把宿主机数据目录属主改成 1000（否则启动即权限拒绝）：

```bash
sudo chown -R 1000:1000 /你的数据目录
```

> 三者互斥，最多同时满足两个：**非 root**、**开启 Chromium 沙箱**、
> **免额外配置**。因为 Chromium 的沙箱辅助程序需要 setuid（即需要 root）。

## 五、依赖与供应链

| 项 | 状态 |
|---|---|
| `playwright` | 固定 `==1.48.0` |
| `cryptography` | 固定 `==43.0.3` |
| 基础镜像 | 固定 tag `mcr.microsoft.com/playwright/python:v1.48.0-noble`（非 latest） |
| 其余依赖 | **全部为 Python 标准库**，无额外第三方包 |

纯标准库是刻意选择：HTTP 服务、JSON 解析、WebDAV 客户端等都不引入依赖，
攻击面因此小很多。

## 六、提交前的自查

```bash
python selfcheck.py            # 检查敏感文件、编码、关键文件完整性
python docs/check_compose.py   # 校验 compose 结构（缩进、关键字段）
python -m unittest discover -s tests
```

`selfcheck.py` 会扫描仓库里是否误留了密钥、密码、token 等敏感内容。
