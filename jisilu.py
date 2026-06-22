"""
集思录数据获取脚本（通过 agent-browser CLI）
独立模块，与 fetcher.py 解耦，通过 JSON 文件通信。
"""
import subprocess
import json
import time
import re
import os

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".jisilu_data.json")
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".jisilu_cookies.json")


def _run_agent(cmd: str, **kwargs) -> str:
    """运行 agent-browser 命令并返回 stdout"""
    full_cmd = f"agent-browser {cmd}"
    result = subprocess.run(
        full_cmd,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=kwargs.get("timeout", 30),
    )
    return result.stdout


def login_and_fetch() -> dict:
    """
    使用 agent-browser 登录集思录并抓取全量 LOF 数据。
    返回: {"success": bool, "data": dict, "error": str}
    """
    print("[JSL agent] 启动浏览器登录...")

    # 1. 打开登录页
    out = _run_agent("open https://www.jisilu.cn/login/ --no-sandbox")
    if "集思录" not in out:
        return {"success": False, "error": "无法打开登录页"}

    # 2. 检查是否已登录（如果跳转到首页说明已登录）
    time.sleep(2)
    out = _run_agent("snapshot -i", timeout=10)
    if "手机号" not in out and "xiaoyucun9999" in out:
        print("[JSL agent] 已登录状态，跳过")
    else:
        # 需要登录
        print("[JSL agent] 填写登录表单...")
        _run_agent('type e15 "13428999519"')
        _run_agent('type e16 "Fuwenjun4227"')
        _run_agent("click e21")  # 同意协议
        time.sleep(0.5)
        _run_agent("click e25")  # 点击登录
        time.sleep(5)

    # 3. 抓取 LOF 数据 - 多页获取
    all_rows = []
    for page in range(1, 20):
        url = f"https://www.jisilu.cn/data/lof/index_lof_list/?rp=200&page={page}"
        out = _run_agent(f"open {url}")
        time.sleep(1)
        
        # 获取页面内容
        raw = _run_agent("snapshot", timeout=15)
        
        # 解析 JSON
        json_match = re.search(r'StaticText "(.*)"', raw, re.DOTALL)
        if not json_match:
            break
        
        raw_json = json_match.group(1)
        # 找到 JSON 对象边界
        depth = 0
        start = end = 0
        for i, ch in enumerate(raw_json):
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        
        if end == 0:
            break
        
        json_str = raw_json[start:end]
        json_str = json_str.replace('\\"', '"').replace('\\/', '/')
        
        try:
            data = json.loads(json_str)
            rows = data.get("rows", [])
            all_rows.extend(rows)
            print(f"[JSL agent] 第{page}页: {len(rows)}行 (累计{len(all_rows)}行, total={data.get('total',0)})")
            if len(rows) < 200:
                break
        except json.JSONDecodeError as e:
            print(f"[JSL agent] 第{page}页解析失败: {e}")
            break

    _run_agent("close", timeout=5)

    if not all_rows:
        return {"success": False, "error": "未获取到数据"}

    # 构建结果映射
    result = {}
    for row in all_rows:
        cell = row.get("cell", {})
        code = cell.get("fund_id", "")
        if not code:
            continue

        def _sf(v):
            try:
                return float(v)
            except Exception:
                return 0.0

        result[code] = {
            "fund_id": code,
            "fund_nm": cell.get("fund_nm", ""),
            "apply_status": cell.get("apply_status", "未知"),
            "nav_discount_rt": _sf(cell.get("nav_discount_rt", 0)),
            "discount_rt": _sf(cell.get("discount_rt", 0)),
            "fund_nav": _sf(cell.get("fund_nav", 0)),
            "price": _sf(cell.get("price", 0)),
            "amount": _sf(cell.get("amount", 0)),
            "volume": _sf(cell.get("volume", 0)),
            "nav_dt": cell.get("nav_dt", ""),
            "issuer_nm": cell.get("issuer_nm", ""),
        }

    # 保存到文件
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)

    print(f"[JSL agent] 完成: {len(all_rows)} 行 → {len(result)} 只 LOF")
    return {"success": True, "data": result}


def load_cached_data() -> dict | None:
    """加载缓存的集思录数据"""
    if not os.path.exists(OUTPUT_FILE):
        return None
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


if __name__ == "__main__":
    result = login_and_fetch()
    if result["success"]:
        print(f"OK: {len(result['data'])} LOFs")
    else:
        print(f"FAIL: {result.get('error')}")
