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

# 高可用 RPC 節點列表
BSC_RPC_URLS = [
    "https://bsc-dataseed1.binance.org",
    "https://bsc-dataseed2.binance.org",
    "https://bsc-dataseed3.binance.org"
]

# --------------------------
# 完整合約 ABI 配置
# --------------------------
PANCAKE_ROUTER_ABI = json.loads("""
[
  {"inputs": [],"name":"WETH","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
  {"name":"swapExactTokensForETHSupportingFeeOnTransferTokens","type":"function","stateMutability":"nonpayable","inputs":[{"name":"amountIn","type":"uint256"},{"name":"amountOutMin","type":"uint256"},{"name":"path","type":"address[]"},{"name":"to","type":"address"},{"name":"deadline","type":"uint256"}],"outputs":[]},
  {"name":"swapExactTokensForTokensSupportingFeeOnTransferTokens","type":"function","stateMutability":"nonpayable","inputs":[{"name":"amountIn","type":"uint256"},{"name":"amountOutMin","type":"uint256"},{"name":"path","type":"address[]"},{"name":"to","type":"address"},{"name":"deadline","type":"uint256"}],"outputs":[]},
  {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"}],"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"}
]
""")

USDT_ABI = json.loads("""
[
  {"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
  {"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"payable":false,"type":"function","stateMutability":"view"}
]
""")

# --------------------------
# 增強版 Web3 連接管理
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
            raise ConnectionError("無法連接任何 BSC 節點")

    def switch_provider(self):
        self.current_provider = (self.current_provider + 1) % len(self.rpc_urls)
        print(f"切換到節點: {self.rpc_urls[self.current_provider]}")
        self.w3 = self._connect()
        self._verify_connection()

    def __getattr__(self, name):
        return getattr(self.w3, name)

# --------------------------
# 合約地址與代幣配置
# --------------------------
CONTRACT_ADDRESSES = {
    "pancake": Web3.to_checksum_address("0x10ED43C718714eb63d5aA57B78B54704E256024E"),
    "biswap": Web3.to_checksum_address("0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8"),
    "mdex":    Web3.to_checksum_address("0x7DAe51BD3E3376B8c7c4900E9107f12Be3AF1bA8"),
    "usdt":    Web3.to_checksum_address("0x55d398326f99059fF775485246999027B3197955"),
    "wbnb":    Web3.to_checksum_address("0xfb5b838b6cfeedc2873ab27866079ac55363d37e"),
    "busd":    Web3.to_checksum_address("0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56")
}
TOKEN_DECIMALS = {addr: 18 for addr in CONTRACT_ADDRESSES.values()}

# --------------------------
# USDT 計價監控核心
# --------------------------
class USDTPriceMonitor:
    def __init__(self, w3):
        self.w3 = w3
        self.exchanges = self._init_exchanges()
        self.paths = {
            'WBNB': [CONTRACT_ADDRESSES['usdt'], CONTRACT_ADDRESSES['wbnb']],
            'BUSD': [CONTRACT_ADDRESSES['usdt'], CONTRACT_ADDRESSES['busd']]
        }

    def _init_exchanges(self):
        return {
            name: self.w3.eth.contract(address=addr, abi=PANCAKE_ROUTER_ABI)
            for name, addr in CONTRACT_ADDRESSES.items() if name in ['pancake', 'biswap', 'mdex']
        }

    def get_all(self):
        results = {}
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = {}
            # 為每個交易所與交易對提交查詢任務
            for name, contract in self.exchanges.items():
                for pair_name, path in self.paths.items():
                    fut = executor.submit(self._get_pair_price, contract, path)
                    futures[fut] = (name, pair_name)

            # 處理完成的任務
            for fut in concurrent.futures.as_completed(futures):
                name, pair_name = futures[fut]
                data = fut.result()
                if data['buy'] > 0 or data['sell'] > 0:
                    results.setdefault(name, {})[pair_name] = data

        return results

    def _get_pair_price(self, contract, path):
        try:
            input_amount = 10 ** TOKEN_DECIMALS[path[0]]
            amounts = contract.functions.getAmountsOut(input_amount, path).call()
            if len(amounts) < 2:
                return {'buy': 0, 'sell': 0, 'spread': 0}

            # 計算買入與賣出價格
            buy_price = amounts[-1] / 10 ** TOKEN_DECIMALS[path[-1]]
            sell_price = 1 / buy_price if buy_price != 0 else 0
            return {
                'buy': buy_price,
                'sell': sell_price,
                'spread': sell_price - buy_price
            }
        except ContractLogicError:
            return {'buy': 0, 'sell': 0, 'spread': 0}
        except Exception:
            # 發生其他錯誤時切換 RPC 節點
            self.w3.switch_provider()
            return {'buy': 0, 'sell': 0, 'spread': 0}

# --------------------------
# 終端顯示模組
# --------------------------
class AdvancedDisplay:
    @staticmethod
    def clear():
        os.system('cls' if os.name == 'nt' else 'clear')

    @staticmethod
    def show(data):
        AdvancedDisplay.clear()
        print("\n🔥 BSC 交易所 USDT 交易對即時監控")
        for exchange, pairs in data.items():
            print(f"\n🔷 {exchange.upper()} 交易所")
            print(f"{'交易對':<10}{'買入(USDT)':<15}{'賣出(USDT)':<15}{'價差':<10}")
            print('-' * 50)
            for pair_name, values in pairs.items():
                print(f"{pair_name:<10}{values['buy']:<15.6f}{values['sell']:<15.6f}{values['spread']:+.6f}")
        print("\n🔄 資料每 3 秒更新｜CTRL+C 退出")

# --------------------------
# 主程式入口
# --------------------------
def main():
    w3 = EnhancedWeb3(BSC_RPC_URLS)
    monitor = USDTPriceMonitor(w3)
    display = AdvancedDisplay()

    try:
        while True:
            display.show(monitor.get_all())
            time.sleep(3)
    except KeyboardInterrupt:
        print("\n🛑 監控已停止")

if __name__ == '__main__':
    main()
