# Changelog

## 2026-06-24

### 新增：LOF溢价详情快速跳转

- 表格中每只LOF的代码列改为可点击链接，点击直接跳转东方财富基金详情页查看溢价情况
- 操作列的链接文案从"Link"改为"公告"，语义更清晰

**改动文件：**
- `static/index.html` — 代码列加 `<a>` 链接到 `https://fundf10.eastmoney.com/{code}.html`
