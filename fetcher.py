"""
LOF套利机会数据获取模块
数据源优先级: 集思录API > AKShare > 降级模拟数据

三步核实法:
  Step 1: 集思录初筛 → 得到"显示溢价率"（可能基于T-2净值）
  Step 2: 天天基金核实 → 获取最新净值 → 重新计算"核实溢价率"
  Step 3: 公告核实 → 检查最近公告是否有暂停/限购变动
"""
import asyncio
import time
import json
import re
import os
from datetime import datetime, timedelta
from typing import Optional

import httpx

# ---------- 配置 ----------
PREMIUM_THRESHOLD = 3.0          # 最终筛选溢价率阈值（%）
PREMIUM_PRESCREEN = 1.5         # 初筛阈值（%），设低一点避免漏掉
MIN_VOLUME = 100000            # 最低成交额（元），低于10万标记为流动性差
MIN_VOLUME_WARN = 500000       # 低于50万标黄色警告
MIN_VOLUME_OK = 5000000        # 高于500万标绿色（可放心交易）
MIN_PURCHASE_LIMIT = 100        # 最低申购限额（元），低于此值视为不可申购
REQUEST_TIMEOUT = 15            # API请求超时（秒）
NAV_VERIFY_CONCURRENCY = 8      # 净值核实时并发数
NAV_VERIFY_MAX = 60             # 最多核实多少只基金的净值（按成交额排序取前N）
JISILU_URL = "https://www.jisilu.cn/data/lof/index_lof_list/"
TENCENT_URL = "https://qt.gtimg.cn/q="
TTJJ_URL = "https://fundgz.1234567.com.cn/js/{code}.js"
EM_LOF_URL = "https://push2.eastmoney.com/api/qt/clist/get"


# ============================================================
#  工具函数
# ============================================================

def _safe_float(val) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _parse_date(date_str: str) -> Optional[datetime]:
    """解析日期字符串（支持 2026-06-01 / 20260601 等格式）"""
    if not date_str:
        return None
    date_str = str(date_str).strip()
    for fmt in ["%Y-%m-%d", "%Y%m%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"]:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    # 试试只有日期部分
    match = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", date_str)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    return None


# ============================================================
#  Step 1: 集思录初筛
# ============================================================

