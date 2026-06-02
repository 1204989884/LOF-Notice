# LOF套利监测系统

每天自动扫描全市场LOF基金，通过"三步核实法"筛选出真正可以申购套利的品种。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动服务
python main.py

# 3. 打开浏览器访问 http://localhost:8877
```

## 定时任务（每日 14:40 自动运行）

### Windows 任务计划程序

1. 搜索打开 `taskschd.msc`（任务计划程序）
2. 创建基本任务 → 名称 `LOF监测`
3. 触发器：每周 → 周一至周五，时间 `14:40`
4. 操作：启动程序 → 程序填 `python.exe` 路径，参数填 `scheduler.py`，起始于本项目文件夹

### macOS / Linux cron

```bash
crontab -e
# 添加：
40 14 * * 1-5 cd /path/to/LOFnotice && python scheduler.py
```

## 项目结构

```
LOFnotice/
├── main.py              # FastAPI 后端（端口 8877）
├── fetcher.py           # 数据获取 + 三步核实
├── scheduler.py         # 定时启动脚本
├── requirements.txt     # Python 依赖
├── README.md            # 本文件
└── static/
    └── index.html       # 前端页面

## 环境要求

- Python 3.10+
- 网络环境能正常访问东方财富、天天基金（不需要翻墙，但不兼容 HTTP 代理）

## 三步核实法

系统不是简单地展示溢价率就完事，而是做了三层交叉验证：

| 步骤 | 数据源 | 做什么 |
|------|--------|--------|
| ① 初筛 | 东方财富 push2 API | 拉全量 LOF 场内价格和成交量，按成交额排序取前 60 只 |
| ② 净值核实 | 天天基金 fundgz API | 逐个获取最新净值，**重算溢价率**（不信东方财富的净值） |
| ③ 最终筛选 + 公告核实 | 天天基金页面 HTML 解析 | 溢价率 > 3% + 成交额 > 10 万，查申购状态和最新公告 |

核心原则：**绝不使用模拟数据。** 数据源全挂就报错，不会偷偷给你假数据。

## 数据来源

| 数据 | 接口 | 说明 |
|------|------|------|
| LOF 实时行情 | `push2.eastmoney.com/api/qt/clist/get` | 全量场内价格、成交量 |
| 基金净值 | `fundgz.1234567.com.cn/js/{code}.js` | 最新单位净值、净值日期 |
| 申购状态/公告 | `fund.eastmoney.com/{code}.html` | HTML 解析，无 API，无反爬 |

当前不依赖集思录（不登录只返 20 条，覆盖不全）。

## API 端点

| 路径 | 说明 |
|------|------|
| `/` | 前端页面 |
| `/api/opportunities` | LOF 套利机会列表（支持 `?min_premium=3.0` 调整阈值） |
| `/api/stats` | 统计概览 |
| `/api/config` | 当前配置 |

## 配置

在 `fetcher.py` 头部可调整：

```python
PREMIUM_THRESHOLD = 3.0      # 溢价率阈值（%）
MIN_VOLUME = 100000          # 最低成交额（元）
NAV_VERIFY_MAX = 60          # 净值核实时最多处理多少只
CACHE_TTL = 300              # 缓存时间（秒）
```

## 注意事项

1. **不要频繁刷新。** 东方财富有频率限制，缓存默认 5 分钟，够用了。
2. **不兼容 HTTP 代理。** 代码里设置了 `trust_env=False`，如果你的网络必须走代理才能出站，需要改 `fetcher.py` 去掉这个参数。
3. **盘后数据会偏少。** 收盘后大部分 LOF 场内价格回归净值，溢价筛选结果会显著减少。
4. **这不是自动交易工具。** 系统只负责筛选，实际操作前请参考 `VERIFY_GUIDE.md` 逐项手动核实。

## 免责声明

本项目仅供研究学习，不构成任何投资建议。LOF 套利存在 T+2 时间风险、流动性风险、净值突变风险和申购失败风险。**每个人都是自己财富的第一责任人。**
