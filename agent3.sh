#!/bin/sh
# NetWatch Agent v3 (OpenWrt)
# Только Wi-Fi подключения — проводные игнорируются

set -e

CONFIG_FILE="/etc/netwatch/config"
LOG_FILE="/mnt/usb/log/netwatch/netwatch-agent.log"

# Загрузка конфига
if [ -f "$CONFIG_FILE" ]; then
    . "$CONFIG_FILE"
else
    PI_URL="https://netwatchagent.duckdns.org"
    ROUTER_ID=""
    AGENT_SECRET=""
fi

log() {
    local ts=$(date '+%Y-%m-%d %H:%M:%S')
    local msg="[$ts] $1"
    echo "$msg"
    if [ -d "$(dirname "$LOG_FILE")" ] 2>/dev/null; then
        echo "$msg" >> "$LOG_FILE"
    fi
}

# JSON escape function for shell strings
json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\//\\\//g; s/\t/\\t/g; s/\n/\\n/g; s/\r/\\r/g'
}

# --- pair ---
cmd_pair() {
    log "Начинаем pair..."

    # Parse flags: --code <code> --url <url>
    local code=""
    local url=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --code) code="$2"; shift 2 ;;
            --url) url="$2"; shift 2 ;;
            *) shift ;;
        esac
    done

    if [ -z "$code" ] || [ -z "$url" ]; then
        echo "Использование: $0 pair --code <код> --url <url>"
        exit 1
    fi

    local router_id="rt_$(cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "rt_$(date +%s)_$(cat /proc/sys/kernel/random/uuid 2>/dev/null || echo 0)")"
    router_id="${router_id:0:13}"
    local mac=$(cat /sys/class/net/wlan0/address 2>/dev/null || cat /sys/class/net/eth0/address 2>/dev/null || echo "unknown")
    local model=$(cat /tmp/sysinfo/model 2>/dev/null || echo "OpenWrt Router")

    local json_payload='{"code":"'"$code"'","router_id":"'"$router_id"'","mac":"'"$mac"'","name":"'"$model"'","model":"'"$model"'"}'

    local response=$(curl -sS -4 -X POST "$url/pair" \
        -H "Content-Type: application/json" \
        -d "$json_payload")

    echo "Ответ Pi: $response"

    local agent_secret=$(echo "$response" | grep -o '"agent_secret"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*:[^"]*"\([^"]*\)"$/\1/')
    if [ -z "$agent_secret" ]; then
        echo "Ошибка: не удалось получить agent_secret"
        exit 1
    fi

    mkdir -p /etc/netwatch
    cat > "$CONFIG_FILE" << EOF
PI_URL="$url"
ROUTER_ID="$router_id"
AGENT_SECRET="$agent_secret"
EOF
    chmod 600 "$CONFIG_FILE"

    echo "Роутер успешно зарегистрирован!"
    echo "Router ID: $router_id"
    echo "Agent Secret: $agent_secret"
    echo "Конфиг сохранен в $CONFIG_FILE"

    # Авто-запуск агента после pair
    log "Авто-запуск агента..."
    (crontab -l 2>/dev/null | grep -v agent3.sh; echo "*/5 * * * * /etc/netwatch/agent3.sh run") | crontab -
    /etc/init.d/netwatch-wifi-monitor start 2>/dev/null || true
    log "Агент установлен и запущен (health-check каждые 5 мин, wifi-monitor)"
}

