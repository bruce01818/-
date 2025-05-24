import web3
import json
import time
import os
import threading
import concurrent.futures
from web3 import Web3
from web3.exceptions import ContractLogicError
from dotenv import load_dotenv
from eth_abi import encode
from collections import deque

# --------------------------
# 初始化配置
# --------------------------
load_dotenv()

# 多載點負載均衡
BSC_RPC_URLS = [
    os.getenv("BSC_RPC_URL1", "https://bsc-dataseed.binance.org/"),
    os.getenv("BSC_RPC_URL2", "https://bsc-dataseed1.defibit.io/"),
    os.getenv("BSC_RPC_URL3", "https://bsc-dataseed2.defibit.io/")
]
w3 = Web3(Web3.HTTPProvider(BSC_RPC_URLS[0]))
assert w3.is_connected(), "❌ BSC節點連接失敗"

# 合約地址（强制校驗格式）
CONTRACT_ADDRESSES = {
    "pancake_router": Web3.to_checksum_address("0x10ED43C718714eb63d5aA57B78B54704E256024E"),
    "Biswap_router": Web3.to_checksum_address("0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8"),
    "babyswap_router": Web3.to_checksum_address("0x8317c460C22A9958c27b4B6403b98d2Ef4E2ad32"),
    "Mdex_router": Web3.to_checksum_address("0x7DAe51BD3E3376B8c7c4900E9107f12Be3AF1bA8"),
    "Openocean_router": Web3.to_checksum_address("0x8ea5219a16c2dbF1d6335A6aa0c6bd45c50347C5"),
    "usdt": Web3.to_checksum_address("0x55d398326f99059fF775485246999027B3197955"),
    "wbnb": Web3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
    "busd": Web3.to_checksum_address("0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56")
}

# -----------------------price_monitor---
# ABI 配置
# --------------------------
PANCAKE_ROUTER_ABI = json.loads("""[
  {"inputs":[],"name":"WETH","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"}],
   "name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},
   {"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},
   {"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForTokensSupportingFeeOnTransferTokens",
   "outputs":[],"stateMutability":"nonpayable","type":"function"}
]""")#祐(標註為個人不可更改)

ERC20_ABI = json.loads("""[
  {"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],
   "name":"approve","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},
  {"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],
   "payable":false,"stateMutability":"view","type":"function"},
  {"constant":true,"inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],
   "name":"allowance","outputs":[{"name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},
  {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"payable":false,"stateMutability":"view","type":"function"}
]""")#祐(標註為個人不可更改)

# --------------------------
# 智能合約實比例
# --------------------------
pancake_router = w3.eth.contract(address=CONTRACT_ADDRESSES["pancake_router"], abi=PANCAKE_ROUTER_ABI)
Biswap_router = w3.eth.contract(address=CONTRACT_ADDRESSES["Biswap_router"], abi=PANCAKE_ROUTER_ABI)
babyswap_router = w3.eth.contract(address=CONTRACT_ADDRESSES["babyswap_router"], abi=PANCAKE_ROUTER_ABI)
Mdex_router = w3.eth.contract(address=CONTRACT_ADDRESSES["Mdex_router"], abi=PANCAKE_ROUTER_ABI)
Openocean_router = w3.eth.contract(address=CONTRACT_ADDRESSES["Openocean_router"], abi=PANCAKE_ROUTER_ABI)
usdt_contract = w3.eth.contract(address=CONTRACT_ADDRESSES["usdt"], abi=ERC20_ABI)
wbnb_contract = w3.eth.contract(address=CONTRACT_ADDRESSES["wbnb"], abi=ERC20_ABI)
busd_contract = w3.eth.contract(address=CONTRACT_ADDRESSES["busd"], abi=ERC20_ABI)

# --------------------------
#策略參數
# --------------------------
class Config:
    CHECK_INTERVAL = 0.5          # 價格檢查時間（秒）
    MAX_TX_DURATION = 1.0         # 最大交易耗時（秒）
    SLIPPAGE_TOLERANCE = 1.5      # 滑點滑鐵盧（百分比）
    MIN_PROFIT_USDT = 0.3         # 最小套利利潤（USDT）
    TRADE_AMOUNT_USDT = 50        # 單次交易金額（USDT）
    GAS_LIMIT_BUFFER = 1.2        # Gas Limit 緩衝系數
    MAX_GAS_GWEI = 25             # 最大接受Gas價格（Gwei）
    BALANCE_BUFFER_BNB = 0.1      # 最低保留BNB餘額（BNB）
    RETRY_ATTEMPTS = 3            # 交易重試次數
    APPROVE_INFINITE = 2**256 -1  # 買賣授權額度無限大

