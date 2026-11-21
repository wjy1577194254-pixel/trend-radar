"""极简 .env 加载器（不引入第三方依赖）。

用法：在程序入口最前面 `import envload`，它会把项目根目录的 .env
读进 os.environ（已存在的环境变量优先，不覆盖）。

顺带把 RADAR_HTTP_PROXY 同步成 HTTP(S)_PROXY —— urllib 会自动读后者，
这样抓国外源不用改代码就能走代理。
"""
import os
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent


def load(path: pathlib.Path | None = None, override: bool = False) -> int:
    """读 .env，返回载入的条目数。文件不存在就安静返回 0。"""
    p = path if path else _ROOT / ".env"
    if not p.exists():
        return 0

    n = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not k:
            continue
        if override or k not in os.environ:
            os.environ[k] = v
            n += 1
    return n


def _sync_proxy() -> None:
    """RADAR_HTTP_PROXY → HTTPS_PROXY/HTTP_PROXY，供 urllib 自动使用。"""
    px = (os.environ.get("RADAR_HTTP_PROXY") or "").strip()
    if not px:
        return
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        os.environ.setdefault(k, px)


load()
_sync_proxy()
