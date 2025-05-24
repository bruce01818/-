import json
import time
import os
import concurrent.futures
from web3 import Web3
from web3.exceptions import TransactionNotFound, ContractLogicError
from dotenv import load_dotenv
import threading
from collections import deque
from typing import Optional, Dict
import eth_utils
from eth_abi import encode

# --------------------------
# 1. 載入環境變數與常數配置
# --------------------------
load_dotenv()

# 必要環境變數
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY    = os.getenv("PRIVATE_KEY")
if not WALLET_ADDRESS or not PRIVATE_KEY:
    raise Exception("❌ 請在環境變數中設定 WALLET_ADDRESS 與 PRIVATE_KEY")

# 常數設定
CACHE_TIMEOUT = 10            # 價格緩存超時秒數
MAX_SLIPPAGE = 1.0            # 最大滑點百分比
MIN_PROFIT_THRESHOLD = 0.5    # 最小套利利潤閥值 (USDT)
MAX_GAS_PRICE_GWEI = 50       # 最大Gas價格（單位：gwei）
BALANCE_BUFFER = 30           # 交易前最低需要保留BNB數量（以ether計）

# --------------------------
# 2. 高可用 BSC RPC 節點
# --------------------------
BSC_RPC_URLS = [
    "https://bsc-dataseed1.binance.org",
    "https://bsc-dataseed2.binance.org",
    "https://bsc-dataseed3.binance.org"
]

# --------------------------
# 3. 完整合約 ABI 配置
# --------------------------
PANCAKE_ROUTER_ABI = json.loads("""
[
  {
    "inputs": [],
    "name": "WETH",
    "outputs": [{"internalType": "address", "name": "", "type": "address"}],
    "stateMutability": "view",
    "type": "function"
  },
  {
    "name": "swapExactTokensForETHSupportingFeeOnTransferTokens",
    "type": "function",
    "stateMutability": "nonpayable",
    "inputs": [
      {"name": "amountIn", "type": "uint256"},
      {"name": "amountOutMin", "type": "uint256"},
      {"name": "path", "type": "address[]"},
      {"name": "to", "type": "address"},
      {"name": "deadline", "type": "uint256"}
    ],
    "outputs": []
  },
  {
    "name": "swapExactTokensForTokensSupportingFeeOnTransferTokens",
    "type": "function",
    "stateMutability": "nonpayable",
    "inputs": [
      {"name": "amountIn", "type": "uint256"},
      {"name": "amountOutMin", "type": "uint256"},
      {"name": "path", "type": "address[]"},
      {"name": "to", "type": "address"},
      {"name": "deadline", "type": "uint256"}
    ],
    "outputs": []
  },
  {
    "inputs": [
      {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
      {"internalType": "address[]", "name": "path", "type": "address[]"}
    ],
    "name": "getAmountsOut",
    "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
    "stateMutability": "view",
    "type": "function"
  }
]
""")

USDT_ABI = json.loads("""
[
  {
    "inputs": [
      {"name": "_spender", "type": "address"},
      {"name": "_value", "type": "uint256"}
    ],
    "name": "approve",
    "outputs": [{"name": "", "type": "bool"}],
    "stateMutability": "nonpayable",
    "type": "function"
  },
  {
    "inputs": [
      {"name": "_owner", "type": "address"},
      {"name": "_spender", "type": "address"}
    ],
    "name": "allowance",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
  },
  {
    "constant": true,
    "inputs": [{"name": "_owner", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "balance", "type": "uint256"}],
    "payable": false,
    "type": "function",
    "stateMutability": "view"
  }
]
""")

# --------------------------
# 4. 增強版 Web3 連線管理
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
            raise ConnectionError("❌ 無法連接任何 BSC 節點")

    def switch_provider(self):
        self.current_provider = (self.current_provider + 1) % len(self.rpc_urls)
        print(f"🔄 切換到節點: {self.rpc_urls[self.current_provider]}")
        self.w3 = self._connect()
        self._verify_connection()

    def __getattr__(self, name):
        return getattr(self.w3, name)

# --------------------------
# 5. 合約地址與代幣配置
# --------------------------
CONTRACT_ADDRESSES = {
    # 交易所路由合約
    "pancake": Web3.to_checksum_address("0x10ED43C718714eb63d5aA57B78B54704E256024E"),
    "biswap": Web3.to_checksum_address("0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8"),
    "mdex": Web3.to_checksum_address("0x7DAe51BD3E3376B8c7c4900E9107f12Be3AF1bA8"),
    
    # 代幣合約 (修正後)
    "usdt": Web3.to_checksum_address("0x55d398326f99059fF775485246999027B3197955"),
    "wbnb": Web3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
    "busd": Web3.to_checksum_address("0xe9e7cea3dedca5984780bafc599bd69add087d56")
}

