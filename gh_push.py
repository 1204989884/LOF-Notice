"""
GitHub Actions 定时推送脚本
在每个交易日的 11:31 和 14:40 触发，抓取 LOF 套利数据推送到微信
"""
import asyncio
import json
import os
import sys
import urllib.request
import urllib.parse

# ---------- 配置 ----------
SENDKEY = os.environ.get("SENDKEY", "")
if not SENDKEY:
    print("[错误] 未设置 SENDKEY 环境变量，请在 GitHub Secrets 中添加")
    sys.exit(1)

# ---------- 数据获取 ----------
async def fetch_data():
    from fetcher import get_all_lof_arbitrage_opportunities
    return await get_all_lof_arbitrage_opportunities(use_mock=False)

# ---------- 格式化 ----------
def format_message(data):
    opportunities = data.get("data", [])
    updated_at = data.get("updated_at", "未知")

    if not opportunities:
        return f"## LOF套利监测\n\n更新时间：{updated_at}\n\n今日暂无符合条件的LOF套利机会。\n\n> 数据来源：{data.get('source', 'unknown')}"

    premium_list = [o for o in opportunities if o.get("is_premium")]
    discount_list = [o for o in opportunities if not o.get("is_premium")]

    lines = [
        "## LOF套利监测日报",
        "",
        f"- 更新时间：{updated_at}",
        f"- 总机会数：{len(opportunities)}",
        f"- 溢价品种：{len(premium_list)} 只",
        f"- 折价品种：{len(discount_list)} 只",
        "",
    ]

    if premium_list:
        lines.append("### 溢价套利机会")
        lines.append("")
        lines.append("| 代码 | 名称 | 场内价 | 净值 | 溢价率 | 成交额 | 申购状态 |")
        lines.append("|------|------|--------|------|--------|--------|----------|")
        for o in premium_list[:15]:
            code = o.get("code", "")
            name = o.get("name", "")
            price = o.get("price", 0)
            nav = o.get("nav_verified", o.get("nav", 0))
            premium = o.get("premium_rt", 0)
            amount = o.get("amount", 0)
            status = o.get("purchase_status", "未知")
            amount_str = f"{amount/10000:.0f}万" if amount >= 10000 else f"{amount:.0f}"
            lines.append(f"| {code} | {name} | {price:.3f} | {nav:.4f} | {premium:+.1f}% | {amount_str} | {status} |")
        lines.append("")

    if discount_list:
        lines.append("### 折价套利机会")
        lines.append("")
        lines.append("| 代码 | 名称 | 场内价 | 净值 | 折价率 | 成交额 | 申购状态 |")
        lines.append("|------|------|--------|------|--------|--------|----------|")
        for o in discount_list[:10]:
            code = o.get("code", "")
            name = o.get("name", "")
            price = o.get("price", 0)
            nav = o.get("nav_verified", o.get("nav", 0))
            premium = o.get("premium_rt", 0)
            amount = o.get("amount", 0)
            status = o.get("purchase_status", "未知")
            amount_str = f"{amount/10000:.0f}万" if amount >= 10000 else f"{amount:.0f}"
            lines.append(f"| {code} | {name} | {price:.3f} | {nav:.4f} | {premium:+.1f}% | {amount_str} | {status} |")
        lines.append("")

    lines.append(f"> 数据来源：{data.get('source', 'unknown')}")
    lines.append("> 仅供参考，不构成投资建议。")

    return "\n".join(lines)

# ---------- 推送 ----------
def push_wechat(title, content):
    url = f"https://sctapi.ftqq.com/{SENDKEY}.send"
    data = urllib.parse.urlencode({"title": title, "desp": content}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            if result.get("code") == 0:
                print("[推送] 微信推送成功")
                return True
            else:
                print(f"[推送] 失败: {result}")
                return False
    except Exception as e:
        print(f"[推送] 异常: {e}")
        return False

# ---------- 主流程 ----------
async def main():
    print("[开始] LOF数据抓取...")
    try:
        result = await fetch_data()
    except Exception as e:
        print(f"[错误] 数据抓取失败: {e}")
        push_wechat("LOF监测推送失败", f"数据抓取异常：{e}")
        sys.exit(1)

    print(f"[数据] 获取到 {result.get('count', 0)} 条机会")
    content = format_message(result)
    push_wechat("LOF套利监测日报", content)
    print("[完成]")

if __name__ == "__main__":
    asyncio.run(main())
