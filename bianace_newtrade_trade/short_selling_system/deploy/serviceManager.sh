#!/bin/bash

###############################################################################
# 币安新币精准做空系统 - 服务管理脚本
# 用途：管理系统服务（启动/停止/重启/查看状态）
###############################################################################

set -e

SERVICE_NAME="short-selling-system"
CONTAINER_NAME="short-selling-system"
SERVICE_FILE="short-selling-system.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 打印函数
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查是否在远程服务器
check_remote() {
    if ! grep -q "short-selling-system" /etc/systemd/system/${SERVICE_NAME}.service 2>/dev/null; then
        print_error "系统服务未安装，请先运行：sudo bash deploy.sh"
        exit 1
    fi
}

# 启动服务
start_service() {
    print_info "正在启动 ${SERVICE_NAME} 服务..."
    
    if sudo systemctl start ${SERVICE_NAME}; then
        print_success "服务已启动"
        show_status
    else
        print_error "启动失败"
        exit 1
    fi
}

# 停止服务
stop_service() {
    print_info "正在停止 ${SERVICE_NAME} 服务..."
    
    if sudo systemctl stop ${SERVICE_NAME}; then
        print_success "服务已停止"
        show_status
    else
        print_error "停止失败"
        exit 1
    fi
}

# 重启服务
restart_service() {
    print_info "正在重启 ${SERVICE_NAME} 服务..."
    
    if sudo systemctl restart ${SERVICE_NAME}; then
        print_success "服务已重启"
        show_status
    else
        print_error "重启失败"
        exit 1
    fi
}

# 查看状态
show_status() {
    echo ""
    print_info "=== ${SERVICE_NAME} 服务状态 ==="
    echo ""
    
    # systemd 服务状态
    if sudo systemctl is-active --quiet ${SERVICE_NAME}; then
        echo -e "服务状态：${GREEN}运行中${NC}"
    else
        echo -e "服务状态：${RED}已停止${NC}"
    fi
    
    # Docker 容器状态
    echo ""
    print_info "=== Docker 容器状态 ==="
    docker ps -a --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    
    # 最近日志
    echo ""
    print_info "=== 最近日志（最后 10 行）==="
    sudo journalctl -u ${SERVICE_NAME} -n 10 --no-pager
    
    echo ""
}

# 查看实时日志
show_logs() {
    print_info "正在查看实时日志 (Ctrl+C 退出)..."
    sudo journalctl -u ${SERVICE_NAME} -f
}

# 启用开机自启
enable_service() {
    print_info "正在启用开机自启..."
    
    if sudo systemctl enable ${SERVICE_NAME}; then
        print_success "已启用开机自启"
    else
        print_error "启用失败"
        exit 1
    fi
}

# 禁用开机自启
disable_service() {
    print_info "正在禁用开机自启..."
    
    if sudo systemctl disable ${SERVICE_NAME}; then
        print_success "已禁用开机自启"
    else
        print_error "禁用失败"
        exit 1
    fi
}

# 重新加载 systemd 配置
reload_daemon() {
    print_info "正在重新加载 systemd 配置..."
    sudo systemctl daemon-reload
    print_success "配置已重新加载"
}

# 显示使用帮助
show_help() {
    echo "币安新币精准做空系统 - 服务管理脚本"
    echo ""
    echo "用法：$0 [命令]"
    echo ""
    echo "命令:"
    echo "  start       启动服务"
    echo "  stop        停止服务"
    echo "  restart     重启服务"
    echo "  status      查看状态"
    echo "  logs        查看实时日志"
    echo "  enable      启用开机自启"
    echo "  disable     禁用开机自启"
    echo "  reload      重新加载配置"
    echo "  help        显示帮助信息"
    echo ""
    echo "示例:"
    echo "  $0 start          # 启动服务"
    echo "  $0 status         # 查看状态"
    echo "  $0 logs           # 查看实时日志"
    echo "  $0 enable         # 启用开机自启"
    echo ""
}

# 主函数
main() {
    case "${1:-status}" in
        start)
            check_remote
            start_service
            ;;
        stop)
            check_remote
            stop_service
            ;;
        restart)
            check_remote
            restart_service
            ;;
        status)
            check_remote
            show_status
            ;;
        logs)
            check_remote
            show_logs
            ;;
        enable)
            check_remote
            enable_service
            ;;
        disable)
            check_remote
            disable_service
            ;;
        reload)
            reload_daemon
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            print_error "未知命令：$1"
            echo ""
            show_help
            exit 1
            ;;
    esac
}

# 执行主函数
main "$@"
