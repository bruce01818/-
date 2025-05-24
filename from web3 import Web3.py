import json
import time
import threading
from itertools import permutations
from decimal import Decimal
from web3 import Web3
import requests

# ========== 1. 基本設定 ==========
BSC_RPC = "https://bsc-dataseed.binance.org/"
web3 = Web3(Web3.HTTPProvider(BSC_RPC))
if not web3.is_connected():
    raise Exception("❌ 無法連接到 BSC")

# 你的錢包資訊（請填入自己的私鑰）
PRIVATE_KEY = "YOUR_PRIVATE_KEY"
ACCOUNT = web3.eth.account.from_key(PRIVATE_KEY).address

# Telegram Bot 設定（填入你自己的 bot token 與 chat id）
TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"
def tg_send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        print(f"[Telegram] 發送錯誤: {e}")

# ========== 2. 合約與 ABI ==========
# PancakeSwap Router 主網地址與內嵌 ABI（僅包含 getAmountsOut 與 swapExactTokensForTokens）
ROUTER_ADDR = Web3.to_checksum_address("0x10ED43C718714eb63d5aA57B78B54704E256024E")
ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"}
        ],
        "name": "getAmountsOut",
        "outputs": [
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactTokensForTokens",
        "outputs": [
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}
        ],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]
router = web3.eth.contract(address=ROUTER_ADDR, abi=ROUTER_ABI)

# ========== 3. 代幣設定 ==========
# 代幣地址（均轉為 checksum 格式）
TOKENS = {
    "USDT": Web3.to_checksum_address("0x55d398326f99059fF775485246999027B3197955"),
    "BUSD": Web3.to_checksum_address("0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56"),
    "WBNB": Web3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
    "CAKE": Web3.to_checksum_address("0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82"),
    "USDC": Web3.to_checksum_address("0x8AC76a51cc950d9822D68b83fe1ad97B32Cd580d"),
    "BTCB": Web3.to_checksum_address("0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c"),
    "ETH": Web3.to_checksum_address("0x2170Ed0880ac9A755fd29B2688956BD959F933F8"),
    "DOT": Web3.to_checksum_address("0x7083609fCE4d1d8Dc0C979AAb8c869Ea2C873402"),
    "LINK": Web3.to_checksum_address("0xF8A0BF9cF54Bb92F17374d9e9A321E6a111a51bD")
}

# 各代幣精度（USDT/USDC:6, 其他:18）
DECIMALS = {
    "USDT": 6,
    "USDC": 6,
    "BUSD": 18,
    "CAKE": 18,
    "WBNB": 18,
    "BTCB": 18,
    "ETH": 18,
    "DOT": 18,
    "LINK": 18
}

# ========== 4. 工具函數 ==========
def to_token_amount(amount: float, symbol: str) -> int:
    return int(amount * 10**DECIMALS[symbol])

def from_token_amount(value: int, symbol: str) -> Decimal:
    return Decimal(value) / Decimal(10**DECIMALS[symbol])

def check_liquidity(token0: str, token1: str) -> bool:
    """檢查交易對的流動性是否足夠"""
    try:
        # 獲取交易對地址
        factory = web3.eth.contract(
            address=Web3.to_checksum_address("0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"),
            abi=[{
                "inputs": [
                    {"internalType": "address", "name": "tokenA", "type": "address"},
                    {"internalType": "address", "name": "tokenB", "type": "address"}
                ],
                "name": "getPair",
                "outputs": [{"internalType": "address", "name": "pair", "type": "address"}],
                "stateMutability": "view",
                "type": "function"
            }]
        )
        
        pair_address = factory.functions.getPair(TOKENS[token0], TOKENS[token1]).call()
        if pair_address == "0x0000000000000000000000000000000000000000":
            return False
            
        # 獲取流動性
        pair = web3.eth.contract(
            address=pair_address,
            abi=[{
                "inputs": [],
                "name": "getReserves",
                "outputs": [
                    {"internalType": "uint112", "name": "_reserve0", "type": "uint112"},
                    {"internalType": "uint112", "name": "_reserve1", "type": "uint112"},
                    {"internalType": "uint32", "name": "_blockTimestampLast", "type": "uint32"}
                ],
                "stateMutability": "view",
                "type": "function"
            }]
        )
        
        reserve0, reserve1, _ = pair.functions.getReserves().call()
        
        # 檢查流動性是否足夠（至少10,000 USDT等值）
        min_liquidity = 10000 * 10**18  # 10,000 USDT等值
        return reserve0 >= min_liquidity or reserve1 >= min_liquidity
        
    except Exception as e:
        print(f"檢查流動性時出錯: {e}")
        return False

def get_price(amount_in: int, path: list) -> int:
    # 回傳以 path 最後代幣單位表示的數值
    return router.functions.getAmountsOut(amount_in, path).call()[-1]

def get_gas_price():
    """獲取當前Gas價格，並增加10%作為緩衝"""
    gas_price = web3.eth.gas_price
    return int(gas_price * 1.1)

def estimate_gas_cost(tx):
    """估算交易所需的Gas費用"""
    try:
        gas_estimate = web3.eth.estimate_gas(tx)
        return gas_estimate
    except Exception as e:
        print(f"Gas估算錯誤: {e}")
        return 300000  # 返回預設值

