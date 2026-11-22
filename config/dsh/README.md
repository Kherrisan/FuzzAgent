# `tui` dsh profile

The dsh profile used by FuzzAgent's `dsh-tui` driver. The profile itself
(`package.json` + `node_modules`) is created **in the image** — the repository
only carries YAML.

```
config/dsh/profiles/tui/cordis.patch.yml   # the `ark` LLM API (this repo)
/opt/dsh-profile/profiles/tui/             # image: dsh plugin add -> package.json + node_modules
```

## Why the image, not the repo

A dsh profile only exists if it has a `package.json` (dsh answers
`profile "x" does not exist` otherwise), and dsh resolves bundle *imports* from
the profile directory, so the profile needs the installed `node_modules` tree
(89 MB). Neither belongs in git, so `docker/Dockerfile` runs

```dockerfile
DSH_HOME=/opt/dsh-home dsh plugin --profile tui add -w \
    "@deepseek-harness-tui/dsh-tui@${DSH_TUI_VERSION}"
```

once and keeps the result at `/opt/dsh-profile/profiles/tui`.

At campaign start `DshTuiDriver._seed_profile()` copies that tree into each
role's `$DSH_HOME` and overlays the YAML patch below on top of it.

## Provider: `ark`

| 字段 | 值 | 来源 |
|---|---|---|
| `displayName` | `Ark` | 本机 `~/.dsh/profiles/dsh-tui/cordis.patch.yml` |
| `baseURL` | `https://ark.cn-beijing.volces.com/api/v3` | 同上 |
| `api` | `openai-completions` | 同上 |
| `model` | `deepseek-flash` | 同上 |
| `apiKeyEnv` | `ARK_FLASH_API_KEY` | 同上 |

密钥**不写进任何仓库文件**。dsh 把它存在凭据库 `~/.dsh/.credentials.yaml`
的 `refs: ARK_FLASH_API_KEY` 下，插件通过 `apiKeyEnv` 指定的环境变量取值。

## 使用方式

```bash
dsh --profile tui                              # 带 ark 的 TUI
dsh --profile tui --dump-config | grep -A6 ark: # 只检查配置
```

在 campaign 里：

- `config/agents-dsh-tui/*.yaml` — 8 个 role，`driver: dsh-tui`、`launch: [dsh]`；
  driver 自动补 `--profile tui --patch <mcp overlay>`
- `config/campaign-dsh-tui.yaml` — `agents_dir` + `auth_config`
- `config/agent_auth-dsh.toml` — 把 `HOST_AUTH_DIR/dsh/.credentials.yaml` 复制到
  每个 role 的 `$HOME/.dsh/.credentials.yaml`（`bin/collect-auth` 负责收集）

## 校验过的内容

```
dsh --profile tui --dump-config
  → providers.ark: {baseURL: https://ark.cn-beijing.volces.com/api/v3,
                    api: openai-completions, models: [{id: deepseek-flash}]}
启动时 "failed to import" 计数 = 0
```