TOKEN_DECIMALS = {
    CONTRACT_ADDRESSES["usdt"]: 18,
    CONTRACT_ADDRESSES["wbnb"]: 18,
    CONTRACT_ADDRESSES["busd"]: 18
}

# --------------------------
# 6. 輔助函式 (傳入 web3 實例)
# --------------------------
def simulate_tx_call(web3_instance, tx_dict: dict):
    ephemeral = {
        'to': tx_dict.get('to'),
        'data': tx_dict.get('data'),
        'from': tx_dict.get('from'),
        'value': tx_dict.get('value', 0),
    }
    return web3_instance.eth.call(ephemeral, block_identifier='latest')

def get_raw_tx(signed_tx):
    if hasattr(signed_tx, 'rawTransaction'):
        return signed_tx.rawTransaction
    elif hasattr(signed_tx, 'raw_transaction'):
        return signed_tx.raw_transaction
    else:
        raise AttributeError("SignedTransaction object has no rawTransaction")

# --------------------------
# 7. 價格管理 (內部建立 Pancake 與 BakerySwap 合約)
# --------------------------
class PriceManager:
    def __init__(self, w3):
        self.w3 = w3
        # 建立路由合約實例
        self.pancake_router = self.w3.eth.contract(address=CONTRACT_ADDRESSES["pancake"], abi=PANCAKE_ROUTER_ABI)
        self.bakery_router  = self.w3.eth.contract(address=CONTRACT_ADDRESSES["biswap"],  abi=PANCAKE_ROUTER_ABI)
        self.price_cache = deque(maxlen=5)
        self.last_update = 0

    def get_prices(self) -> Optional[Dict]:
        if time.time() - self.last_update < CACHE_TIMEOUT and self.price_cache:
            return self.price_cache[-1]
        prices = {}
        for fn in [self._get_pancake_price, self._get_bakeryswap_price]:
            try:
                r = fn()
                if isinstance(r, dict):
                    prices.update(r)
            except Exception as e:
                print(f"查詢錯誤: {str(e)}")
        if prices:
            self.price_cache.append(prices)
            self.last_update = time.time()
            return prices
        elif self.price_cache:
            return self.price_cache[-1]
        else:
            return None

    def _get_pancake_price(self) -> Dict:
        try:
            amounts = self.pancake_router.functions.getAmountsOut(10**18, [CONTRACT_ADDRESSES["wbnb"], CONTRACT_ADDRESSES["usdt"]]).call()
            return {"pancake": amounts[-1] / 1e18}
        except Exception as e:
            print(f"Pancake 查詢錯誤: {e}")
            return {}

    def _get_bakeryswap_price(self) -> Dict:
        try:
            amounts = self.bakery_router.functions.getAmountsOut(10**18, [CONTRACT_ADDRESSES["wbnb"], CONTRACT_ADDRESSES["usdt"]]).call()
            return {"bakeryswap": amounts[-1] / 1e18}
        except Exception as e:
            print(f"BakerySwap 查詢錯誤: {e}")
            return {}

