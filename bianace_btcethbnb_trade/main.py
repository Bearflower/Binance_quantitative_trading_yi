import os
import sys
from datetime import datetime
from utils.deepseek_client import send_multiple_screenshots_to_deepseek, save_response
from utils.lark_notifier import notify_completion, notify_error
from config.settings import REPORT_OUTPUT_DIR, SCREENSHOT_OUTPUT_DIR, SUPPORTED_CURRENCIES
import logging
from logging.handlers import RotatingFileHandler
import argparse


# Setup logging
def setup_logging():
    """Setup logging configuration"""
    log_dir = "./logs"
    os.makedirs(log_dir, exist_ok=True)
    
    log_file = os.path.join(log_dir, f"{datetime.now().strftime('%Y-%m-%d')}.log")
    
    # Create logger
    logger = logging.getLogger("binance_trade_analyzer")
    logger.setLevel(logging.INFO)
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # Create file handler
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10*1024*1024, backupCount=5
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    return logger


# analyze_currency function removed as it depends on deprecated screenshot functionality


# analyze_currency_multiple_screenshots function removed as it depends on deprecated screenshot functionality


# analyze_screenshots_from_path function removed as it depends on deprecated screenshot functionality


# main function removed as it depends on deprecated screenshot functionality


# main_multiple_screenshots function removed as it depends on deprecated screenshot functionality


if __name__ == "__main__":
    print("Main script has been deprecated. Please use scheduler.py for analysis.")
    print("The system now uses Binance API data directly instead of screenshots.")
    print("Run: python scheduler.py --auto-start-chrome")
