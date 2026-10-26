# Optional local diagnostics

The Hub reads per-device configuration from `AGENT_HUB_DATA_DIR` or `~/.agent-hub`; executors and proxies are configured by the user. Optional diagnostic scripts do not require a nested Muse checkout.

## Muse

Use a Python environment with `muse_mcp` installed. The caller of `verify_muse.py` also needs the `mcp` package. By default the verifier starts Muse with its own Python interpreter. `MUSE_PYTHON` or `--python` selects a different interpreter; the explicit argument takes precedence.

```sh
python verify_muse.py --python /path/to/muse-environment/bin/python
```

The verifier checks status by default. `--send` explicitly sends a connection-test message. Existing proxy environment variables are inherited; no proxy is required or supplied by the script.

The Windows authentication helper resolves Python and native `agent-browser.exe` from PATH, with optional overrides:

```powershell
.\Authenticate-Muse.ps1 -MusePython 'C:/tools/muse/python.exe' -BrowserDirectory 'C:/tools/browser' -CheckOnly
.\Authenticate-Muse.ps1 -MusePython 'C:/tools/muse/python.exe' -BrowserDirectory 'C:/tools/browser'
```

`BrowserDirectory` must contain native `agent-browser.exe`; an explicit directory is never replaced by a different PATH installation. A `.cmd` npm shim alone is not sufficient for Muse's subprocess launcher. If the installed package provides `agent-browser-win32-x64.exe`, copy that native binary to a tools directory as `agent-browser.exe` and select that directory. `MUSE_PYTHON` and `MUSE_BROWSER_BIN` supply defaults for these arguments. `-CheckOnly` resolves the local commands without starting them, connecting to a browser, or changing authentication or PATH. The second command exports authentication from an existing logged-in browser session and checks Muse status. `-ProxyUrl` is optional and applies to both authentication and verification; otherwise the existing environment is retained.

## Archived feasibility probes

`docs/probes/preflight.py` uses `codex` and `claude` on PATH, or `CODEX_EXECUTABLE` / `CLAUDE_EXECUTABLE`. `docs/probes/muse_probe.py` uses `MUSE_PYTHON` or the current interpreter. These are opt-in live probes: they can submit messages, create sessions, and overwrite historical result files. Do not run them as offline regression tests or publish their output without reviewing it for personal information.

中文说明：诊断工具使用当前环境或显式配置的程序路径，不依赖某个用户的目录。`-CheckOnly` 只检查命令位置，不进行认证；历史探针会访问真实服务，不属于离线测试。