# --------------------------
# 8. 套利執行
# --------------------------
class ArbitrageExecutor:
    def __init__(self, w3):
        self.w3 = w3  # 使用 EnhancedWeb3 的 w3
        self.price_manager = PriceManager(self.w3)
        self.dex_map = {
            "pancake": self.w3.eth.contract(address=CONTRACT_ADDRESSES["pancake"], abi=PANCAKE_ROUTER_ABI),
            "bakeryswap": self.w3.eth.contract(address=CONTRACT_ADDRESSES["biswap"], abi=PANCAKE_ROUTER_ABI)
            # 可根據需要加入 mdex
        }
        # 建立 USDT 合約實例
        self.usdt_contract = self.w3.eth.contract(address=CONTRACT_ADDRESSES["usdt"], abi=USDT_ABI)

    def check_opportunity(self, prices: Dict) -> Optional[Dict]:
        if not prices or len(prices) < 2:
            print("⚠️ DEX 價格不足")
            return None
        dex_prices = {k: v for k, v in prices.items() if k in self.dex_map}
        if len(dex_prices) < 2:
            print("⚠️ 有效 DEX 不足")
            return None
        buy_dex = min(dex_prices, key=dex_prices.get)
        sell_dex = max(dex_prices, key=dex_prices.get)
        buy_price = dex_prices[buy_dex]
        sell_price = dex_prices[sell_dex]
        spread = sell_price - buy_price
        print(f"最低價格: {buy_price:.4f} ({buy_dex}), 最高價格: {sell_price:.4f} ({sell_dex}), 價差: {spread:.4f}")
        if spread > MIN_PROFIT_THRESHOLD:
            return {
                "buy_dex": buy_dex,
                "sell_dex": sell_dex,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "spread": spread
            }
        return None

    def _decide_path_usdt_to_wbnb(self, router, amt_in: int):
        best_path = [CONTRACT_ADDRESSES["usdt"], CONTRACT_ADDRESSES["wbnb"]]
        best_out = 0
        try:
            single = router.functions.getAmountsOut(amt_in, [CONTRACT_ADDRESSES["usdt"], CONTRACT_ADDRESSES["wbnb"]]).call()
            out_single = single[-1]
            if out_single > best_out:
                best_out = out_single
                best_path = [CONTRACT_ADDRESSES["usdt"], CONTRACT_ADDRESSES["wbnb"]]
        except Exception as e:
            print(f"單跳路徑計算失敗: {e}")
        try:
            multi = router.functions.getAmountsOut(amt_in, [CONTRACT_ADDRESSES["usdt"], CONTRACT_ADDRESSES["busd"], CONTRACT_ADDRESSES["wbnb"]]).call()
            out_multi = multi[-1]
            if out_multi > best_out:
                best_out = out_multi
                best_path = [CONTRACT_ADDRESSES["usdt"], CONTRACT_ADDRESSES["busd"], CONTRACT_ADDRESSES["wbnb"]]
        except Exception as e:
            print(f"多跳路徑計算失敗: {e}")
        return best_path, best_out

    def _decide_path_wbnb_to_usdt(self, router, amt_in: int):
        best_path = [CONTRACT_ADDRESSES["wbnb"], CONTRACT_ADDRESSES["usdt"]]
        best_out = 0
        try:
            single = router.functions.getAmountsOut(amt_in, [CONTRACT_ADDRESSES["wbnb"], CONTRACT_ADDRESSES["usdt"]]).call()
            out_single = single[-1]
            if out_single > best_out:
                best_out = out_single
                best_path = [CONTRACT_ADDRESSES["wbnb"], CONTRACT_ADDRESSES["usdt"]]
        except Exception as e:
            print(f"單跳路徑 (WBNB->USDT) 計算失敗: {e}")
        try:
            multi = router.functions.getAmountsOut(amt_in, [CONTRACT_ADDRESSES["wbnb"], CONTRACT_ADDRESSES["busd"], CONTRACT_ADDRESSES["usdt"]]).call()
            out_multi = multi[-1]
            if out_multi > best_out:
                best_out = out_multi
                best_path = [CONTRACT_ADDRESSES["wbnb"], CONTRACT_ADDRESSES["busd"], CONTRACT_ADDRESSES["usdt"]]
        except Exception as e:
            print(f"多跳路徑 (WBNB->BUSD->USDT) 計算失敗: {e}")
        return best_path, best_out

    def _approve_if_needed(self, token_addr: str, spender_addr: str, amt_wei: int) -> bool:
        curr_allow = self._get_allowance(token_addr, WALLET_ADDRESS, spender_addr)
        if curr_allow < amt_wei:
            nonce_ap = self.w3.eth.get_transaction_count(WALLET_ADDRESS, 'pending')
            tx = self._build_approve_tx(token_addr, spender_addr, amt_wei, nonce_ap)
            try:
                simulate_tx_call(self.w3, tx)
            except ContractLogicError as ce:
                print(f"❌ Dry-run Approve 失敗: {ce}")
                return False
            signed = self.w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            txh = self.w3.eth.send_raw_transaction(get_raw_tx(signed))
            print(f"Approve 交易送出: {txh.hex()}")
            rc = self.w3.eth.wait_for_transaction_receipt(txh, 180)
            if rc.status != 1:
                print("❌ Approve 失敗")
                return False
        return True

    def _build_approve_tx(self, token_addr: str, spender_addr: str, amt_wei: int, nonce_v: int) -> dict:
        c = self.w3.eth.contract(address=token_addr, abi=USDT_ABI)
        return c.functions.approve(spender_addr, amt_wei).build_transaction({
            'from': WALLET_ADDRESS,
            'gas': 100000,
            'gasPrice': min(self.w3.eth.gas_price, Web3.to_wei(MAX_GAS_PRICE_GWEI, 'gwei')),
            'nonce': nonce_v
        })

    def _get_allowance(self, token_addr: str, owner: str, spender: str) -> int:
        c = self.w3.eth.contract(address=token_addr, abi=USDT_ABI)
        return c.functions.allowance(owner, spender).call()

    def _get_token_balance(self, token_addr: str, account: str) -> int:
        c = self.w3.eth.contract(address=token_addr, abi=USDT_ABI)
        return c.functions.balanceOf(account).call()

    def execute_arbitrage(self, usdt_amt: float) -> bool:
        try:
            # BNB 餘額檢查
            bal_bnb = self.w3.eth.get_balance(WALLET_ADDRESS)
            if bal_bnb < self.w3.to_wei(BALANCE_BUFFER, 'ether'):
                print("⚠️ BNB 不足")
                return False

            # USDT 餘額檢查
            bal_usdt = self.usdt_contract.functions.balanceOf(WALLET_ADDRESS).call()
            amt_wei = self.w3.to_wei(usdt_amt, 'ether')
            if bal_usdt < amt_wei:
                print(f"⚠️ USDT 不足: 需要 {usdt_amt}, 實際 {bal_usdt / 1e18:.2f}")
                return False

            # 取得價格並檢查套利機會
            prices = self.price_manager.get_prices()
            if not prices:
                print("⚠️ 無法取得價格")
                return False
            opp = self.check_opportunity(prices)
            if not opp:
                print("⏸ 無套利機會")
                return False

            buy_dex = opp["buy_dex"]
            sell_dex = opp["sell_dex"]
            buy_price = opp["buy_price"]
            sell_price = opp["sell_price"]
            spread = opp["spread"]
            print(f"套利機會: {buy_dex} → {sell_dex}, 價差: {spread:.2f} USDT")

            router_buy = self.dex_map[buy_dex]
            router_sell = self.dex_map[sell_dex]

            # 確保 USDT Approve 足夠
            if not self._approve_if_needed(CONTRACT_ADDRESSES["usdt"], router_buy.address, amt_wei):
                print("❌ USDT Approve 失敗")
                return False

            # 選擇 USDT -> WBNB 最佳路徑
            path_buy, wbnb_out_est = self._decide_path_usdt_to_wbnb(router_buy, amt_wei)
            min_wbnb = int(wbnb_out_est * (100 - MAX_SLIPPAGE) / 100)
            nonce_buy = self.w3.eth.get_transaction_count(WALLET_ADDRESS, 'pending')

            # 建立買單交易 (USDT -> WBNB)
            buy_tx = router_buy.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                amt_wei,
                min_wbnb,
                path_buy,
                WALLET_ADDRESS,
                int(time.time() + 300)
            ).build_transaction({
                'from': WALLET_ADDRESS,
                'gas': 500000,
                'gasPrice': min(self.w3.eth.gas_price, Web3.to_wei(MAX_GAS_PRICE_GWEI, 'gwei')),
                'nonce': nonce_buy
            })
            # Dry-run 模擬
            try:
                simulate_tx_call(self.w3, buy_tx)
            except ContractLogicError as ce:
                print(f"❌ Dry-run 買入失敗: {ce}")
                return False
            signed_buy = self.w3.eth.account.sign_transaction(buy_tx, PRIVATE_KEY)
            txh_buy = self.w3.eth.send_raw_transaction(get_raw_tx(signed_buy))
            print(f"買入交易送出, TxHash: {txh_buy.hex()}")
            rc_buy = self.w3.eth.wait_for_transaction_receipt(txh_buy, 180)
            if rc_buy.status != 1:
                print("❌ 買入失敗")
                return False

            # 檢查 WBNB 餘額
            wbnb_bal = self._get_token_balance(CONTRACT_ADDRESSES["wbnb"], WALLET_ADDRESS)
            if wbnb_bal <= 0:
                print("❌ WBNB 餘額不足")
                return False

            # 確保 WBNB Approve 足夠
            if not self._approve_if_needed(CONTRACT_ADDRESSES["wbnb"], router_sell.address, wbnb_bal):
                print("❌ WBNB Approve 失敗")
                return False

            # 選擇 WBNB -> USDT 最佳路徑
            path_sell, usdt_out_est = self._decide_path_wbnb_to_usdt(router_sell, wbnb_bal)
            min_usdt = int(usdt_out_est * (100 - MAX_SLIPPAGE) / 100)
            nonce_sell = self.w3.eth.get_transaction_count(WALLET_ADDRESS, 'pending')

            sell_tx = router_sell.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                wbnb_bal,
                min_usdt,
                path_sell,
                WALLET_ADDRESS,
                int(time.time() + 300)
            ).build_transaction({
                'from': WALLET_ADDRESS,
                'gas': 500000,
                'gasPrice': min(self.w3.eth.gas_price, Web3.to_wei(MAX_GAS_PRICE_GWEI, 'gwei')),
                'nonce': nonce_sell
            })
            # Dry-run 模擬
            try:
                simulate_tx_call(self.w3, sell_tx)
            except ContractLogicError as ce:
                print(f"❌ Dry-run 賣出失敗: {ce}")
                return False
            signed_sell = self.w3.eth.account.sign_transaction(sell_tx, PRIVATE_KEY)
            txh_sell = self.w3.eth.send_raw_transaction(get_raw_tx(signed_sell))
            print(f"賣出交易送出, TxHash: {txh_sell.hex()}")
            rc_sell = self.w3.eth.wait_for_transaction_receipt(txh_sell, 180)
            if rc_sell.status != 1:
                print("❌ 賣出失敗")
                return False

            final_usdt = self.usdt_contract.functions.balanceOf(WALLET_ADDRESS).call()
            profit_wei = final_usdt - bal_usdt
            profit = profit_wei / 1e18

            if profit < 0:
                print(f"❌ 最終虧損: {profit:.6f} USDT（可能被手續費或滑點影響）")
            else:
                print(f"🎉 最終利潤: {profit:.6f} USDT（未扣除Gas費用）")
            return profit > 0

        except ContractLogicError as ce:
            print(f"⛔ 交易 Revert: {ce}")
            return False
        except Exception as e:
            print(f"❌ 執行錯誤: {e}")
            return False

