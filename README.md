# A 股数据 MCP 服务 (CnStock)

这是一个为大模型提供的 A 股数据 MCP (Model Context Protocol) 服务。

## 项目亮点

- **基于开源库**：本仓库基于 [elsejj/mcp-cn-a-stock](https://github.com/elsejj/mcp-cn-a-stock) 修改，核心改进是将原有的私有数据源替换为基于 [AkShare](https://github.com/akfamily/akshare) 和 [efinance](https://github.com/nelsonie/efinance) 的开源数据接口，不再依赖外部付费 API。
- **数据全面**：覆盖沪深京全市场股票及 **场内 ETF 基金**。
- **本地运行**：通过本地环境直接获取实时行情和财报数据，保障隐私与速度。

## 功能特性

支持 A 股和场内 ETF 行情查询，目前为大模型提供以下维度的数据：

- **基本信息**：股票代码、名称、所属行业概念、总市值、流通市值等。
- **行情数据**：实时价格、成交量、换手率，以及历史 K 线统计。
- **财务数据**：近年的主要财务指标（净利润、营收、ROE、EPS、NAV 等）。
- **技术指标**：实时计算 KDJ、MACD、RSI、布林带等常用指标。

这些数据通过以下三个 MCP 工具提供：

- `brief`: 提供股票基本信息及当日行情。
- `medium`: 在 `brief` 基础上增加主要财务数据。
- `full`: 提供最完整的数据，包括详细财务报表及实时技术指标。

## 工具返回示例

- [查询结果展示: 兆易创新 (SH603986) 全量分析报告](docs/SH603986-full.md)

## 开发与运行

### 1. 环境准备

建议使用 Python 3.12+ 编译环境。

```bash
# 克隆仓库
git clone <your-repo-url>
cd mcp-cn-a-stock

# 创建虚拟环境并安装依赖 (推荐使用 uv)
uv sync
# 或者使用 pip
pip install .
```

### 2. 配置说明

在项目根目录下创建 `.env` 文件，配置 AkShare 代理补丁（用于提高东财接口稳定性）：

```env
AKSHARE_PROXY_IP=你的代理IP
AKSHARE_PROXY_PASSWORD=你的代理密码
AKSHARE_PROXY_PORT=50
```

### 3. 启动服务

```bash
# 使用 uv 运行
uv run qtf-mcp --transport http

# 或者直接运行 python
python main.py --transport http
```

服务默认运行在 `http://localhost:8686/cnstock/mcp`。

### 4. 调试与测试

使用 MCP Inspector 进行交互式调试：

```bash
npx @modelcontextprotocol/inspector --url http://localhost:8686/cnstock/mcp
```

## 客户端接入

你可以将此服务接入常用的 MCP 客户端（如 Claude Desktop, CherryStudio, DeepChat 等）。

### CherryStudio 配置示例

1. 进入 **设置 -> MCP 设置 -> 添加服务器**。
2. 类型选择 `可流式传输的HTTP(streamableHttp)`，地址填入 `http://localhost:8686/cnstock/mcp`。

![cherrystudio](docs/cherrystudio.jpg)

## 免责申明

本项目利用开源社区接口尽可能的保障数据准确可用, 但不对因数据延迟或错误产生的任何交易决策负任何责任。股市有风险，入市需谨慎。

## 许可证

基于原项目协议，本项目采用 MIT 许可证。

## 联系方式

如有问题或建议，欢迎提交 Issue。
