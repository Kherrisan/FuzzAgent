# `ark-deepseek-flash` dsh profile

A minimal [dsh](https://github.com/deepseek-ai/deepseek-harness) profile that
runs the dsh-TUI over the DeepSeek **ark** API.

```
config/dsh/profiles/ark-deepseek-flash/
├── package.json        # bundles: @deepseek-ai/dsh-base + @deepseek-harness-tui/dsh-tui
└── cordis.patch.yml    # LLM provider "ark" (baseURL / model / api key source)
```

## Provider: `ark`

| 字段 | 值 | 来源 |
|---|---|---|
| `displayName` | `Ark` | 本机 `~/.dsh/profiles/dsh-tui/cordis.patch.yml` |
| `baseURL` | `https://ark.cn-beijing.volces.com/api/v3` | 同上 |
| `api` | `openai-completions` | 同上 |
| `model` | `deepseek-flash`（DeepSeek Flash） | 同上 |
| `apiKeyEnv` | `ARK_FLASH_API_KEY` | 同上 |

密钥**不写进任何仓库文件**。dsh 把它存在凭据库 `~/.dsh/.credentials.yaml` 的
`refs: ARK_FLASH_API_KEY` 下，插件通过 `apiKeyEnv` 指定的环境变量取值。

## 使用方式

### 1. 放进某个 DSH_HOME

```bash
mkdir -p "$DSH_HOME/profiles"
cp -a config/dsh/profiles/ark-deepseek-flash "$DSH_HOME/profiles/ark-deepseek-flash"
```

profile 的 bundles（`dsh-base`、`dsh-tui`）从 **dsh 安装目录**解析，所以只要
`@deepseek-harness-tui/dsh-tui` 是全局安装的，这个 profile 就**不需要**
`dsh plugin add` / pnpm / 额外网络。

### 2. 提供密钥

两种方式任选：

```bash
# A. 复用本机 dsh 的凭据库
cp ~/.dsh/.credentials.yaml "$DSH_HOME/.credentials.yaml"
chmod 600 "$DSH_HOME/.credentials.yaml"

# B. 直接给环境变量
export ARK_FLASH_API_KEY=...
```

### 3. 启动

```bash
dsh --profile ark-deepseek-flash             # 等价于 dsh-tui，但带 ark 提供方
dsh --profile ark-deepseek-flash --dump-config | grep -A8 "ark:"   # 只检查配置
```

## 在 campaign 里使用

- `config/agent_auth-dsh.toml` 会把 `HOST_AUTH_DIR/dsh/.credentials.yaml` 复制到
  每个 role 的 `$HOME/.dsh/.credentials.yaml`（`bin/collect-auth` 负责把宿主机的
  那份收集到 auth bundle 里）。
- driver 侧：`DshTuiDriver._seed_profile()` 会把镜像里预烤的 profile 复制进 role 的
  `DSH_HOME`；本目录的 profile 可作为「带上 ark 提供方」的那一份模板。

## 校验过的内容

`dsh --profile ark-deepseek-flash --dump-config`（临时 `DSH_HOME`）输出中能看到：

```yaml
- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers:
      ark:
        displayName: Ark
        apiKeyEnv: ARK_FLASH_API_KEY
        api: openai-completions
        baseURL: https://ark.cn-beijing.volces.com/api/v3
        models:
          - id: deepseek-flash
            name: DeepSeek V4.1 Flash
            contextWindow: 1048576
```