# --- run ---
cmd_run() {
    local router_id=$(get_router_id)
    if [ -z "$router_id" ]; then
        log "Роутер не зарегистрирован. Выполните pair."
        exit 1
    fi

    local timestamp=$(date +%s)
    local internet=1
    local wifi_status=1
    local cpu=""
    local ram=""
    local wifi_clients=0
    local podkop_status="unknown"
    local podkop_test=0

    # Проверка интернета
    if ping -c 1 -W 3 8.8.8.8 > /dev/null 2>&1; then
        internet=1
    else
        internet=0
    fi

    # Проверка Wi-Fi (hostapd_cli)
    if hostapd_cli status > /dev/null 2>&1; then
        wifi_status=1
        # Количество подключённых Wi-Fi клиентов
        wifi_clients=$(hostapd_cli all_sta 2>/dev/null | grep -c "SSID=" || echo 0)
    else
        wifi_status=0
    fi

    # CPU
    cpu=$(top -bn1 2>/dev/null | grep "Cpu(s)" | awk '{print $2}' | cut -d'%' -f1)
    [ -z "$cpu" ] && cpu="0"

    # RAM
    ram=$(free -m 2>/dev/null | awk '/Mem:/ { printf "%dMB/%dMB", $3, $2 }')
    [ -z "$ram" ] && ram="0MB/0MB"

    # Состояние Podkop (если установлен)
    if command -v podkop > /dev/null 2>&1; then
        if podkop status > /dev/null 2>&1; then
            podkop_status="running"
        else
            podkop_status="stopped"
        fi
        if podkop test > /dev/null 2>&1; then
            podkop_test=1
        else
            podkop_test=0
        fi
    fi

    # Экранируем строковые значения для JSON
    local cpu_escaped=$(json_escape "$cpu")
    local ram_escaped=$(json_escape "$ram")
    local podkop_status_escaped=$(json_escape "$podkop_status")

    # Сбор устройств для /devices endpoint (DHCP leases + ARP-table, fallback on iw)
    local devices_json="[]"
    if [ -f /tmp/dhcp.leases ]; then
        # DHCP leases: MAC, IP, hostname
        local leases=$(cat /tmp/dhcp.leases 2>/dev/null | awk '{print $2, $3, $4}' | while read mac ip hostname; do
            [ -n "$mac" ] && [ -n "$ip" ] && echo "{\"mac\":\"$mac\",\"ip\":\"$ip\",\"hostname\":\"$hostname\"}"
        done | tr '\n' ',' | sed 's/,$//')
        [ -n "$leases" ] && devices_json="[$leases]"
    fi

    # Fallback: ARP-таблица, если DHCP leases пусты
    if [ "$devices_json" = "[]" ]; then
        local arp_list=$(arp -a 2>/dev/null | awk '{print $2, $3}' | sed 's/[()]//g' | while read ip mac; do
            [ -n "$ip" ] && [ -n "$mac" ] && echo "{\"mac\":\"$mac\",\"ip\":\"$ip\",\"hostname\":\"unknown\"}"
        done | tr '\n' ',' | sed 's/,$//')
        [ -n "$arp_list" ] && devices_json="[$arp_list]"
    fi
    
    # Fallback: iw (Wi-Fi clients only)
    if [ "$devices_json" = "[]" ] && command -v iw > /dev/null 2>&1; then
        local mac_list=$(iw dev wlan0 station dump 2>/dev/null | grep "Station" | awk '{print $2}')
        mac_list="$mac_list $(iw dev wlan1 station dump 2>/dev/null | grep "Station" | awk '{print $2}')"
        
        if [ -n "$mac_list" ]; then
            local dev_json=""
            for mac in $mac_list; do
                mac=$(echo "$mac" | tr '[:upper:]' '[:lower:]')
                local ip=$(ip neigh show | grep -i "$mac" | awk '{print $1}' | head -1)
                [ -z "$ip" ] && ip="unknown"
                # Используем printf для безопасного создания JSON объекта
                local dev_obj=$(printf '{"mac":"%s","ip":"%s","hostname":"%s"}' "$mac" "$ip" "unknown")
                dev_json="${dev_json}${dev_obj},"
            done
            # Убираем последнюю запятую и оборачиваем в массив
            dev_json=$(echo "$dev_json" | sed 's/,$//')
            devices_json="[$dev_json]"
        fi
    fi

    # Отправка устройств на /devices
    curl -sS -4 -X POST "$PI_URL/devices" \
        -H "Content-Type: application/json" \
        -H "X-Agent-Secret: $AGENT_SECRET" \
        -d "{\"router_id\":\"$router_id\",\"devices\":$devices_json,\"timestamp\":$timestamp}" >/dev/null 2>&1
    echo ""

    # Отправка health-check на /health
    curl -sS -4 -X POST "$PI_URL/health" \
        -H "Content-Type: application/json" \
        -H "X-Agent-Secret: $AGENT_SECRET" \
        -d "{\"router_id\":\"$router_id\",\"internet\":$internet,\"wifi\":$wifi_status,\"cpu\":\"$cpu_escaped\",\"ram\":\"$ram_escaped\",\"wifi_clients\":$wifi_clients,\"podkop_status\":\"$podkop_status_escaped\",\"podkop_test\":$podkop_test,\"timestamp\":$timestamp}"
    echo ""
}

