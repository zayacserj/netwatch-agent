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
    # Copy this script to /etc/netwatch/ for cron
    cp "$0" /etc/netwatch/agent3.sh
    chmod +x /etc/netwatch/agent3.sh

    # Авто-установка cron
    cmd_install_cron

    # Авто-запуск агента после pair
    log "Авто-запуск агента..."
    # Первый запуск сразу
    /etc/netwatch/agent3.sh run
    /etc/init.d/netwatch-wifi-monitor start 2>/dev/null || true
    log "Агент установлен и запущен (snapshot каждую минуту, health-check каждые 5 мин, wifi-monitor)"
}

# --- snapshot ---
cmd_snapshot() {
    local router_id=$(get_router_id)
    if [ -z "$router_id" ]; then
        log "Роутер не зарегистрирован. Выполните pair."
        exit 1
    fi

    log "Начинаем сбор данных устройств..."

    # Сбор Wi-Fi клиентов
    local sta_list=$(hostapd_cli all_sta 2>/dev/null)
    if [ -z "$sta_list" ]; then
        log "Нет подключённых Wi-Fi клиентов"
        return
    fi

    # Парсим MAC-адреса из hostapd_cli all_sta
    local macs=$(echo "$sta_list" | awk '/^[0-9A-Fa-f:]{17,} / {print $1}')

    local devices_json="[]"
    local separator=""

    for mac in $macs; do
        local ip=""
        local hostname=""

        # Пытаемся получить IP/hostname из /tmp/dhcp.leases
        if [ -f "/tmp/dhcp.leases" ]; then
            local lease=$(grep -i "$mac" "/tmp/dhcp.leases" 2>/dev/null | head -1)
            if [ -n "$lease" ]; then
                ip=$(echo "$lease" | cut -f 3 -s -d" ")
                hostname=$(echo "$lease" | cut -f 4 -s -d" ")
                [ -z "$ip" ] && ip="unknown"
                [ -z "$hostname" ] && hostname="unknown"
            fi
        fi

        # Fallback: ARP table
        if [ "$ip" = "unknown" ]; then
            local arp_ip=$(arp -n 2>/dev/null | grep -i "$mac" | awk '{print $1}' | head -1)
            if [ -n "$arp_ip" ]; then
                ip="$arp_ip"
            fi
        fi

        # Формируем устройство JSON
        local dev_obj=$(printf '{"mac":"%s","ip":"%s","hostname":"%s"}' "$mac" "$ip" "$hostname")
        devices_json=$(echo "$devices_json" | jq --argjson dev "$dev_obj" '. + [$dev]' 2>/dev/null || echo "$devices_json")
        # fallback если jq нет
        if [ $? -ne 0 ]; then
            devices_json=$(echo "$devices_json" | sed "s|]\\$|,$dev_obj\\]|" 2>/dev/null || echo "$devices_json")
        fi
    done

    if [ "$devices_json" = "[]" ]; then
        log "Нет клиентов для отправки"
        return
    fi

    local timestamp=$(date +%s)
    log "Отправляем $devices_json на $PI_URL/devices"
    local response=$(curl -sS -4 -X POST "$PI_URL/devices" \
        -H "Content-Type: application/json" \
        -H "X-Agent-Secret: $AGENT_SECRET" \
        -d "{\"router_id\":\"$router_id\",\"devices\":$devices_json,\"timestamp\":$timestamp}")

    echo "Ответ Pi: $response"
}