def build_tx(function_call):
    """構建交易參數"""
    gas_price = get_gas_price()
    nonce = web3.eth.get_transaction_count(ACCOUNT)
    
    # 構建基本交易參數
    tx = {
        'from': ACCOUNT,
        'gas': 300000,
        'gasPrice': gas_price,
        'nonce': nonce,
        'chainId': 56  # BSC主網的chainId
    }
    
    # 估算實際Gas用量
    try:
        gas_estimate = estimate_gas_cost(tx)
        tx['gas'] = int(gas_estimate * 1.2)  # 增加20%作為緩衝
    except Exception as e:
        print(f"Gas估算失敗，使用預設值: {e}")
    
    return tx

def execute_swap(path: list, amount_in: int, amount_out_min: int):
    """執行代幣交換"""
    try:
        # 設置交易截止時間（30秒）
        deadline = int(time.time()) + 30
        
        # 構建交易
        tx = router.functions.swapExactTokensForTokens(
            amount_in, 
            amount_out_min, 
            path, 
            ACCOUNT, 
            deadline
        ).build_transaction(build_tx(router))
        
        # 簽名交易
        signed = web3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        
        # 發送交易
        tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)
        
        # 等待交易確認
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        
        # 檢查交易狀態
        if receipt['status'] == 1:
            return receipt
        else:
            raise Exception("交易失敗")
            
    except Exception as e:
        error_msg = f"交易執行錯誤: {str(e)}"
        print(error_msg)
        tg_send(error_msg)
        raise

# ========== 5. 三角套利檢測與交易 ==========
BASE = "USDT"           # 起始與回收代幣
amount_in_token = 50.0  # 每次投入 50 USDT（增加交易金额以提高成功率）
profit_threshold = 0.5  # 利潤門檻：至少 0.5 USDT（提高利润门槛）

# 定義優先套利組合
PRIORITY_PAIRS = [
    ("USDT", "WBNB", "BUSD"),
    ("USDT", "WBNB", "USDC"),
    ("USDT", "BTCB", "BUSD"),
    ("USDT", "ETH", "BUSD"),
    ("USDT", "CAKE", "BUSD"),
    ("USDT", "DOT", "BUSD"),
    ("USDT", "LINK", "BUSD")
]

def worker(path_symbols: tuple):
    """工作線程函數"""
    path = [TOKENS[s] for s in (*path_symbols, path_symbols[0])]
    try:
        # 檢查流動性
        for i in range(len(path_symbols)):
            if not check_liquidity(path_symbols[i], path_symbols[(i+1)%3]):
                print(f"⚠️ 路徑 {'->'.join(path_symbols)} 中 {path_symbols[i]}-{path_symbols[(i+1)%3]} 流動性不足")
                return
        
        # 獲取當前價格
        amt_in = to_token_amount(amount_in_token, BASE)
        out = get_price(amt_in, path)
        profit = out - amt_in
        profit_token = from_token_amount(profit, BASE)

        # 計算Gas成本
        gas_price = get_gas_price()
        gas_cost = Decimal(gas_price * 300000) / Decimal(10**18)  # 轉換為BNB
        gas_cost_usdt = gas_cost * Decimal(get_price(web3.to_wei(1, 'ether'), [TOKENS['WBNB'], TOKENS['USDT']])) / Decimal(10**18)
        
        # 計算淨利潤
        net_profit = profit_token - gas_cost_usdt

        # 印出檢測結果
        print(f"[{time.strftime('%H:%M:%S')}] 檢測 {'->'.join(path_symbols)} | 毛利 {profit_token:.6f} {BASE} | Gas成本 {gas_cost_usdt:.6f} USDT | 淨利 {net_profit:.6f} USDT")
        
        if net_profit >= Decimal(profit_threshold):
            msg = f"💰 套利機會：{'->'.join(path_symbols)}\n毛利: {profit_token:.6f} {BASE}\nGas成本: {gas_cost_usdt:.6f} USDT\n淨利: {net_profit:.6f} USDT"
            print(msg)
            tg_send(msg)
            
            # 設定最低接受輸出為 99.7% 的預估數量（0.3% 滑點保護）
            min_out = int(out * 0.997)
            receipt = execute_swap(path, amt_in, min_out)
            
            tx_msg = f"✅ 交易完成：{receipt.transactionHash.hex()}\nGas使用: {receipt.gasUsed}\nGas價格: {web3.from_wei(gas_price, 'gwei')} Gwei"
            print(tx_msg)
            tg_send(tx_msg)
            
    except Exception as e:
        error_msg = f"[{path_symbols}] 錯誤：{str(e)}"
        print(error_msg)
        tg_send(error_msg)

# ========== 6. 多線程監控 ==========
print("🔎 開始三角套利監控與自動交易...\n")
while True:
    threads = []
    # 優先處理優先套利組合
    for pair in PRIORITY_PAIRS:
        t = threading.Thread(target=worker, args=(pair,))
        t.start()
        threads.append(t)
    
    # 等待優先組合處理完成
    for t in threads:
        t.join()
    
    # 每輪掃描間隔 3 秒（縮短間隔以提高機會捕捉）
    time.sleep(3)