# --------------------------
# 9. 增強版終端顯示
# --------------------------
class AdvancedDisplay:
    @staticmethod
    def clear_screen():
        os.system('cls' if os.name == 'nt' else 'clear')

    @staticmethod
    def show(data):
        AdvancedDisplay.clear_screen()
        print("\n🔥 BSC 交易所 USDT 交易對套利監控系統")
        print("┌──────────────────────────────────────────")
        for exchange, pairs in data.items():
            print(f"\n🔷 {exchange.upper()} 交易所")
            print(f"{'交易對':<10}{'買入價(USDT)':<18}{'賣出價(USDT)':<18}{'價差':<12}")
            print("────────────────────────────────────────────")
            for pair, values in pairs.items():
                print(f"{pair:<10}{values['buy']:<18.6f}{values['sell']:<18.6f}{values['spread']:+.6f}")
        print("\n🔄 每3秒刷新 | CTRL+C 結束")

# --------------------------
# 10. 主程式
# --------------------------
def main():
    # 初始化 EnhancedWeb3 連線管理
    enhanced_web3 = EnhancedWeb3(BSC_RPC_URLS)
    print(f"✅ 連接成功 | 最新區塊: {enhanced_web3.eth.block_number}")
    
    # 初始化套利執行器
    executor = ArbitrageExecutor(enhanced_web3.w3)
    display = AdvancedDisplay()
    
    tcount = int(input("▶ 請輸入最大檢查次數: "))
    iv = int(input("⏱ 請輸入檢查間隔(秒): "))
    usdt_amt = float(input("💵 請輸入單次交易金額 (USDT): "))
    print("\n=== 套利機器人啟動 ===")
    success_count = 0
    for i in range(tcount):
        print(f"\n🔍 正在檢查第 {i+1}/{tcount} 次...")
        price_data = executor.price_manager.get_prices()
        display.show(price_data)
        if executor.execute_arbitrage(usdt_amt):
            success_count += 1
        else:
            print("⚠️ 交易未成功或未達套利條件")
        if i < tcount - 1:
            time.sleep(iv)
    print("\n=== 結束 ===")
    print(f"成功交易次數: {success_count}/{tcount}")

if __name__ == "__main__":
    main()
