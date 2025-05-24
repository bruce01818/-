import json
import time
import os
import concurrent.futures
from web3 import Web3
from web3.exceptions import ContractLogicError
from dotenv import load_dotenv

# --------------------------
# 初始化配置
# --------------------------
load_dotenv()

# 高可用RPC節點列表
BSC_RPC_URLS = [
    "https://bsc-dataseed1.binance.org",
    "https://bsc-dataseed2.binance.org",
    "https://bsc-dataseed3.binance.org"
]

# --------------------------
# 完整合約ABI配置
# --------------------------
PANCAKE_ROUTER_ABI = json.loads("""
[
  {
    "inputs": [],
    "name":"WETH",
    "outputs":[{"internalType":"address","name":"","type":"address"}],
    "stateMutability":"view",
    "type":"function"
  },
  {
    "name":"swapExactTokensForETHSupportingFeeOnTransferTokens",
    "type":"function",
    "stateMutability":"nonpayable",
    "inputs":[
      {"name":"amountIn","type":"uint256"},
      {"name":"amountOutMin","type":"uint256"},
      {"name":"path","type":"address[]"},
      {"name":"to","type":"address"},
      {"name":"deadline","type":"uint256"}
    ],
    "outputs":[]
  },
  {
    "name":"swapExactTokensForTokensSupportingFeeOnTransferTokens",
    "type":"function",
    "stateMutability":"nonpayable",
    "inputs":[
      {"name":"amountIn","type":"uint256"},
      {"name":"amountOutMin","type":"uint256"},
      {"name":"path","type":"address[]"},
      {"name":"to","type":"address"},
      {"name":"deadline","type":"uint256"}
    ],
    "outputs":[]
  },
  {
    "inputs":[
      {"internalType":"uint256","name":"amountIn","type":"uint256"},
      {"internalType":"address[]","name":"path","type":"address[]"}
    ],
    "name":"getAmountsOut",
    "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
    "stateMutability":"view",
    "type":"function"
  }
]
""")

USDT_ABI = json.loads("""
[
  {
    "inputs":[
      {"name":"_spender","type":"address"},
      {"name":"_value","type":"uint256"}
    ],
    "name":"approve",
    "outputs":[{"name":"","type":"bool"}],
    "stateMutability":"nonpayable",
    "type":"function"
  },
  {
    "inputs":[
      {"name":"_owner","type":"address"},
      {"name":"_spender","type":"address"}
    ],
    "name":"allowance",
    "outputs":[{"name":"","type":"uint256"}],
    "stateMutability":"view",
    "type":"function"
  },
  {
    "constant":true,
    "inputs":[{"name":"_owner","type":"address"}],
    "name":"balanceOf",
    "outputs":[{"name":"balance","type":"uint256"}],
    "payable":false,
    "type":"function",
    "stateMutability":"view"
  }
]
""")

# --------------------------
# 增強版Web3連接管理
# --------------------------
class EnhancedWeb3:
    def __init__(self, rpc_urls):
        self.rpc_urls = rpc_urls
        self.current_provider = 0
        self.w3 = self._connect()
        self._verify_connection()

    def _connect(self):
        return Web3(Web3.HTTPProvider(
            self.rpc_urls[self.current_provider],
            request_kwargs={'timeout': 10}
        ))

    def _verify_connection(self):
        if not self.w3.is_connected():
            raise ConnectionError("無法連接任何BSC節點")

    def switch_provider(self):
        """切換到備用RPC節點"""
        self.current_provider = (self.current_provider + 1) % len(self.rpc_urls)
        print(f"🔄 切換到節點: {self.rpc_urls[self.current_provider]}")
        self.w3 = self._connect()
        self._verify_connection()

    def __getattr__(self, name):
        return getattr(self.w3, name)

# --------------------------
# 合約地址與代幣配置（已修正）
# --------------------------
CONTRACT_ADDRESSES = {
    # 交易所路由合約
    "pancake": Web3.to_checksum_address("0x10ED43C718714eb63d5aA57B78B54704E256024E"),
    "biswap": Web3.to_checksum_address("0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8"),
    "mdex": Web3.to_checksum_address("0x7DAe51BD3E3376B8c7c4900E9107f12Be3AF1bA8"),
    
    # 代幣合約（已修正WBNB地址）
    "usdt": Web3.to_checksum_address("0x55d398326f99059fF775485246999027B3197955"),
    "wbnb": Web3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),  # 正確地址
    "busd": Web3.to_checksum_address("0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56")
}

TOKEN_DECIMALS = {
    CONTRACT_ADDRESSES["usdt"]: 18,
    CONTRACT_ADDRESSES["wbnb"]: 18,
    CONTRACT_ADDRESSES["busd"]: 18
}