# --- wifi-monitor ---
cmd_wifi_monitor() {
    local router_id=$(get_router_id)
    if [ -z "$router_id" ]; then
        log "Роутер не зарегистрирован. Выполните pair."
        exit 1
    fi

    log "Запуск wifi-monitor (hostapd_cli)..."

    # Мониторинг событий Wi-Fi (только беспроводные подключения)
    hostapd_cli -i wlan0 -i wlan1 -a /usr/bin/netwatch-agent-hostapd.sh 2>/dev/null &

    # Пишем PID для возможности остановки
    echo $! > /var/run/netwatch-wifi-monitor.pid
    log "wifi-monitor запущен (PID: $(cat /var/run/netwatch-wifi-monitor.pid))"

    # Ждём
    wait
}

# --- uninstall ---
cmd_uninstall() {
    log "Удаление NetWatch Agent..."
    
    # Остановка сервисов
    /etc/init.d/netwatch-wifi-monitor stop 2>/dev/null || true
    kill $(cat /var/run/netwatch-wifi-monitor.pid 2>/dev/null) 2>/dev/null || true
    rm -f /var/run/netwatch-wifi-monitor.pid
    
    # Удаление cron
    crontab -l 2>/dev/null | grep -v agent3.sh | crontab -
    
    # Удаление конфига и логов
    rm -f /etc/netwatch/config
    rm -f /mnt/usb/log/netwatch/netwatch-agent.log
    rm -rf /etc/netwatch
    
    echo "Агент полностью удалён с роутера"
}

# --- get_router_id из файла ---
get_router_id() {
    if [ -f /etc/netwatch/router_id ]; then
        cat /etc/netwatch/router_id
    elif [ -f /etc/netwatch/config ] && grep -q '^ROUTER_ID=' /etc/netwatch/config; then
        grep '^ROUTER_ID=' /etc/netwatch/config | cut -d'=' -f2- | sed 's/^"//; s/"$//'
    fi
}

# --- Главное меню ---
case "$1" in
    pair)
        shift
        cmd_pair "$@"
        ;;
    run)
        cmd_run
        ;;
    wifi-monitor)
        cmd_wifi_monitor
        ;;
    health-check)
        cmd_run
        ;;
    uninstall)
        cmd_uninstall
        ;;
    *)
        echo "NetWatch Agent v3 (OpenWrt)"
        echo "Использование: $0 {pair|run|wifi-monitor|health-check|uninstall}"
        echo ""
        echo "  pair          -- Регистрация роутера на Pi + авто-установка агента"
        echo "  run           -- Health-check + heartbeat (cron) + отправка устройств"
        echo "  wifi-monitor  -- Мониторинг Wi-Fi в реальном времени (hostapd_cli)"
        echo "  health-check  -- Проверка здоровья роутера (интернет, Wi-Fi, CPU, RAM, клиенты, Podkop)"
        echo "  uninstall     -- Полное удаление агента с роутера"
        ;;
esac