async def fetch_jisilu_lof() -> Optional[list[dict]]:
    """从集思录获取全量LOF数据（初筛阶段，降低阈值）"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.jisilu.cn/data/lof/",
        "Accept": "application/json",
    }
    params = {
        "___jsl": f"LST___t={int(time.time() * 1000)}",
        "rp": "50",
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True, trust_env=False) as client:
            resp = await client.get(JISILU_URL, headers=headers, params=params)
            if resp.status_code != 200:
                print(f"[集思录] 请求失败: HTTP {resp.status_code}")
                return None
            data = resp.json()
            rows = data.get("rows", [])
            if not rows:
                print("[集思录] 返回空数据（可能需登录）")
                return None

            results = []
            for row in rows:
                cell = row.get("cell", {})
                fund_id = cell.get("fund_id", "")
                fund_nm = cell.get("fund_nm", "")
                price = _safe_float(cell.get("price", 0))
                fund_nav = _safe_float(cell.get("fund_nav", 0))
                discount_rt = _safe_float(cell.get("discount_rt", 0))
                volume = _safe_float(cell.get("volume", 0))
                amount = _safe_float(cell.get("amount", 0))
                apply_status = cell.get("apply_status", "未知")
                nav_dt = cell.get("nav_dt", "")
                issuer_nm = cell.get("issuer_nm", "")
                turnover_rt = _safe_float(cell.get("turnover_rt", 0))

                if fund_nav <= 0 or price <= 0:
                    continue

                premium_rt = -discount_rt

                # 初筛: 降低阈值，避免漏掉（后面会核实）
                if abs(premium_rt) < PREMIUM_PRESCREEN:
                    continue

                results.append({
                    "code": fund_id,
                    "name": fund_nm,
                    "price": price,
                    "nav": fund_nav,
                    "nav_date": nav_dt,
                    "premium_rt": round(premium_rt, 2),
                    "volume": volume,
                    "amount": amount,
                    "apply_status": apply_status,
                    "issuer": issuer_nm,
                    "turnover_rt": round(turnover_rt, 2),
                    "source": "jisilu",
                })

            print(f"[集思录] 初筛（阈值>{PREMIUM_PRESCREEN}%）: {len(results)} 只候选")
            return results

    except httpx.TimeoutException:
        print("[集思录] 请求超时")
        return None
    except Exception as e:
        print(f"[集思录] 异常: {e}")
        return None


# ============================================================
#  集思录 Playwright 登录 + Cookie 管理 + 全量数据获取
#  解决直接POST登录被反爬的问题
# ============================================================

async def fetch_jisilu_data_full() -> Optional[dict[str, dict]]:
    """
    获取集思录全量 LOF 数据（含申购状态）。

    数据来源：.jisilu_data.json（由外部 agent-browser 脚本生成）
    自动化执行前需先运行 agent-browser 登录并抓取数据保存到此文件。

    Returns:
        {code: {"apply_status": "开放申购", "nav_discount_rt": 5.2, ...}}
    """
    import os as _os
    # 项目根目录下的缓存文件
    _jisilu_file = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".jisilu_data.json")

    if not _os.path.exists(_jisilu_file):
        print("[集思录全量] 缓存文件不存在，请先运行 agent-browser 抓取")
        return None

    mtime = _os.path.getmtime(_jisilu_file)
    from datetime import datetime as _dt
    age_hours = (_dt.now().timestamp() - mtime) / 3600

    try:
        with open(_jisilu_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"[集思录全量] 加载缓存（{age_hours:.1f}h前）: {len(data)} 只LOF")
        return data
    except Exception as e:
        print(f"[集思录全量] 加载失败: {e}")
        return None


def enrich_with_jisilu_status(lof_data: list[dict], jisilu_info: dict[str, dict]) -> list[dict]:
    """
    用集思录的申购状态覆盖东财的"未知"申购状态。
    集思录的 apply_status 更可靠（开放申购/暂停申购/限大额 等具体值）。

    jisilu_info: fetch_jisilu_data_full() 的返回值
    """
    updated_count = 0
    for item in lof_data:
        code = item.get("code", "")
        if code not in jisilu_info:
            continue

        jsl = jisilu_info[code]
        jsl_status = jsl.get("apply_status", "")

        # 只覆盖"未知"或空状态，保留已知的状态
        current_status = item.get("purchase_status", "未知")
        if current_status == "未知" or not current_status:
            if jsl_status and jsl_status != "未知":
                item["purchase_status"] = jsl_status
                item["purchase_status_source"] = "jisilu"
                updated_count += 1

        # 同时补充 jisilu 的溢价率和净值（用于交叉验证参考）
        item["jsl_nav_discount_rt"] = jsl.get("nav_discount_rt", 0)
        item["jsl_fund_nav"] = jsl.get("fund_nav", 0)
        item["jsl_apply_status"] = jsl_status

    if updated_count > 0:
        print(f"[集思录状态] 覆盖了 {updated_count} 只LOF的申购状态（未知→具体值）")
    return lof_data


def _jisilu_dict_to_lof_list(jisilu_data: dict[str, dict]) -> list[dict]:
    """
    将 .jisilu_data.json 的扁平字典格式转换为流水线标准格式。
    当东财/集思录API/AKShare全部不可用时，作为主数据源兜底。
    
    输入: {"160119": {"fund_id":..., "fund_nm":..., ...}, ...}
    输出: [{"code":..., "name":..., "source":"jisilu_cache", ...}, ...]
    """
    results = []
    for code, info in jisilu_data.items():
        if not code:
            continue
        price = info.get("price", 0) or 0
        volume = info.get("volume", 0) or 0  # 成交量(万份)
        amount = info.get("amount", 0) or 0  # 份额(万份)

        # 计算成交额: volume万份 × price元 ≈ 万元
        turnover = float(volume) * float(price)

        results.append({
            "code": str(code),
            "name": info.get("fund_nm", ""),
            "price": float(price),
            "nav": float(info.get("fund_nav", 0) or 0),
            "nav_discount_rt": float(info.get("nav_discount_rt", 0) or 0),  # 集思录溢价率
            "nav_verified": float(info.get("fund_nav", 0) or 0),
            "premium_rt": float(info.get("nav_discount_rt", 0) or 0),
            "volume": float(volume),
            "amount": float(amount),
            "turnover": turnover,
            "change_pct": 0,  # 集思录缓存无此字段
            "purchase_status": info.get("apply_status", "未知"),
            "purchase_status_source": "jisilu_cache",
            "daily_limit": 999999,  # 默认无限额（后续HTML核实更新）
            "source": "jisilu_cache",
        })
    return results


# ============================================================
#  东方财富LOF列表（主力数据源，无需登录）
# ============================================================

async def fetch_lof_from_eastmoney() -> Optional[list[dict]]:
    """从东方财富API获取所有LOF的实时行情（价格、成交量等）"""
    params = {
        "pn": "1", "pz": "200", "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2",
        "fid": "f3",
        "fs": "b:MK0404,b:MK0405,b:MK0406,b:MK0407",
        "fields": "f2,f3,f4,f12,f14,f15,f16,f17,f20,f21",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(EM_LOF_URL, params=params, headers=headers)
            data = resp.json()
        rows = data.get("data", {}).get("diff", [])
        if not rows:
            return None

        results = []
        for row in rows:
            code = str(row.get("f12", ""))
            name = str(row.get("f14", ""))
            price = _safe_float(row.get("f2", 0))
            amount = _safe_float(row.get("f20", 0))

            if not code or price <= 0 or amount < MIN_VOLUME:
                continue

            results.append({
                "code": code,
                "name": name,
                "price": price,
                "nav": 0,
                "premium_rt": 0,
                "volume": _safe_float(row.get("f15", 0)),
                "amount": amount,
                "apply_status": "未知",
                "nav_date": "",
                "issuer": "",
                "turnover_rt": 0,
                "source": "eastmoney",
            })

        print(f"[东方财富] 获取到 {len(results)} 只LOF行情数据")
        return results

    except Exception as e:
        print(f"[东方财富] 异常: {e}")
        return None


# ============================================================
#  Step 2: 天天基金净值核实
# ============================================================

async def _fetch_single_nav_ttjj(client: httpx.AsyncClient, code: str) -> Optional[dict]:
    """获取单只基金的最新净值（含净值日期和估算净值）"""
    url = TTJJ_URL.format(code=code)
    try:
        resp = await client.get(url)
        text = resp.text

        dwjz_match = re.search(r'"dwjz":"([^"]+)"', text)
        jzrq_match = re.search(r'"jzrq":"([^"]+)"', text)
        gsz_match = re.search(r'"gsz":"([^"]+)"', text)
        gztime_match = re.search(r'"gztime":"([^"]+)"', text)
        name_match = re.search(r'"name":"([^"]+)"', text)

        nav = _safe_float(dwjz_match.group(1)) if dwjz_match else 0.0
        nav_date = jzrq_match.group(1) if jzrq_match else ""
        est_nav = _safe_float(gsz_match.group(1)) if gsz_match else 0.0
        est_time = gztime_match.group(1) if gztime_match else ""
        name = name_match.group(1) if name_match else ""

        if nav > 0:
            return {
                "code": code,
                "name": name,
                "nav": nav,
                "nav_date": nav_date,
                "est_nav": est_nav,
                "est_time": est_time,
            }
        return None
    except Exception:
        return None


async def verify_navs_batch(candidates: list[dict]) -> list[dict]:
    """
    批量核实净值：对初筛到的候选LOF，到天天基金获取最新净值，
    重新计算「核实溢价率」=(场内价格 - 最新净值) / 最新净值 × 100%
    """
    if not candidates:
        return candidates

    to_verify = candidates[:NAV_VERIFY_MAX]
    print(f"[净值核实] 准备核实 {len(to_verify)} 只LOF的最新净值...")

    semaphore = asyncio.Semaphore(NAV_VERIFY_CONCURRENCY)
    verified_count = 0

    async def verify_one(item: dict) -> dict:
        nonlocal verified_count
        async with semaphore:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                result = await _fetch_single_nav_ttjj(client, item["code"])
                await asyncio.sleep(0.2)  # 礼貌间隔

        if result and result["nav"] > 0:
            verified_count += 1
            real_price = item["price"]
            real_nav = result["nav"]

            # 核实溢价率 = 当前场内价格 vs 天天基金最新净值
            if real_nav > 0:
                verified_premium = round((real_price - real_nav) / real_nav * 100, 2)
            else:
                verified_premium = item["premium_rt"]

            # 净值日期对比：集思录的 vs 天天基金的
            jsl_date = _parse_date(item.get("nav_date", ""))
            ttjj_date = _parse_date(result.get("nav_date", ""))

            # 判断核实效果
            if ttjj_date and jsl_date:
                days_diff = (ttjj_date - jsl_date).days
                if days_diff > 0:
                    verify_note = f"净值更新+{days_diff}天"
                elif days_diff == 0:
                    verify_note = "净值一致"
                else:
                    verify_note = f"净值早{abs(days_diff)}天"
            else:
                verify_note = "已核实"

            item["nav_verified"] = real_nav
            item["nav_date_verified"] = result["nav_date"]
            item["nav_date_jisilu"] = item.get("nav_date", "")
            item["premium_rt_jisilu"] = item["premium_rt"]
            item["premium_rt_verified"] = verified_premium
            item["est_nav"] = result.get("est_nav", 0)
            item["est_time"] = result.get("est_time", "")
            item["verify_note"] = verify_note
            item["verified"] = True

            premium_gap = item["premium_rt_jisilu"] - verified_premium
            if abs(premium_gap) > 1:
                print(f"  [!] {item['code']} {item['name']}: "
                      f"JSL={item['premium_rt_jisilu']:+.1f}% -> TTJJ={verified_premium:+.1f}% "
                      f"(nav {item['nav_date_jisilu']}->{result['nav_date']})")
        else:
            # 天天基金查不到，标记为未核实
            item["nav_verified"] = item["nav"]
            item["nav_date_verified"] = item.get("nav_date", "")
            item["nav_date_jisilu"] = item.get("nav_date", "")
            item["premium_rt_jisilu"] = item["premium_rt"]
            item["premium_rt_verified"] = item["premium_rt"]
            item["verify_note"] = "待核实"
            item["verified"] = False

        return item

    tasks = [verify_one(item) for item in to_verify]
    verified_results = await asyncio.gather(*tasks)

    # 未核实的部分保持原样
    unverified = candidates[NAV_VERIFY_MAX:]
    for item in unverified:
        item["nav_verified"] = item["nav"]
        item["nav_date_verified"] = item.get("nav_date", "")
        item["nav_date_jisilu"] = item.get("nav_date", "")
        item["premium_rt_jisilu"] = item["premium_rt"]
        item["premium_rt_verified"] = item["premium_rt"]
        item["verify_note"] = "批量待核实"
        item["verified"] = False

    print(f"[净值核实] 完成: {verified_count}/{len(to_verify)} 核实成功")
    return verified_results + unverified


# ============================================================
#  Step 3: 公告核实（基金主页解析）
# ============================================================

# 订阅相关公告关键词
SUBSCRIPTION_KEYWORDS = [
    "暂停申购", "暂停大额申购", "限制大额申购", "限制申购",
    "暂停定期定额", "恢复申购", "恢复大额申购",
    "调整大额申购", "调整申购金额", "暂停转换转入",
]

SUSPEND_KEYWORDS = ["暂停申购", "暂停大额申购", "暂停定期定额", "暂停转换转入"]
LIMIT_KEYWORDS = ["限制大额申购", "限制申购", "调整大额申购", "调整申购金额"]
RESUME_KEYWORDS = ["恢复申购", "恢复大额申购", "恢复定期定额"]
RESUME_TRADE_KEYWORDS = ["复牌公告", "复牌"]

# 停牌/复牌
TRADE_STATUS_KEYWORDS = [
    "停牌公告", "临时停牌", "停复牌", "复牌公告",
    "溢价风险提示", "交易风险提示",
]


async def _fetch_fund_page_html(client: httpx.AsyncClient, code: str) -> Optional[str]:
    """获取基金主页HTML"""
    url = f"https://fund.eastmoney.com/{code}.html"
    try:
        resp = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://fund.eastmoney.com/",
        })
        resp.encoding = "utf-8"
        return resp.text
    except Exception as e:
        print(f"[公告核实] 获取{code}页面失败: {e}")
        return None


def _parse_purchase_status_from_html(html: str) -> dict:
    """从基金主页HTML中解析申购状态和限额
    支持的HTML模式：
    - 交易状态：暂停申购 （单日累计购买上限10元） 开放赎回
    - 交易状态：开放申购 开放赎回
    - 申购状态：开放申购
    - 交易状态：暂停申购 暂停赎回（定开基金）
    """
    result = {
        "purchase_status_raw": "",
        "daily_limit_raw": "",
        "fee_raw": "",
    }

    # ---------- 申购状态 ----------
    # 模式1: "交易状态：XXX" 或 "申购状态：XXX"（最常见）
    # HTML格式可能是: 交易状态：</span><span class="staticCell">暂停申购 ...</span>
    # 先用宽松匹配取整段文本，再strip HTML标签
    trade_status_match = re.search(
        r'(?:交易状态|申购状态)[：:]\s*'
        r'((?:<[^>]+>)*\s*[^<]+(?:\s*<[^>]+>[^<]*)*)',  # 匹配HTML标签包裹的状态文本
        html
    )
    if trade_status_match:
        raw = trade_status_match.group(1).strip()
        # 清理HTML残留 → 纯文本
        raw = re.sub(r'<[^>]+>', '', raw)
        raw = re.sub(r'\s+', ' ', raw).strip()
        result["purchase_status_raw"] = raw

    # 如果上面的正则没匹配到（可能是纯文本无HTML标签的情况），降级尝试
    if not result["purchase_status_raw"]:
        # 尝试匹配到下一个HTML块结束或下一个"XX状态"标签
        fallback = re.search(
            r'(?:交易状态|申购状态)[：:]\s*(.+?)(?=<(?:/td|/div|br)|(?:开放|暂停)(?:申购|赎回|转换|定投)(?!\s*(?:上限|金额))|$)',
            html
        )
        if fallback:
            raw = fallback.group(1).strip()
            raw = re.sub(r'<[^>]+>', '', raw)
            raw = re.sub(r'\s+', ' ', raw).strip()
            if raw:
                result["purchase_status_raw"] = raw

    # 模式2: JS变量 / data属性（备用）
    if not result["purchase_status_raw"]:
        js_patterns = [
            r'"buyStatus"\s*:\s*"([^"]+)"',
            r'"sgzt"\s*:\s*"([^"]+)"',
            r'data-purchasestatus="([^"]+)"',
        ]
        for pattern in js_patterns:
            match = re.search(pattern, html)
            if match:
                result["purchase_status_raw"] = match.group(1).strip()
                break

    # ---------- 限额 ----------
    # 模式1: "单日累计购买上限XX元"（在交易状态行中）
    limit_match = re.search(
        r'单日(?:累计)?(?:购买|申购)上限[：:：\s]*([\d,.]+)\s*(?:元|万)?',
        html
    )
    if limit_match:
        raw_limit = limit_match.group(1).replace(',', '')
        try:
            result["daily_limit_raw"] = str(float(raw_limit))
        except ValueError:
            result["daily_limit_raw"] = raw_limit

    # 模式2: JS变量
    if not result["daily_limit_raw"]:
        js_limit = re.search(r'"dayMaxAmt"\s*:\s*"?([^",}]+)"?', html)
        if js_limit:
            result["daily_limit_raw"] = js_limit.group(1).strip()

    return result


def _parse_announcements_from_html(html: str) -> list[dict]:
    """从基金主页HTML中提取最近公告"""
    announcements = []

    # 公告区域通常在包含 "基金公告" 标签附近
    # 模式: 公告标题(日期) 或 data-属性
    ann_patterns = [
        # 模式1: <a>标签中包含公告标题和日期
        r'<a[^>]*href="[^"]*gonggao[^"]*"[^>]*title="([^"]*)"[^>]*>\s*(?:<span[^>]*>)?([^<]*(?:暂停|限制|恢复|停牌|申购|赎回)[^<]*)',
        # 模式2: 公告标题和日期分离
        r'<li[^>]*>\s*<a[^>]*href="[^"]*gonggao[^"]*"[^>]*>([^<]*)</a>\s*<span[^>]*>(\d{2}-\d{2})</span>',
        # 模式3: 脚本中的公告数据
        r'"title"\s*:\s*"([^"]*(?:暂停|限制|恢复|停牌|申购|赎[^"]{0,20})[^"]*)"[^}]*?"pubdate"\s*:\s*"([^"]+)"',
    ]
    for pattern in ann_patterns:
        for match in re.finditer(pattern, html, re.IGNORECASE | re.DOTALL):
            groups = match.groups()
            title = ""
            date = ""
            if len(groups) >= 1:
                title = groups[0].strip()
            if len(groups) >= 2:
                date = groups[1].strip()

            # 过滤：只要最近7天的
            if title and any(kw in title for kw in
                             SUBSCRIPTION_KEYWORDS + TRADE_STATUS_KEYWORDS):
                announcements.append({
                    "title": title[:80],
                    "date": date,
                })

    return announcements[:10]


def _normalize_purchase_status(raw_status: str) -> dict:
    """将HTML中解析的原始状态标准化
    返回: {"status": "开放申购|暂停申购|限大额|封闭期|未知", "daily_limit": 0}
    """
    if not raw_status:
        return {"status": "未知", "daily_limit": 0}

    raw = raw_status.strip()

    # 提取单日限额
    daily_limit = 0
    limit_match = re.search(r'单日(?:累计)?(?:购买|申购)上限[：:：\s]*([\d,.]+)\s*(?:元|万)?', raw)
    if limit_match:
        try:
            daily_limit = float(limit_match.group(1).replace(',', ''))
        except ValueError:
            pass

    # 判断状态类型
    if any(kw in raw for kw in ["封闭期", "暂停申购", "暂停赎回"]):
        # 如果是定开基金（同时暂停申购和赎回），标记为"封闭期"
        if "暂停申购" in raw and "暂停赎回" in raw:
            return {"status": "封闭期(定开)", "daily_limit": 0}
        return {"status": "暂停申购", "daily_limit": daily_limit}
    elif "开放申购" in raw or "开放赎回" in raw:
        if daily_limit > 0 and daily_limit < 100:
            return {"status": "限大额", "daily_limit": daily_limit}
        return {"status": "开放申购", "daily_limit": daily_limit if daily_limit > 0 else 999999}
    elif "限" in raw or "限制" in raw:
        return {"status": "限大额", "daily_limit": daily_limit}

    return {"status": raw_status[:8], "daily_limit": daily_limit}


def _check_announcement_concerns(announcements: list[dict]) -> dict:
    """分析公告内容，判断是否有需要关注的变动"""
    concerns = {
        "has_suspend_risk": False,
        "has_limit_risk": False,
        "has_resume_signal": False,
        "has_trade_risk": False,
        "matched_anns": [],
        "risk_level": "safe",
        "risk_note": "",
    }

    for ann in announcements:
        title = ann.get("title", "")
        date = ann.get("date", "")

        for kw in SUSPEND_KEYWORDS:
            if kw in title:
                concerns["has_suspend_risk"] = True
                concerns["matched_anns"].append(f"⚠ {date} {title}")
                break
        for kw in LIMIT_KEYWORDS:
            if kw in title:
                concerns["has_limit_risk"] = True
                concerns["matched_anns"].append(f"🔶 {date} {title}")
                break
        for kw in RESUME_KEYWORDS:
            if kw in title:
                concerns["has_resume_signal"] = True
                concerns["matched_anns"].append(f"🔄 {date} {title}")
                break
        for kw in RESUME_TRADE_KEYWORDS:
            if kw in title:
                concerns["has_trade_risk"] = False  # 复牌是正面信号
                concerns["has_resume_signal"] = True
                concerns["matched_anns"].append(f"🔄 {date} {title}")
                break
        for kw in TRADE_STATUS_KEYWORDS:
            if kw in title:
                concerns["has_trade_risk"] = True
                concerns["matched_anns"].append(f"⚠ {date} {title}")
                break

    # 风险评估：恢复信号优先于暂停信号
    # 如果有恢复信号，说明之前的状态已解除
    if concerns["has_resume_signal"]:
        concerns["risk_level"] = "resume"
        concerns["risk_note"] = "检测到恢复申购/复牌信号，可能是新机会！请手动确认"
    elif concerns["has_suspend_risk"] or concerns["has_trade_risk"]:
        concerns["risk_level"] = "danger"
        concerns["risk_note"] = "有暂停申购或停牌公告，不建议操作"
    elif concerns["has_limit_risk"]:
        concerns["risk_level"] = "warning"
        concerns["risk_note"] = "有限额调整公告，需确认当前限额"
    else:
        concerns["risk_note"] = "公告无异常"

    return concerns


async def verify_announcements_batch(candidates: list[dict]) -> list[dict]:
    """
    Step 3: 批量核实在公告
    对最终入选的LOF，到天天基金主页核实申购状态和最新公告
    """
    if not candidates:
        return candidates

    print(f"[公告核实] 准备核实 {len(candidates)} 只LOF的最新公告...")

    semaphore = asyncio.Semaphore(NAV_VERIFY_CONCURRENCY)
    checked_count = 0
    warned_count = 0

    async def check_one(item: dict) -> dict:
        nonlocal checked_count, warned_count
        async with semaphore:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                html = await _fetch_fund_page_html(client, item["code"])
                await asyncio.sleep(0.3)

        if html:
            checked_count += 1
            # 解析申购状态（改进版：支持"交易状态"格式）
            status_info = _parse_purchase_status_from_html(html)

            # 标准化申购状态
            if status_info.get("purchase_status_raw"):
                normalized = _normalize_purchase_status(status_info["purchase_status_raw"])
                html_status = normalized["status"]
                html_limit = normalized["daily_limit"]

                # 用HTML解析结果覆盖未知状态
                current_status = item.get("purchase_status", "未知")
                if current_status == "未知" or not current_status:
                    item["purchase_status"] = html_status
                    item["daily_limit"] = html_limit
                    item["purchase_status_source"] = "html_verified"
                    if html_status != "未知":
                        print(f"  ✓ {item['code']} {item['name']}: 申购状态 = {html_status}"
                              + (f" (限额{html_limit:.0f}元)" if html_limit > 0 and html_limit < 999999 else ""))
                elif "暂停" in html_status and "暂停" not in current_status:
                    # 状态变更：原来是开放/未知，现在HTML显示暂停 → 立即更新
                    print(f"  🚨 {item['code']} {item['name']}: 申购状态变更 → {html_status}")
                    item["purchase_status"] = html_status
                    item["daily_limit"] = html_limit
                    item["purchase_status_source"] = "html_verified"
                    warned_count += 1

            # 解析公告
            announcements = _parse_announcements_from_html(html)
            concerns = _check_announcement_concerns(announcements)

            item["announcement_checked"] = True
            item["ann_risk_level"] = concerns["risk_level"]
            item["ann_risk_note"] = concerns["risk_note"]
            item["ann_matched"] = concerns["matched_anns"]

            # 根据公告结果更新申购状态
            if concerns["risk_level"] == "danger":
                warned_count += 1
                item["purchase_status"] = "暂停申购(公告确认)"
                item["daily_limit"] = 0
            elif concerns["risk_level"] == "resume":
                # 恢复信号：以前是暂停的现在可能已恢复，标记为需要手动确认
                if item.get("purchase_status", "").find("暂停") >= 0:
                    item["purchase_status"] = "开放申购(公告恢复)"
                    print(f"  🔄 {item['code']} {item['name']}: 检测到恢复申购信号")
            elif concerns["risk_level"] == "warning":
                warned_count += 1
        else:
            item["announcement_checked"] = False
            item["ann_risk_level"] = "unknown"
            item["ann_risk_note"] = "无法获取公告"
            item["ann_matched"] = []

        return item

    tasks = [check_one(item) for item in candidates[:NAV_VERIFY_MAX]]
    verified = await asyncio.gather(*tasks)

    print(f"[公告核实] 完成: {checked_count}/{len(candidates)} 核实成功, {warned_count} 有风险")
    return verified


# ============================================================
#  申购状态获取
# ============================================================

async def fetch_purchase_limits() -> dict[str, dict]:
    """从天天基金API获取所有基金的申购限购信息"""
    url = "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFBPurchaseLimit"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://fund.eastmoney.com/",
    }
    all_data = {}
    page_index = 1
    max_pages = 5

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            while page_index <= max_pages:
                params = {
                    "pageIndex": str(page_index),
                    "pageSize": "500",
                    "Sort": "CODE",
                    "SortOrder": "ASC",
                    "FundType": "",
                    "deviceid": "web",
                    "plat": "web",
                    "product": "EFund",
                    "version": "1.0.0",
                }
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code != 200:
                    break
                data = resp.json()
                if data.get("ErrCode") != 0:
                    break
                fund_list = data.get("Data", {}).get("FundList", [])
                if not fund_list:
                    break
                for fund in fund_list:
                    code = fund.get("FCODE", "")
                    if code:
                        all_data[code] = {
                            "name": fund.get("SHORTNAME", ""),
                            "purchase_status": fund.get("SGTEXT", "未知"),
                            "daily_limit": _safe_float(fund.get("DAYMAXAMT", 0)),
                            "min_purchase": _safe_float(fund.get("MINTIMEMINAMT", 0)),
                            "purchase_fee": _safe_float(fund.get("RATE", 0)),
                            "fund_type": fund.get("FTYPE", ""),
                        }
                total_pages = data.get("Data", {}).get("TotalPages", 1)
                if page_index >= total_pages:
                    break
                page_index += 1
                await asyncio.sleep(0.3)

        print(f"[申购限额] 获取到 {len(all_data)} 只基金的限购数据")
        return all_data

    except Exception as e:
        print(f"[申购限额] 异常: {e}")
        return {}


# ============================================================
#  腾讯财经实时价格（备用）
# ============================================================

async def fetch_tencent_prices(codes: list[str]) -> dict[str, dict]:
    """从腾讯财经获取实时场内价格（备用）"""
    if not codes:
        return {}
    query_parts = []
    for code in codes:
        prefix = "sh" if code.startswith("5") else "sz"
        query_parts.append(f"{prefix}{code}")
    query = ",".join(query_parts[:50])
    url = f"{TENCENT_URL}{query}"

    results = {}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(url)
            resp.encoding = "gbk"
            text = resp.text
            for line in text.strip().split("\n"):
                if "=" not in line:
                    continue
                match = re.search(r'v_(\w+)="(.+)"', line)
                if not match:
                    continue
                raw_code = match.group(1)
                code = raw_code[2:] if raw_code.startswith(("sh", "sz")) else raw_code
                fields = match.group(2).split("~")
                if len(fields) < 10:
                    continue
                try:
                    price = float(fields[3]) if fields[3] else 0
                except ValueError:
                    price = 0
                results[code] = {
                    "price": price,
                    "name": fields[1],
                    "change_pct": _safe_float(fields[32]),
                    "volume": _safe_float(fields[6]),
                }
        print(f"[腾讯财经] 获取到 {len(results)} 只LOF价格")
    except Exception as e:
        print(f"[腾讯财经] 异常: {e}")

    return results


# ============================================================
#  AKShare备用
# ============================================================

async def fetch_lof_data_akshare() -> Optional[list[dict]]:
    """通过AKShare获取LOF数据（备用方案）"""
    try:
        import akshare as ak
        from concurrent.futures import ThreadPoolExecutor

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            df = await loop.run_in_executor(pool, ak.fund_lof_spot_em)

        if df is None or df.empty:
            return None

        results = []
        for _, row in df.iterrows():
            code = str(row.get("基金代码", ""))
            name = str(row.get("基金简称", ""))
            price = _safe_float(row.get("最新价", 0))
            volume = _safe_float(row.get("成交量", 0))
            amount = _safe_float(row.get("成交额", 0))

            if price <= 0:
                continue

            results.append({
                "code": code,
                "name": name,
                "price": price,
                "nav": 0,
                "premium_rt": 0,
                "volume": volume,
                "amount": amount,
                "apply_status": "未知",
                "nav_date": "",
                "issuer": "",
                "turnover_rt": 0,
                "source": "akshare",
            })

        print(f"[AKShare] 获取到 {len(results)} 只LOF行情数据")
        return results

    except ImportError:
        print("[AKShare] 模块未安装")
        return None
    except Exception as e:
        print(f"[AKShare] 异常: {e}")
        return None


# ============================================================
#  数据富化与筛选
# ============================================================

def enrich_with_purchase_limits(lof_data: list[dict], limits: dict[str, dict]) -> list[dict]:
    """将申购限购数据合并到LOF数据中"""
    for item in lof_data:
        code = item["code"]
        if code in limits:
            lim = limits[code]
            item["purchase_status"] = lim.get("purchase_status", item.get("apply_status", "未知"))
            item["daily_limit"] = lim.get("daily_limit", 0)
            item["min_purchase"] = lim.get("min_purchase", 0)
            item["purchase_fee"] = lim.get("purchase_fee", 0)
        else:
            item["purchase_status"] = item.get("apply_status", "未知")
            item["daily_limit"] = 0
            item["min_purchase"] = 0
            item["purchase_fee"] = 0
    return lof_data


def filter_arbitrage_opportunities(data: list[dict]) -> list[dict]:
    """
    最终筛选：用「核实溢价率」做筛选条件
    不按申购状态过滤——暂停申购的也展示，让用户自行判断
    """
    opportunities = []
    for item in data:
        premium = item.get("premium_rt_verified", item.get("premium_rt", 0))
        amount = item.get("amount", 0)
        price = item.get("price", 0)
        nav = item.get("nav_verified", item.get("nav", 0))

        if abs(premium) < PREMIUM_THRESHOLD:
            continue
        if price <= 0 or nav <= 0:
            continue
        if amount < MIN_VOLUME:
            continue

        is_premium = premium > 0
        verified = item.get("verified", False)

        # 成交量分级
        if amount >= MIN_VOLUME_OK:
            volume_level = "high"
        elif amount >= MIN_VOLUME_WARN:
            volume_level = "mid"
        elif amount >= MIN_VOLUME:
            volume_level = "low"
        else:
            volume_level = "none"

        # 扣费后的净溢价（申购费1折0.15% + 卖出佣金0.01% ≈ 0.16%）
        purchase_fee = item.get("purchase_fee", 0.15)
        net_premium = round(premium - purchase_fee - 0.01, 2) if premium > 0 else premium

        opportunities.append({
            **item,
            "premium_rt": premium,
            "nav": nav,
            "direction": "溢价套利" if is_premium else "折价套利",
            "is_premium": is_premium,
            "verified": verified,
            "volume_level": volume_level,
            "net_premium": net_premium,
            "signal_score": _calc_signal_score(premium, amount, item.get("turnover_rt", 0)),
            # 公告链接
            "ann_url": f"https://fundf10.eastmoney.com/jjgg_{item['code']}.html",
        })

    opportunities.sort(key=lambda x: abs(x["premium_rt"]), reverse=True)
    return opportunities


def _check_purchase_status(status: str) -> bool:
    status_lower = status.lower() if status else ""
    blocked = ["暂停", "suspend", "封闭", "close"]
    return not any(kw in status_lower for kw in blocked)


def _calc_signal_score(premium_rt: float, amount: float, turnover: float) -> int:
    score = min(60, abs(premium_rt) * 8)
    if amount > 1e7:
        score += 20
    elif amount > 1e6:
        score += 10
    if turnover > 5:
        score += 10
    elif turnover > 2:
        score += 5
    return min(100, int(score))


# ============================================================
#  模拟数据（含核实对比效果）
# ============================================================

def generate_mock_data() -> list[dict]:
    """
    生成模拟数据，包含「显示溢价」vs「核实溢价」的对比
    模拟场景：
    - 前3个：显示溢价高但核实后大幅缩水（T-2净值过期导致虚高）
    - 中间3个：显示溢价和核实基本一致
    - 最后2个：核实溢价仍然超过阈值，真机会
    """
    mock_funds = [
        # code, name, price, jsl_nav, jsl_date, ttjj_nav, ttjj_date, premium_apparent, amount, status, limit
        ("161226", "国投白银LOF",    1.085, 0.962, "2026-05-28", 1.052, "2026-05-30", 9.2,   3.1,   85600000, "开放申购", 100),
        ("161128", "标普信息科技LOF",  1.568, 1.445, "2026-05-28", 1.538, "2026-05-30", 6.8,   2.0,   55000000, "开放申购", 2000),
        ("164824", "印度基金LOF",      1.315, 1.285, "2026-05-28", 1.308, "2026-05-30", 2.3,   0.5,   12000000, "限大额", 500),
        # ↑ 上面3个：净值日期差2天，核实后溢价缩水
        ("161116", "易方达黄金LOF",    1.152, 1.085, "2026-05-30", 1.089, "2026-05-30", 6.5,   5.8,   62100000, "开放申购", 5000),
        ("160723", "嘉实原油LOF",      1.198, 1.145, "2026-05-30", 1.150, "2026-05-30", 3.8,   4.2,   32000000, "限大额", 100),
        ("163208", "全球油气能源LOF",  1.456, 1.385, "2026-05-30", 1.390, "2026-05-30", 3.2,   4.7,   18000000, "开放申购", 50000),
        # ↑ 上面3个：净值日期一致，核实后溢价接近
        ("160644", "港美互联网LOF",    1.385, 1.302, "2026-05-29", 1.342, "2026-05-30", 5.0,   3.2,   42000000, "暂停申购", 0),
        ("501018", "南方原油LOF",      1.228, 1.175, "2026-05-29", 1.190, "2026-05-30", 3.5,   3.2,   38000000, "限大额", 1000),
        # ↑ 上面2个：净值日期差1天，核实后溢价适度下调
        ("161129", "原油LOF易方达",    1.312, 1.268, "2026-05-30", 1.270, "2026-05-30", 2.8,   3.3,   25000000, "暂停申购", 0),
        # ↑ 这个核实溢价>3%但暂停申购 → danger → 被过滤
        # 恢复申购示例：之前暂停，最近有恢复公告
        ("160140", "美国REIT精选LOF",   1.445, 1.380, "2026-05-30", 1.385, "2026-05-30", 5.2,   4.3,   15000000, "开放申购(公告恢复)", 5000),
    ]

    results = []
    for fund in mock_funds:
        code, name, price, jsl_nav, jsl_date, ttjj_nav, ttjj_date, premium_apparent, premium_real, amount, status, limit = fund
        is_premium = premium_real > 0
        verified_premium = round(premium_real, 2)
        apparent_premium = round(premium_apparent, 2)

        # 判断核实效果
        jsl_dt = _parse_date(jsl_date)
        ttjj_dt = _parse_date(ttjj_date)
        if jsl_dt and ttjj_dt:
            days_diff = (ttjj_dt - jsl_dt).days
            if days_diff > 0:
                verify_note = f"净值更新+{days_diff}天"
            elif days_diff == 0:
                verify_note = "净值一致"
            else:
                verify_note = f"净值早{abs(days_diff)}天"
        else:
            verify_note = "已核实"

        results.append({
            "code": code,
            "name": name,
            "price": round(price, 3),
            "nav": ttjj_nav,
            "nav_verified": ttjj_nav,
            "nav_date": ttjj_date,
            "nav_date_verified": ttjj_date,
            "nav_date_jisilu": jsl_date,
            "premium_rt": verified_premium,
            "premium_rt_verified": verified_premium,
            "premium_rt_jisilu": apparent_premium,
            "volume": amount / 10000,
            "amount": amount,
            "purchase_status": status,
            "daily_limit": limit,
            "min_purchase": 10,
            "purchase_fee": 0.15,
            "issuer": "模拟基金公司",
            "turnover_rt": round(amount / 1e8 * 100, 2),
            "source": "mock",
            "verified": True,
            "verify_note": verify_note,
            "direction": "溢价套利" if is_premium else "折价套利",
            "is_premium": is_premium,
            "signal_score": min(90, int(abs(verified_premium) * 8) + 30),
            # 公告核实字段
            "announcement_checked": True,
            "ann_risk_level": (
                "resume" if status.find("恢复") >= 0
                else "danger" if status.find("暂停") >= 0
                else "safe"
            ),
            "ann_risk_note": (
                "检测到恢复申购信号，可能是新机会！" if status.find("恢复") >= 0
                else "暂停申购公告已确认" if status.find("暂停") >= 0
                else "公告无异常"
            ),
            "ann_matched": (
                ["🔄 06-01 恢复申购及定期定额投资业务"] if status.find("恢复") >= 0
                else (["⚠ 05-27 暂停申购和定期定额投资"] if status.find("暂停") >= 0
                else (["🔶 05-25 限制大额申购金额上限调整为500元"] if status.find("限大额") >= 0 else []))
            ),
            "ann_url": f"https://fundf10.eastmoney.com/jjgg_{code}.html",
        })

    return results


# ============================================================
#  主流程
# ============================================================

async def get_all_lof_arbitrage_opportunities(use_mock: bool = False) -> dict:
    """
    主函数：两步核实法获取LOF套利机会

    Step 1: 集思录初筛（低阈值 PREMIUM_PRESCREEN）
    Step 2: 天天基金净值核实 → 重新计算溢价率
    Step 3: 最终筛选（阈值 PREMIUM_THRESHOLD）
    """
    if use_mock:
        print("[数据源] 使用模拟数据")
        raw_data = generate_mock_data()
        opportunities = filter_arbitrage_opportunities(raw_data)
        return {
            "success": True,
            "source": "mock",
            "count": len(opportunities),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data": opportunities,
        }

    # Step 1: 东方财富LOF列表（主力，无需登录，数据最全）
    lof_data = await fetch_lof_from_eastmoney()
    if lof_data is None or len(lof_data) == 0:
        print("[主流程] 东方财富失败或无数据，尝试集思录...")
        lof_data = await fetch_jisilu_lof()

    if lof_data is None or len(lof_data) == 0:
        print("[主流程] 集思录失败或无候选，尝试AKShare...")
        lof_data = await fetch_lof_data_akshare()

    if lof_data is None or len(lof_data) == 0:
        # 最终兜底：直接读取 .jisilu_data.json 作为主数据源
        print("[主流程] AKShare失败或无候选，尝试本地集思录缓存作为主数据源...")
        jisilu_full = await fetch_jisilu_data_full()
        if jisilu_full:
            lof_data = _jisilu_dict_to_lof_list(jisilu_full)
            if lof_data:
                print(f"[主流程] ✅ 本地集思录缓存可用: {len(lof_data)} 只LOF")

    if lof_data is None:
        print("[主流程] 所有数据源均失败")
        return {
            "success": False,
            "error": "所有数据源均无法访问，请稍后重试",
            "source": "none",
            "count": 0,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data": [],
        }

    # Step 2: 净值核实（按成交额排序，优先核对流动性好的LOF）
    lof_data.sort(key=lambda x: x.get("amount", 0), reverse=True)
    lof_data = await verify_navs_batch(lof_data)

    # Step 3: 合并申购限购信息
    try:
        limits = await fetch_purchase_limits()
    except Exception:
        limits = {}

    lof_data = enrich_with_purchase_limits(lof_data, limits)

    # Step 3.5: 用集思录申购状态覆盖东财的"未知"
    try:
        jisilu_info = await fetch_jisilu_data_full()
        if jisilu_info:
            lof_data = enrich_with_jisilu_status(lof_data, jisilu_info)
    except Exception as e:
        print(f"[主流程] 集思录数据获取失败（不影响主流程）: {e}")

    # Step 4: 最终筛选
    opportunities = filter_arbitrage_opportunities(lof_data)

    # Step 5: 公告核实（针对溢价达标 + 净值核实的LOF，不限申购状态）
    # 注意：不在此处过滤！暂停申购的也展示，让用户自行判断
    if opportunities:
        opportunities = await verify_announcements_batch(opportunities)

    return {
        "success": True,
        "source": lof_data[0]["source"] if lof_data else "unknown",
        "count": len(opportunities),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": opportunities,
    }


if __name__ == "__main__":
    async def main():
        result = await get_all_lof_arbitrage_opportunities()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    asyncio.run(main())