# --------------------------
# USDT計價監控核心模組（含盈虧分析）
# --------------------------
# --------------------------
# USDT计价监控核心模块（最终修正版）
# --------------------------
class USDTPriceMonitor:
    def __init__(self, w3):
        self.w3 = w3
        self.exchanges = self._init_exchanges()  # 需要补充这个方法
        self.base_token = CONTRACT_ADDRESSES["usdt"]
        
        # 明确定义交易路径（最终修正）
        self.trading_pairs = {
            "WBNB": {
                "sell_path": [CONTRACT_ADDRESSES["wbnb"], self.base_token],  # WBNB→USDT
                "buy_path": [self.base_token, CONTRACT_ADDRESSES["wbnb"]]    # USDT→WBNB
            },
            "BUSD": {
                "sell_path": [CONTRACT_ADDRESSES["busd"], self.base_token],  # BUSD→USDT
                "buy_path": [self.base_token, CONTRACT_ADDRESSES["busd"]]    # USDT→BUSD
            }
        }

    def _init_exchanges(self):
        """初始化交易所合约实例（新增方法）"""
        return {
            "pancake": self.w3.eth.contract(
                address=CONTRACT_ADDRESSES["pancake"],
                abi=PANCAKE_ROUTER_ABI
            ),
            "biswap": self.w3.eth.contract(
                address=CONTRACT_ADDRESSES["biswap"],
                abi=PANCAKE_ROUTER_ABI
            ),
            "mdex": self.w3.eth.contract(
                address=CONTRACT_ADDRESSES["mdex"],
                abi=PANCAKE_ROUTER_ABI
            )
        }

    def get_all_prices(self):
        """获取全交易所价格数据（新增方法）"""
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {}
            for exchange in self.exchanges:
                for pair_name in self.trading_pairs:
                    key = f"{exchange}_{pair_name}"
                    futures[executor.submit(
                        self._get_pair_price,
                        exchange, pair_name
                    )] = key

            for future in concurrent.futures.as_completed(futures):
                try:
                    data = future.result()
                    exchange = data["exchange"]
                    pair = data["pair"]
                    if exchange not in results:
                        results[exchange] = {}
                    results[exchange][pair] = data
                except Exception as e:
                    print(f"❌ 查询失败: {str(e)}")
        return results

    def _get_error_data(self, exchange, pair):
        """错误数据处理（新增方法）"""
        return {
            "exchange": exchange,
            "pair": pair,
            "buy_price": 0,
            "sell_price": 0,
            "spread": 0,
            "net_profit": 0,
            "status": "⚪ 无数据"
        }

    def _get_pair_price(self, exchange_name, pair_name):
        """最终修正版价格计算"""
        contract = self.exchanges[exchange_name]
        paths = self.trading_pairs[pair_name]
        
        try:
            # 买入价：通过反向路径计算（USDT→代币→取倒数）
            buy_result = self._calculate_direct_price(contract, paths["buy_path"])
            buy_price = 1 / buy_result if buy_result > 0 else 0
            buy_price = round(buy_price, 6)
            
            # 卖出价：直接使用卖出路径（代币→USDT）
            sell_price = self._calculate_direct_price(contract, paths["sell_path"])
            sell_price = round(sell_price, 6)
            
            # 计算实际盈亏（含手续费）
            fee = 0.003  # 0.3%手续费
            net_profit = sell_price*(1 - fee) - buy_price*(1 + fee)
            
            # 判断交易状态
            if net_profit > 0.1:
                status = "🟢 盈利"
            elif net_profit < -0.1:
                status = "🔴 亏损"
            else:
                status = "🟡 持平"
            
            return {
                "exchange": exchange_name,
                "pair": pair_name,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "spread": sell_price - buy_price,
                "net_profit": net_profit,
                "status": status
            }
        except Exception as e:
            print(f"价格查询异常: {str(e)}")
            return self._get_error_data(exchange_name, pair_name)

    def _calculate_direct_price(self, contract, path):
        """直接路径价格计算"""
        try:
            if path[0] not in TOKEN_DECIMALS or path[-1] not in TOKEN_DECIMALS:
                raise ValueError("无效路径")
            
            input_decimals = TOKEN_DECIMALS[path[0]]
            output_decimals = TOKEN_DECIMALS[path[-1]]
            
            amounts = contract.functions.getAmountsOut(
                1 * 10 ** input_decimals,
                path
            ).call()
            
            return amounts[-1] / 10 ** output_decimals
        except Exception as e:
            print(f"计算异常: {str(e)}")
            return 0

# --------------------------
# 终端显示模块（优化版）
# --------------------------
# --------------------------
# 终端显示模块（优化版）
# --------------------------
class AdvancedDisplay:
    @staticmethod
    def clear_screen():
        """清空终端屏幕"""
        os.system('cls' if os.name == 'nt' else 'clear')

    @staticmethod
    def show(data):
        AdvancedDisplay.clear_screen()
        print("\n🔥 BSC交易所套利监控系统")
        print("📌 买价=购买1个代币所需USDT | 卖价=卖出1个代币获得USDT | 手续费=0.3%")
        
        for exchange, pairs in data.items():
            print(f"\n🔷 {exchange.upper()}交易所")
            print(f"{'代币':<4} | {'买价':<6} | {'卖价':<6} | {'价差':<8} | {'净收益':<7} | {'状态':<6}")
            print("-" * 70)
            for pair, values in pairs.items():
                print(f"{pair:<6} | "
                      f"{values['buy_price']:<8.4f} | "
                      f"{values['sell_price']:<8.4f} | "
                      f"{values['spread']:>+10.4f} | "
                      f"{values['net_profit']:>+10.4f} | "
                      f"{values['status']}")
        print("\n🔄 数据每3秒刷新 | CTRL+C 退出")


# --------------------------
# 主程序
# --------------------------
def main():
    # 初始化區塊鏈連接
    web3 = EnhancedWeb3(BSC_RPC_URLS)
    print(f"✅ 連接成功 | 最新區塊高度: {web3.eth.block_number}")
    
    # 啟動價格監控系統
    monitor = USDTPriceMonitor(web3)
    display = AdvancedDisplay()
    
    try:
        while True:
            start_time = time.time()
            
            # 獲取並顯示價格數據
            price_data = monitor.get_all_prices()
            display.show(price_data)
            
            # 精確控制刷新頻率
            elapsed = time.time() - start_time
            sleep_time = max(5.0 - elapsed, 0)
            time.sleep(sleep_time)
            
    except KeyboardInterrupt:
        print("\n🛑 監控系統已安全停止")

if __name__ == "__main__":
    main()