# --------------------------
# 高頻價格監控模組（二版）by祐
# --------------------------
class EnhancedPriceMonitor:
    def __init__(self):
        self.price_cache = {}
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        self.last_update = 0
        self.dex_list = [
            ("pancake", pancake_router),
            ("Openocean", Openocean_router),
            ("Biswap", Biswap_router),
            ("Mdex", Mdex_router),
            ("babyswap", babyswap_router)
        ]
#蘇
    def get_real_time_prices(self):
        """多線成獲取各DEX最優價格"""
        if time.time() - self.last_update < Config.CHECK_INTERVAL:
            return self.price_cache

        futures = {}
        for dex_name, router in self.dex_list:
            futures[self.executor.submit(self._fetch_dex_price, router)] = dex_name

        updated_prices = {}
        for future in concurrent.futures.as_completed(futures):
            dex_name = futures[future]
            try:
                price_data = future.result()
                updated_prices[dex_name] = {
                    'buy_price': price_data[0],
                    'sell_price': price_data[1]
                }
            except Exception as e:
                print(f"[{dex_name}] 價格獲取異常: {str(e)}")

        if updated_prices:
            self.price_cache = updated_prices
            self.last_update = time.time()
        return self.price_cache

    def _fetch_dex_price(self, router):
        """獲取雙向最優價格（買/賣）"""
        try:
            # 買入：USDT -> WBNB
            buy_paths = [
                [CONTRACT_ADDRESSES["usdt"], CONTRACT_ADDRESSES["wbnb"]],
                [CONTRACT_ADDRESSES["usdt"], CONTRACT_ADDRESSES["busd"], CONTRACT_ADDRESSES["wbnb"]]]
            buy_prices = []
            for path in buy_paths:
                try:
                    amounts = router.functions.getAmountsOut(10**18, path).call(timeout=2)
                    buy_prices.append(amounts[-1] / 1e18)
                except:
                    continue
            
            # 賣出：WBNB -> USDT
            sell_paths = [
                [CONTRACT_ADDRESSES["wbnb"], CONTRACT_ADDRESSES["usdt"]],
                [CONTRACT_ADDRESSES["wbnb"], CONTRACT_ADDRESSES["busd"], CONTRACT_ADDRESSES["usdt"]]]
            sell_prices = []
            for path in sell_paths:
                try:
                    amounts = router.functions.getAmountsOut(10**18, path).call(timeout=2)
                    sell_prices.append(amounts[-1] / 1e18)
                except:
                    continue

            return (max(buy_prices) if buy_prices else 0, 
                    max(sell_prices) if sell_prices else 0)
        except Exception as e:
            raise RuntimeError(f"DEX價格獲取失敗: {str(e)}")

# --------------------------
# 套利引擎核心（完整版）
# --------------------------
class CompleteArbitrageEngine:
    def __init__(self):
        self.wallet_address = Web3.to_checksum_address(os.getenv("WALLET_ADDRESS"))
        self.private_key = os.getenv("PRIVATE_KEY")
        self.price_monitor = EnhancedPriceMonitor()
        self.nonce = w3.eth.get_transaction_count(self.wallet_address)
        self.nonce_lock = threading.Lock()
        self.gas_strategy = self.dynamic_gas_price
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        self.pending_transactions = {}
        
        # 初始化代幣精度
        self.usdt_decimals = usdt_contract.functions.decimals().call()
        self.wbnb_decimals = wbnb_contract.functions.decimals().call()

   # --------------------------
