import requests
import json
import time
import hashlib
import hmac
from urllib.parse import urlencode
from datetime import datetime, timedelta
import logging
import warnings
import os

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

# 配置日志
def setup_logger():
    # 创建logs目录
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # 按日期生成日志文件名
    log_filename = os.path.join(log_dir, f"{datetime.now().strftime('%Y-%m-%d')}.log")
    
    # 创建logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    
    # 避免重复添加handler
    if not logger.handlers:
        # 创建文件handler
        file_handler = logging.FileHandler(log_filename, encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        
        # 创建控制台handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        
        # 创建formatter
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        
        # 设置formatter
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        # 添加handler到logger
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
    
    return logger

logger = setup_logger()

class BinanceFutureMonitor:
    def __init__(self, feishu_webhook):
        self.base_url = "https://fapi.binance.com"
        self.feishu_webhook = feishu_webhook
        self.exchange_info_endpoint = "/fapi/v1/exchangeInfo"
        self.last_symbols = set()
        self.last_alert_time = None  # 记录上次发送警报的时间
        
    def get_future_symbols(self):
        """
        获取币安永续合约交易对信息
        """
        max_retries = 3
        retry_delay = 5  # 秒
        
        for retry in range(max_retries):
            try:
                url = f"{self.base_url}{self.exchange_info_endpoint}"
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                
                data = response.json()
                symbols = set()
                for symbol_info in data.get('symbols', []):
                    # 只获取永续合约(合约类型为PERPETUAL)
                    if symbol_info.get('contractType') == 'PERPETUAL':
                        symbols.add(symbol_info['symbol'])
                
                logger.info(f"成功获取币安合约信息，共 {len(symbols)} 个合约")
                return symbols
            except Exception as e:
                logger.error(f"获取币安合约信息失败 (重试 {retry+1}/{max_retries}): {e}")
                if retry < max_retries - 1:
                    logger.info(f"{retry_delay}秒后重试...")
                    time.sleep(retry_delay)
                else:
                    logger.error("达到最大重试次数，返回空集合")
                    return set()
    
    def send_feishu_message(self, message):
        """
        发送消息到飞书
        """
        try:
            headers = {'Content-Type': 'application/json'}
            payload = {
                "msg_type": "text",
                "content": {
                    "text": message
                }
            }
            
            response = requests.post(self.feishu_webhook, headers=headers, data=json.dumps(payload), timeout=10)
            if response.status_code == 200:
                logger.info("飞书消息发送成功")
                return True
            else:
                logger.error(f"飞书消息发送失败: {response.text}")
                return False
        except Exception as e:
            logger.error(f"发送飞书消息异常: {e}")
            return False
    
    def check_new_symbols(self):
        """
        检查是否有新的合约上市
        """
        check_time = datetime.now()
        logger.info(f"执行检查，时间: {check_time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        current_symbols = self.get_future_symbols()
        
        if not self.last_symbols:
            # 首次运行，记录当前合约列表
            self.last_symbols = current_symbols
            logger.info(f"初始化合约列表，共 {len(current_symbols)} 个合约")
            return False  # 返回False表示没有发送警报
        
        # 找出新增的合约
        new_symbols = current_symbols - self.last_symbols
        
        if new_symbols:
            message = f"【币安合约新品上线】\n时间: {check_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            message += "新增合约:\n"
            for symbol in new_symbols:
                message += f"- {symbol}\n"
            
            logger.info(f"发现 {len(new_symbols)} 个新合约: {', '.join(new_symbols)}")
            logger.info(f"当前合约总数: {len(current_symbols)}, 上次合约总数: {len(self.last_symbols)}")
            success = self.send_feishu_message(message)
            if success:
                self.last_alert_time = check_time  # 记录发送警报时间
                logger.info(f"已记录警报发送时间: {self.last_alert_time.strftime('%Y-%m-%d %H:%M:%S')}")
            
            # 更新合约列表
            self.last_symbols = current_symbols
            logger.info(f"已更新合约列表，当前共 {len(current_symbols)} 个合约")
            return True  # 返回True表示已发送警报
        
        # 更新合约列表
        self.last_symbols = current_symbols
        logger.info(f"无新增合约，当前共 {len(current_symbols)} 个合约")
        return False  # 返回False表示没有发送警报
    
    def is_within_operating_hours(self):
        """
        判断当前是否在操作时间内 (24小时监控)
        """
        return True
    
    def should_pause_after_alert(self):
        """
        判断是否应该在发送警报后暂停2小时
        """
        if self.last_alert_time is None:
            return False
        
        # 计算距离上次警报是否已过去2小时
        time_since_last_alert = datetime.now() - self.last_alert_time
        return time_since_last_alert < timedelta(hours=2)
    
    def calculate_sleep_time(self):
        """
        计算合适的休眠时间
        """
        now = datetime.now()
        
        # 如果刚发过警报，需要等待更合理的时间（如2小时）
        if self.should_pause_after_alert():
            remaining_time = timedelta(hours=2) - (now - self.last_alert_time)
            return min(remaining_time.total_seconds(), 3600)  # 最多休眠1小时
        
        return None  # 不需要特殊休眠
    

    
    def run(self, interval=60):
        """
        运行监控程序
        :param interval: 常规检查间隔(秒)，但在特定条件下会被覆盖
        """
        logger.info("开始监控币安永续合约新品上市...")
        
        while True:
            try:
                # 检查是否需要特殊休眠
                sleep_time = self.calculate_sleep_time()
                
                if sleep_time is not None:
                    logger.info(f"根据规则需要休眠 {sleep_time:.0f} 秒")
                    time.sleep(sleep_time)
                    continue
                
                # 检查是否在操作时间内
                if not self.is_within_operating_hours():
                    logger.info("当前不在操作时间范围内 (14:00-23:00)，进入休眠")
                    time.sleep(300)  # 休眠5分钟再检查
                    continue
                
                # 正常执行检查
                alert_sent = self.check_new_symbols()
                
                # 如果发送了警报，则休眠2小时
                if alert_sent:
                    logger.info("检测到新合约，将在2小时内暂停常规检查")
                    # 实际上完全停止运行2小时
                    time.sleep(2 * 60 * 60)  # 休眠2小时
                else:
                    time.sleep(interval)
                    
            except KeyboardInterrupt:
                logger.info("程序被用户中断")
                break
            except Exception as e:
                logger.error(f"监控过程中出现异常: {e}")
                time.sleep(interval)

# 使用示例
if __name__ == "__main__":
    # 从环境变量读取飞书webhook地址，如果没有设置则使用默认值
    FEISHU_WEBHOOK = os.environ.get('FEISHU_WEBHOOK', 'https://open.feishu.cn/open-apis/bot/v2/hook/9804fe72-cd95-48a4-9fc2-368907210451')
    
    # 创建监控实例
    monitor = BinanceFutureMonitor(FEISHU_WEBHOOK)
    
    # 开始监控，每3300秒（55分钟）检查一次（在操作时段内）
    monitor.run(interval=3300)