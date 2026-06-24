"""
LOF套利盈亏跟踪模块

逻辑：
- 每日自动检测溢价>3%且开放申购/限大额的LOF品种
- 假设每只符合条件的新品种用5000元申购（按当日净值买入）
- T+2日LOF到场内后可交易，以当日开盘价模拟卖出
- 跟踪持仓盈亏（当天未卖出的以实时价格计算浮盈亏）

费用：
- 申购费：0.15%（按1折计算）
- 卖出佣金：0.01%
"""
import json
import os
from datetime import datetime, date, timedelta
from typing import Optional

# 交易日志路径
TRADE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".trades.json")

# 费用率
PURCHASE_FEE_RATE = 0.0015   # 申购费 0.15%
SELL_FEE_RATE = 0.0001       # 卖出佣金 0.01%
BUY_AMOUNT = 5000             # 每次申购金额（元）
SETTLE_DAYS = 2               # T+2日到账


def _load_trades() -> dict:
    """加载交易日志"""
    if not os.path.exists(TRADE_FILE):
        return {"trades": [], "summary": {"total_invested": 0, "total_realized_pnl": 0, "total_unrealized_pnl": 0}}
    try:
        with open(TRADE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {"trades": [], "summary": {"total_invested": 0, "total_realized_pnl": 0, "total_unrealized_pnl": 0}}


def _save_trades(data: dict) -> None:
    """保存交易日志"""
    with open(TRADE_FILE, "r+b" if os.path.exists(TRADE_FILE) else "wb") as f:
        pass  # touch
    with open(TRADE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def record_buy(code: str, name: str, nav: float, premium_rt: float, daily_limit: float = 0) -> Optional[dict]:
    """
    记录一笔模拟买入
    - 同一只基金同一天不重复买入
    - 计算买入份额 = BUY_AMOUNT / nav（扣除申购费）
    """
    data = _load_trades()
    today = get_today_str()

    # 检查今天是否已经买入同一只基金
    for trade in data["trades"]:
        if trade["code"] == code and trade["buy_date"] == today:
            print(f"[交易] {code} {name} 今日已买入，跳过")
            return None

    if nav <= 0:
        print(f"[交易] {code} {name} 净值异常({nav})，跳过")
        return None

    # 计算实际买入金额（扣除申购费后）
    fee = BUY_AMOUNT * PURCHASE_FEE_RATE
    actual_invest = BUY_AMOUNT - fee
    units = actual_invest / nav

    trade = {
        "code": code,
        "name": name,
        "buy_date": today,
        "buy_nav": round(nav, 4),
        "buy_amount": BUY_AMOUNT,
        "fee_purchase": round(fee, 2),
        "units": round(units, 2),
        "premium_rt_at_buy": round(premium_rt, 2),
        "daily_limit_at_buy": daily_limit,
        "status": "holding",       # holding / sold / expired
        "settle_date": (datetime.now() + timedelta(days=SETTLE_DAYS)).strftime("%Y-%m-%d"),
        "sell_date": None,
        "sell_price": None,
        "sell_fee": 0,
        "realized_pnl": 0,
        "note": "",
    }

    data["trades"].append(trade)
    _save_trades(data)

    print(f"[交易] 买入 {code} {name}: {units:.0f}份 @ NAV={nav:.4f}, 溢价{premium_rt:+.1f}%, 投入￥{BUY_AMOUNT}")
    return trade


def record_sell(code: str, sell_date: str, sell_price: float, note: str = "") -> Optional[dict]:
    """记录卖出（按开盘价）"""
    data = _load_trades()

    for trade in data["trades"]:
        if trade["code"] == code and trade["status"] == "holding":
            units = trade["units"]
            gross = units * sell_price
            fee = gross * SELL_FEE_RATE
            net = gross - fee
            cost = trade["buy_amount"]
            pnl = net - cost

            trade["status"] = "sold"
            trade["sell_date"] = sell_date
            trade["sell_price"] = round(sell_price, 4)
            trade["sell_fee"] = round(fee, 2)
            trade["realized_pnl"] = round(pnl, 2)
            trade["note"] = note

            # 更新汇总
            data["summary"]["total_realized_pnl"] = round(
                data["summary"].get("total_realized_pnl", 0) + pnl, 2
            )

            _save_trades(data)
            print(f"[交易] 卖出 {code} {trade['name']}: {units:.0f}份 @ {sell_price:.4f}, 盈亏￥{pnl:+.2f}")
            return trade

    print(f"[交易] {code} 没有持仓可卖出")
    return None


def update_market_prices(prices: dict[str, float]) -> list[dict]:
    """
    更新所有持仓的市场价格并计算浮盈亏，同时持久化到文件
    prices: {code: current_price}

    返回：持仓列表（含浮盈计算）
    """
    data = _load_trades()
    holdings = []

    for trade in data["trades"]:
        if trade["status"] != "holding":
            continue

        code = trade["code"]
        current_price = prices.get(code, 0)
        units = trade["units"]
        cost = trade["buy_amount"]

        if current_price > 0:
            current_value = units * current_price
            unrealized_pnl = current_value - cost

            # 如果已到结算日，按当前价格计算"可卖出盈亏"
            settle_date = datetime.strptime(trade["settle_date"], "%Y-%m-%d")
            can_sell = datetime.now() >= settle_date

            # 持久化到trade记录中
            trade["current_price"] = round(current_price, 4)
            trade["current_value"] = round(current_value, 2)
            trade["unrealized_pnl"] = round(unrealized_pnl, 2)
            trade["unrealized_pnl_pct"] = round(unrealized_pnl / cost * 100, 2) if cost > 0 else 0
            trade["can_sell"] = can_sell
            trade["days_held"] = (datetime.now() - datetime.strptime(trade["buy_date"], "%Y-%m-%d")).days

            holdings.append({**trade})
        else:
            trade["current_price"] = 0
            trade["current_value"] = 0
            trade["unrealized_pnl"] = 0
            trade["unrealized_pnl_pct"] = 0
            trade["can_sell"] = False
            trade["days_held"] = (datetime.now() - datetime.strptime(trade["buy_date"], "%Y-%m-%d")).days

            holdings.append({**trade})

    # 更新汇总
    total_unrealized = sum(h.get("unrealized_pnl", 0) for h in holdings)
    data["summary"]["total_unrealized_pnl"] = round(total_unrealized, 2)
    data["summary"]["total_invested"] = len(data["trades"]) * BUY_AMOUNT
    _save_trades(data)

    return holdings


def get_holdings_summary() -> dict:
    """获取持仓汇总"""
    data = _load_trades()
    holdings = [t for t in data["trades"] if t["status"] == "holding"]
    sold = [t for t in data["trades"] if t["status"] == "sold"]

    total_realized = sum(t.get("realized_pnl", 0) for t in sold)
    total_invested = len(data["trades"]) * BUY_AMOUNT

    return {
        "total_trades": len(data["trades"]),
        "active_holdings": len(holdings),
        "closed_trades": len(sold),
        "total_invested": total_invested,
        "total_realized_pnl": round(total_realized, 2),
        "total_realized_pnl_pct": round(total_realized / total_invested * 100, 2) if total_invested > 0 else 0,
        "holdings": holdings,
        "closed": sold[-10:],  # 最近10笔已平仓
    }


def auto_buy_opportunities(opportunities: list[dict]) -> list[dict]:
    """
    对符合条件的机会自动模拟买入
    条件：溢价>3% + 开放申购（或限大额且日限额>=100）+ 成交额>1万
    有色/资源类LOF溢价需>5%才买入
    返回：新买入的交易列表
    """
    # 有色/资源类关键词（与fetcher.py保持一致）
    RESOURCE_KW = ["有色", "资源", "大宗商品", "煤炭", "钢铁", "矿业", "黄金", "白银"]

    def _is_resource(name: str) -> bool:
        return any(kw in name for kw in RESOURCE_KW)

    new_trades = []
    today = get_today_str()

    for opp in opportunities:
        # 只需溢价品种
        if not opp.get("is_premium", False):
            continue

        # 申购状态必须是开放申购或限大额（且限额>=100元）
        status = opp.get("purchase_status", "未知")
        daily_limit = opp.get("daily_limit", 0)

        if "暂停" in status or "封闭" in status:
            continue
        if "限" in status and daily_limit > 0 and daily_limit < 100:
            continue
        # 状态未知的不自动买入（保守）
        if status == "未知":
            continue

        # 成交额检查
        if opp.get("amount", 0) < 10000:
            continue

        # 溢价检查
        premium = opp.get("premium_rt", 0)
        name = opp.get("name", "")
        min_premium = 5.0 if _is_resource(name) else 3.0
        if abs(premium) < min_premium:
            continue

        # 使用核实后的净值
        nav = opp.get("nav_verified", opp.get("nav", 0))
        if nav <= 0:
            continue

        trade = record_buy(
            code=opp["code"],
            name=opp["name"],
            nav=nav,
            premium_rt=premium,
            daily_limit=daily_limit,
        )
        if trade:
            new_trades.append(trade)

    return new_trades


def clear_trades() -> None:
    """清空所有交易记录"""
    _save_trades({"trades": [], "summary": {"total_invested": 0, "total_realized_pnl": 0, "total_unrealized_pnl": 0}})
    print("[交易] 已清空所有交易记录")


if __name__ == "__main__":
    # 测试
    print("=== 当前持仓 ===")
    summary = get_holdings_summary()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