# 套利引擎核心（完整版）
# --------------------------
class CompleteArbitrageEngine:


    def check_and_execute_arbitrage(self):
        """完整的套利檢測與執行流程"""
        prices = self.price_monitor.get_real_time_prices()
        if not prices or len(prices) < 2:
            return False

        # +++ 新增价格显示功能 +++
        print("\n=== 实时价格监控 ===")
        for dex, data in prices.items():
            buy_price = data['buy_price'] if data['buy_price'] else 0.0
            sell_price = data['sell_price'] if data['sell_price'] else 0.0
            print(f"[{dex.upper():<10}] 买价: {buy_price:.6f} | 卖价: {sell_price:.6f} | 价差: {(sell_price - buy_price):.4f}")
        print("====================\n")

        # 尋找最佳套利組合
        best_opp = None
        for buy_dex, buy_data in prices.items():
            for sell_dex, sell_data in prices.items():
                if buy_dex == sell_dex:
                    continue
                
                spread = sell_data['sell_price'] - buy_data['buy_price']
                if spread < Config.MIN_PROFIT_USDT:
                    continue
                
                if not best_opp or spread > best_opp['spread']:
                    best_opp = {
                        'buy_dex': buy_dex,
                        'sell_dex': sell_dex,
                        'spread': spread,
                        'buy_price': buy_data['buy_price'],
                        'sell_price': sell_data['sell_price']
                    }

        if not best_opp:
            return False

        # 計算實際利潤
        net_profit = self.calculate_net_profit(
            best_opp['spread'], 
            Config.TRADE_AMOUNT_USDT
        )
        if net_profit < Config.MIN_PROFIT_USDT:
            return False

        # 執行套利交易
        
    def _calculate_gas_cost(self, start_time):
        """計算總Gas成本"""
        current_bnb_price = self._get_bnb_price()
        gas_used = 0
        for tx_hash in self.pending_transactions.values():
            try:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                gas_used += receipt.gasUsed * receipt.effectiveGasPrice
            except:
                continue
        return (gas_used / 1e18) * current_bnb_price

    def _get_router_address(self, dex_name):
        return CONTRACT_ADDRESSES[f"{dex_name}_router"]

    def dynamic_gas_price(self):
        current_gas = w3.eth.gas_price
        return min(int(current_gas * 1.15), Web3.to_wei(Config.MAX_GAS_GWEI, "gwei"))

    def _get_optimal_path(self, router, in_token, out_token, amount_in):
        token_map = {
            "usdt": CONTRACT_ADDRESSES["usdt"],
            "wbnb": CONTRACT_ADDRESSES["wbnb"],
            "busd": CONTRACT_ADDRESSES["busd"]
        }
        
        possible_paths = [
            [token_map[in_token], token_map[out_token]],
            [token_map[in_token], token_map["busd"], token_map[out_token]]
        ]
        
        best_path = None
        max_out = 0
        for path in possible_paths:
            try:
                amounts = router.functions.getAmountsOut(amount_in, path).call()
                if amounts[-1] > max_out:
                    max_out = amounts[-1]
                    best_path = path
            except:
                continue
        
        if not best_path:
            raise ValueError("無有效交易路徑")
        
        min_out = int(max_out * (100 - Config.SLIPPAGE_TOLERANCE) / 100)
        return best_path, min_out

    def _check_balances(self, amount_usdt):
        usdt_balance = usdt_contract.functions.balanceOf(self.wallet_address).call()
        if usdt_balance < amount_usdt * 10**self.usdt_decimals:
            print(f"❌ USDT餘額不足 需要: {amount_usdt} 當前的: {usdt_balance/10**self.usdt_decimals:.2f}")
            return False
        
        bnb_balance = w3.eth.get_balance(self.wallet_address)
        if bnb_balance < Web3.to_wei(Config.BALANCE_BUFFER_BNB, "ether"):
            print(f"❌ BNB餘額不足 需要至少 {Config.BALANCE_BUFFER_BNB} BNB")
            return False
        return True

    def _estimate_gas(self, router, tx):
        try:
            return int(router.estimate_gas(tx) * Config.GAS_LIMIT_BUFFER)
        except:
            return 300000

    def _get_nonce(self):
        with self.nonce_lock:
            current = self.nonce
            self.nonce += 1
        return current

    def _get_bnb_price(self):
        try:
            amounts = pancake_router.functions.getAmountsOut(
                10**18,
                [CONTRACT_ADDRESSES["wbnb"], CONTRACT_ADDRESSES["usdt"]]
            ).call()
            return amounts[-1] / 1e18
        except:
            return 300

# --------------------------
# 執行監控（模組加強版，參考監視功能by謝金輝）
# --------------------------
def main():
    engine = CompleteArbitrageEngine()
    print("🚀高頻價格監控模組啟動")
    
    while True:
        cycle_start = time.time()
        
        try:
            if engine.check_and_execute_arbitrage():
                print("🎉 完成套利循環")
            else:
                print("🔍 未發現有效機會")
        except Exception as e:
            print(f"⚠️ 系統異常: {str(e)}")
        
        # 精確間隔控制
        elapsed = time.time() - cycle_start
        sleep_time = max(Config.CHECK_INTERVAL - elapsed, 0)
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()