"""
人工确认命令行界面

负责：
- 查看待确认信号
- 确认执行交易
- 取消信号
- 查看系统状态
"""

import sys
import argparse
from typing import List, Optional

from .signal_manager import signal_manager, Signal, SignalStatus
from .notifier import feishu_notifier
from utils.logger import logger


class CommandHandler:
    """命令行处理器"""
    
    def __init__(self):
        """初始化命令行处理器"""
        self.signal_mgr = signal_manager
        self.notifier = feishu_notifier
        logger.info("✅ 命令行处理器初始化完成")
    
    def list_signals(self, symbol: Optional[str] = None):
        """
        查看待确认信号
        
        Args:
            symbol: 可选的币种符号过滤
        """
        logger.info("📊 查看待确认信号...")
        
        if symbol:
            signal = self.signal_mgr.get_signal_by_symbol(symbol)
            signals = [signal] if signal else []
        else:
            signals = self.signal_mgr.get_pending_signals()
        
        if not signals:
            print("\nℹ️ 暂无待确认信号\n")
            return
        
        print(f"\n📊 待确认信号：{len(signals)}个\n")
        print("=" * 80)
        
        for i, signal in enumerate(signals, 1):
            result = signal.scoring_result
            remaining = signal.time_remaining()
            
            print(f"\n【信号 {i}】")
            print(f"  ID: {signal.id[:8]}...")
            print(f"  币种：{signal.symbol}")
            print(f"  综合评分：{result.total_score:.2f}/10")
            print(f"  当前价格：{signal.current_price:.2f} USDT")
            print(f"  入场区间：{signal.entry_min:.2f} - {signal.entry_max:.2f} USDT")
            print(f"  止损位：{signal.stop_loss:.2f} USDT")
            print(f"  止盈位：{signal.take_profit_1:.2f} / {signal.take_profit_2:.2f} USDT")
            print(f"  创建时间：{signal.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"  过期时间：{signal.expire_at.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"  剩余时间：{remaining.seconds // 60}分钟")
            print(f"  评分详情：合约={result.contract_score:.1f}, 基本={result.fundamental_score:.1f}, "
                  f"技术={result.technical_score:.1f}, 情绪={result.sentiment_score:.1f}")
            
            if result.veto:
                print(f"  ⚠️ 否决原因：{result.veto_reason}")
            
            print()
        
        print("=" * 80)
        print(f"\n提示：使用 'python main.py confirm <币种>' 确认执行交易\n")
    
    def confirm_signal(self, symbol: str, stop_loss: Optional[float] = None,
                      take_profit: Optional[float] = None,
                      position_size: Optional[float] = None) -> bool:
        """
        确认执行交易
        
        Args:
            symbol: 币种符号
            stop_loss: 可选的自定义止损价
            take_profit: 可选的自定义止盈价
            position_size: 可选的自定义仓位
            
        Returns:
            是否确认成功
        """
        logger.info(f"确认执行 {symbol} 做空交易...")
        
        signal = self.signal_mgr.get_signal_by_symbol(symbol)
        
        if not signal:
            print(f"\n❌ 未找到 {symbol} 的待确认信号\n")
            return False
        
        # 显示信号详情
        self._print_signal_details(signal)
        
        # 确认操作
        print("\n请确认以下信息：")
        print(f"  币种：{signal.symbol}")
        print(f"  入场区间：{signal.entry_min:.2f} - {signal.entry_max:.2f} USDT")
        
        if stop_loss:
            print(f"  自定义止损：{stop_loss:.2f} USDT")
        else:
            print(f"  止损：{signal.stop_loss:.2f} USDT")
        
        if take_profit:
            print(f"  自定义止盈：{take_profit:.2f} USDT")
        else:
            print(f"  止盈 1/2: {signal.take_profit_1:.2f} / {signal.take_profit_2:.2f} USDT")
        
        if position_size:
            print(f"  自定义仓位：{position_size} USDT")
        else:
            print(f"  默认仓位：4.0 USDT")
        
        confirm = input("\n是否确认执行？(yes/no): ").strip().lower()
        
        if confirm in ['yes', 'y']:
            # 确认信号
            self.signal_mgr.confirm_signal(signal.id)
            
            # 发送通知
            self.notifier.send_signal_notification(signal)
            
            print(f"\n✅ 已确认 {symbol} 做空信号")
            print(f"📱 飞书通知已发送")
            print(f"\n下一步：系统将在合适时机执行交易，或手动执行后调用 'python main.py executed {signal.id}'\n")
            return True
        else:
            print("\n❌ 已取消确认\n")
            return False
    
    def cancel_signal(self, symbol: str, reason: str = "") -> bool:
        """
        取消信号
        
        Args:
            symbol: 币种符号
            reason: 取消原因
            
        Returns:
            是否取消成功
        """
        logger.info(f"取消 {symbol} 信号...")
        
        signal = self.signal_mgr.get_signal_by_symbol(symbol)
        
        if not signal:
            print(f"\n❌ 未找到 {symbol} 的待确认信号\n")
            return False
        
        if not reason:
            reason = input("请输入取消原因：").strip()
        
        self.signal_mgr.cancel_signal(signal.id, reason)
        
        print(f"\n✅ 已取消 {symbol} 信号")
        print(f"原因：{reason}\n")
        return True
    
    def show_status(self):
        """查看系统状态"""
        logger.info("查看系统状态...")
        
        pending_signals = self.signal_mgr.get_pending_signals()
        
        print("\n📊 系统状态")
        print("=" * 60)
        print(f"待确认信号：{len(pending_signals)}个")
        print(f"总信号数：{len(self.signal_mgr.signals)}个")
        print(f"飞书通知：{'✅ 已配置' if self.notifier.webhook_url else '❌ 未配置'}")
        print("=" * 60)
        
        if pending_signals:
            print("\n最近的信号:")
            for signal in pending_signals[-3:]:
                print(f"  • {signal.symbol}: {signal.scoring_result.total_score:.2f}分")
        print()
    
    def _print_signal_details(self, signal: Signal):
        """打印信号详情"""
        result = signal.scoring_result
        
        print("\n" + "=" * 80)
        print(f"🎯 信号详情：{signal.symbol}")
        print("=" * 80)
        print(f"综合评分：{result.total_score:.2f}/10")
        print(f"操作建议：{self.signal_mgr.scoring_engine.get_recommendation(result)}")
        print(f"\n评分详情:")
        print(f"  合约数据：{result.contract_score:.1f}/10")
        print(f"  基本面：{result.fundamental_score:.1f}/10")
        print(f"  技术面：{result.technical_score:.1f}/10")
        print(f"  情绪面：{result.sentiment_score:.1f}/10")
        
        if result.veto:
            print(f"\n⚠️ 否决：{result.veto_reason}")
        
        print(f"\n关键价位:")
        print(f"  当前价：{signal.current_price:.2f} USDT")
        print(f"  入场：{signal.entry_min:.2f} - {signal.entry_max:.2f} USDT")
        print(f"  止损：{signal.stop_loss:.2f} USDT (+{(signal.stop_loss/signal.current_price-1)*100:.1f}%)")
        print(f"  止盈 1: {signal.take_profit_1:.2f} USDT (-{(signal.current_price-signal.take_profit_1)/signal.current_price*100:.1f}%)")
        print(f"  止盈 2: {signal.take_profit_2:.2f} USDT (-{(signal.current_price-signal.take_profit_2)/signal.current_price*100:.1f}%)")
        print("=" * 80 + "\n")


# 全局命令行处理器
cmd_handler = CommandHandler()


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='币安新币精准做空系统 - 人工确认命令行工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py signals              查看所有待确认信号
  python main.py signals BTC          查看 BTC 相关信号
  python main.py confirm BTC          确认 BTC 做空信号
  python main.py cancel BTC           取消 BTC 信号
  python main.py status               查看系统状态
        """
    )
    
    parser.add_argument(
        'command',
        nargs='?',
        choices=['signals', 'confirm', 'cancel', 'status'],
        help='命令类型'
    )
    
    parser.add_argument(
        'symbol',
        nargs='?',
        help='交易对符号 (用于 confirm/cancel 命令)'
    )
    
    parser.add_argument(
        '--stop-loss',
        type=float,
        help='自定义止损价格'
    )
    
    parser.add_argument(
        '--take-profit',
        type=float,
        help='自定义止盈价格'
    )
    
    parser.add_argument(
        '--position-size',
        type=float,
        help='自定义仓位大小 (USDT)'
    )
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        if args.command == 'signals':
            cmd_handler.list_signals(args.symbol)
        elif args.command == 'confirm':
            if not args.symbol:
                print("❌ 错误：confirm 命令需要指定币种符号")
                return
            cmd_handler.confirm_signal(
                args.symbol,
                args.stop_loss,
                args.take_profit,
                args.position_size
            )
        elif args.command == 'cancel':
            if not args.symbol:
                print("❌ 错误：cancel 命令需要指定币种符号")
                return
            cmd_handler.cancel_signal(args.symbol)
        elif args.command == 'status':
            cmd_handler.show_status()
    
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断操作\n")
    except Exception as e:
        logger.error(f"❌ 命令执行异常：{e}")
        print(f"\n❌ 错误：{e}\n")


if __name__ == '__main__':
    main()