# --- install-cron ---
cmd_install_cron() {
    log "Установка cron-задач для NetWatch Agent"

    local crontab_content=$(crontab -l 2>/dev/null || echo "")

    # Добавляем задачу snapshot раз в минуту
    local snapshot_cron="* * * * * $0 snapshot >> /mnt/usb/log/netwatch/snapshot.log 2>&1"
    if [[ "$crontab_content" != *"$0 snapshot"* ]]; then
        echo "$crontab_content" | crontab -
        echo "$snapshot_cron" | crontab -
        log "Добавлена cron-задача: $snapshot_cron"
    else
        log "Cron-задача уже существует, пропускаем"
    fi

    # Добавляем задачу run каждые 5 минут
    local run_cron="*/5 * * * * $0 run >> /mnt/usb/log/netwatch/run.log 2>&1"
    if [[ "$crontab_content" != *"$0 run"* ]]; then
        echo "$crontab_content" | crontab -
        echo "$run_cron" | crontab -
        log "Добавлена cron-задача: $run_cron"
    else
        log "Cron-задача run уже существует, пропускаем"
    fi

    log "Cron-задачи установлены"
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

    # Проверка Wi-Fi (hostapd_cli) — проверяем все Wi-Fi интерфейсы
    local wifi_interface_found=0
    local wifi_clients_total=0
    if command -v iwinfo > /dev/null 2>&1; then
        local iface_list=$(iwinfo 2>/dev/null | grep ESSID | cut -f 1 -s -d" ")
        for iface in $iface_list; do
            if hostapd_cli -i "$iface" status > /dev/null 2>&1; then
                wifi_interface_found=1
                local iface_clients=$(hostapd_cli -i "$iface" all_sta 2>/dev/null | grep -c "SSID=" || echo 0)
                wifi_clients_total=$((wifi_clients_total + iface_clients))
            fi
        done
    fi
    if [ "$wifi_interface_found" = "1" ]; then
        wifi_status=1
        wifi_clients=$wifi_clients_total
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

    # Сбор устройств для /devices endpoint (Associated Stations via iwinfo + DHCP leases correlation)
    # iwinfo — универсальный интерфейс OpenWrt, работает со всеми драйверами (mac80211, Broadcom wl и т.д.)
    local devices_json="[]"
    local all_devices=""

    if command -v iwinfo > /dev/null 2>&1; then
        # Список всех беспроводных интерфейсов
        local iface_list=$(iwinfo 2>/dev/null | grep ESSID | cut -f 1 -s -d" ")
        
        for iface in $iface_list; do
            # assoclist показывает ТОЛЬКО ассоциированные (подключенные сейчас) станции с RSSI
            local mac_list=$(iwinfo "$iface" assoclist 2>/dev/null | grep dBm | cut -f 1 -s -d" ")
            
            for mac in $mac_list; do
                mac=$(echo "$mac" | tr '[:upper:]' '[:lower:]')
                
                # Корреляция с DHCP leases для получения IP и hostname
                local ip="unknown"
                local host="unknown"
                if [ -f /tmp/dhcp.leases ]; then
                    local lease=$(grep -i "$mac" /tmp/dhcp.leases 2>/dev/null | head -1)
                    if [ -n "$lease" ]; then
                        ip=$(echo "$lease" | cut -f 3 -s -d" ")
                        host=$(echo "$lease" | cut -f 4 -s -d" ")
                        [ -z "$ip" ] && ip="unknown"
                        [ -z "$host" ] && host="unknown"
                    fi
                fi

                # Fallback для репитеров без DHCP: пытаемся взять IP из ARP-таблицы
                if [ "$ip" = "unknown" ]; then
                    local arp_ip=""
                    arp_ip=$(arp -n 2>/dev/null | grep -i "$mac" | awk '{print $1}' | head -1)
                    [ -z "$arp_ip" ] && arp_ip=$(ip neigh 2>/dev/null | grep -i "$mac" | awk '{print $1}' | head -1)
                    [ -n "$arp_ip" ] && ip="$arp_ip"
                fi
                
                local dev_obj=$(printf '{"mac":"%s","ip":"%s","hostname":"%s"}' "$mac" "$ip" "$host")
                all_devices="${all_devices}${dev_obj},"
            done
        done
    fi

    # Убираем последнюю запятую и формируем JSON
    all_devices=$(echo "$all_devices" | sed 's/,$//')
    [ -n "$all_devices" ] && devices_json="[$all_devices]"

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
    snapshot)
        cmd_snapshot
        ;;
    install-cron)
        cmd_install_cron
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
        echo "Использование: $0 {pair|run|snapshot|install-cron|wifi-monitor|health-check|uninstall}"
        echo ""
        echo "  pair          -- Регистрация роутера на Pi + авто-установка агента"
        echo "  run           -- Health-check + heartbeat (cron) + отправка устройств"
        echo "  snapshot      -- Сбор данных устройств (Wi-Fi клиенты) (cron)"
        echo "  install-cron  -- Установка cron-задач (snapshot + run)"
        echo "  wifi-monitor  -- Мониторинг Wi-Fi в реальном времени (hostapd_cli)"
        echo "  health-check  -- Проверка здоровья роутера (интернет, Wi-Fi, CPU, RAM, клиенты, Podkop)"
        echo "  uninstall     -- Полное удаление агента с роутера"
        ;;
esac