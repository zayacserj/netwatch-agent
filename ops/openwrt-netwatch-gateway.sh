#!/bin/sh
# Prepare a primary OpenWrt gateway for a NetWatch Raspberry Pi.
set -eu

fail() { printf 'STOP: %s\n' "$*" >&2; exit 1; }
get() { uci -q get "$1" || :; }
sections() { uci show "$1" | awk -F= -v kind="$2" '$2 == kind { print $1 }'; }

mode=plan
if [ "${1:-}" = "--apply" ]; then
    mode=apply
    shift
fi
[ "$#" -eq 3 ] || fail "Usage: sh $0 [--apply] DOMAIN PI_IPV4 PI_MAC"
domain=$(printf '%s' "$1" | tr 'A-Z' 'a-z')
pi_ip=$2
pi_mac=$(printf '%s' "$3" | tr 'A-F' 'a-f')

[ -r /etc/openwrt_release ] || fail "This command is for OpenWrt."
for tool in uci awk grep tr fw4; do
    command -v "$tool" >/dev/null 2>&1 || fail "Missing command: $tool"
done
[ "${#domain}" -le 253 ] || fail "Invalid domain."
case "$domain" in *[!a-z0-9.-]*) fail "Invalid domain." ;; esac
case "$pi_mac" in *[!0-9a-f:]*) fail "Invalid Pi MAC." ;; esac
printf '%s\n' "$domain" | grep -Eq '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$' ||
    fail "Invalid domain."
printf '%s\n' "$pi_mac" | grep -Eq '^[0-9a-f]{2}(:[0-9a-f]{2}){5}$' || fail "Invalid Pi MAC."
valid_ipv4() {
    printf '%s\n' "$1" | awk -F. '
        NF == 4 {
            for (i=1; i<=4; i++)
                if ($i !~ /^(0|[1-9][0-9]?[0-9]?)$/ || $i > 255) exit 1
            valid=1
        }
        END { if (!valid || NR != 1) exit 1 }
    '
}
valid_ipv4 "$pi_ip" || fail "Invalid Pi IPv4 address."

for package in network dhcp firewall; do
    [ -z "$(uci changes "$package")" ] || fail "Pending UCI changes in $package."
done
[ "$(get network.lan.proto)" = static ] || fail "Configure the static LAN interface first."
lan=$(get network.lan.ipaddr)
case "$lan" in
    */24) lan=${lan%/24} ;;
    */*) fail "This profile expects a /24 LAN." ;;
    *) [ "$(get network.lan.netmask)" = 255.255.255.0 ] || fail "This profile expects a /24 LAN." ;;
esac
valid_ipv4 "$lan" || fail "Invalid LAN address."
[ "${pi_ip%.*}" = "${lan%.*}" ] && [ "$pi_ip" != "$lan" ] ||
    fail "Pi must have a different address in the same /24 LAN."
case "${pi_ip##*.}" in 0|255) fail "Pi address must be usable by a host." ;; esac
[ "$(get dhcp.lan)" = dhcp ] && [ "$(get dhcp.lan.ignore)" != 1 ] ||
    fail "Enable DHCP on the LAN interface first."
for name in wan lan; do
    found=0
    for zone in $(sections firewall zone); do
        if [ "$(get "$zone.name")" = "$name" ]; then
            case " $(get "$zone.network") " in *" $name "*) found=1 ;; esac
        fi
    done
    [ "$found" = 1 ] || fail "Firewall zone $name must contain interface $name."
done

need_dns=1
for section in $(sections dhcp domain); do
    [ "$(get "$section.name" | tr 'A-Z' 'a-z')" = "$domain" ] || continue
    [ "$(get "$section.ip")" = "$pi_ip" ] || fail "The domain already points to a different address."
    need_dns=0
done

need_host=1
for section in $(sections dhcp host); do
    host_ip=$(get "$section.ip")
    case " $(get "$section.mac" | tr 'A-F' 'a-f') " in
        *" $pi_mac "*)
            [ "$host_ip" = "$pi_ip" ] || fail "Pi MAC already has a different DHCP reservation."
            need_host=0
            ;;
        *) [ "$host_ip" != "$pi_ip" ] || fail "Pi address is reserved for another client." ;;
    esac
done
if [ -r /tmp/dhcp.leases ]; then
    awk -v ip="$pi_ip" -v mac="$pi_mac" '
        $3 == ip && tolower($2) != mac && ($1 == 0 || $1 > systime()) { exit 1 }
    ' /tmp/dhcp.leases || fail "Pi address is leased to another client."
fi

needs_forward() {
    wanted=$1
    needed=1
    for section in $(sections firewall redirect); do
        [ "$(get "$section.enabled")" != 0 ] || continue
        [ "$(get "$section.src")" = wan ] || continue
        [ "$(get "$section.family")" != ipv6 ] || continue
        proto=$(get "$section.proto")
        case " $proto " in
            '  '|*' tcp '*|*' all '*) ;;
            *) continue ;;
        esac
        target=$(get "$section.target")
        [ -z "$target" ] || [ "$target" = DNAT ] || continue
        ports=$(get "$section.src_dport")
        # Treat wildcard, negated, named and unknown port syntax conservatively.
        if printf '%s\n' "$ports" | awk -v wanted="$wanted" '
            NF == 0 { overlap=1 }
            {
                for (i=1; i<=NF; i++) {
                    if ($i ~ /^[0-9]+$/) {
                        if ($i == wanted) overlap=1
                    } else if ($i ~ /^[0-9]+[-:][0-9]+$/) {
                        split($i, ends, /[-:]/)
                        if (wanted >= ends[1] && wanted <= ends[2]) overlap=1
                    } else overlap=1
                }
            }
            END { exit !overlap }
        '; then
            [ "$ports" = "$wanted" ] && [ "$proto" = tcp ] &&
            [ "$(get "$section.dest")" = lan ] &&
            [ "$(get "$section.dest_ip")" = "$pi_ip" ] &&
            [ "$(get "$section.dest_port")" = "$wanted" ] &&
            [ -z "$(get "$section.src_ip")" ] &&
            [ -z "$(get "$section.src_dip")" ] &&
            [ -z "$(get "$section.src_mac")" ] &&
            [ -z "$(get "$section.src_port")" ] ||
                fail "Conflicting WAN TCP $wanted redirect: $section."
            needed=0
        fi
    done
    printf '%s\n' "$needed"
}

need_http=$(needs_forward 80) || exit 1
need_https=$(needs_forward 443) || exit 1
for item in "$need_dns:dhcp.netwatch_pi" "$need_host:dhcp.netwatch_host" \
            "$need_http:firewall.netwatch_http" "$need_https:firewall.netwatch_https"; do
    if [ "${item%%:*}" = 1 ] && [ -n "$(get "${item#*:}")" ]; then
        fail "The reserved UCI section ${item#*:} is already in use."
    fi
done

printf 'LAN: %s/24\nPi: %s\nDNS: %s → %s (add: %s)\nDHCP reservation (add: %s)\n' \
    "$lan" "$pi_ip" "$domain" "$pi_ip" "$need_dns" "$need_host"
printf 'WAN TCP 80 → Pi:80 (add: %s)\nWAN TCP 443 → Pi:443 (add: %s)\n' "$need_http" "$need_https"
[ "$mode" = apply ] || exit 0
[ "$(id -u)" = 0 ] || fail "Run --apply as root."
[ "$need_dns$need_host$need_http$need_https" != 0000 ] || exit 0

umask 077
backup=$(mktemp -d /etc/netwatch-gateway-before.XXXXXX)
cp -p /etc/config/dhcp /etc/config/firewall "$backup/"
printf 'Backup: %s\n' "$backup"

rollback() {
    uci revert dhcp >/dev/null 2>&1 || :
    uci revert firewall >/dev/null 2>&1 || :
    cp -p "$backup/dhcp" "$backup/firewall" /etc/config/
    /etc/init.d/firewall reload || :
    /etc/init.d/dnsmasq reload || :
    printf 'Previous DHCP and firewall configuration restored.\n' >&2
}
trap rollback EXIT
trap 'exit 1' HUP INT TERM

if [ "$need_dns" = 1 ]; then
    uci set dhcp.netwatch_pi=domain
    uci set "dhcp.netwatch_pi.name=$domain"
    uci set "dhcp.netwatch_pi.ip=$pi_ip"
fi
if [ "$need_host" = 1 ]; then
    uci set dhcp.netwatch_host=host
    uci set dhcp.netwatch_host.name=netwatch-pi
    uci set "dhcp.netwatch_host.mac=$pi_mac"
    uci set "dhcp.netwatch_host.ip=$pi_ip"
fi

add_forward() {
    prefix=$1 port=$2
    uci set "$prefix=redirect"
    uci set "$prefix.name=NetWatch TCP $port"
    uci set "$prefix.src=wan"
    uci set "$prefix.dest=lan"
    uci set "$prefix.proto=tcp"
    uci set "$prefix.family=ipv4"
    uci set "$prefix.src_dport=$port"
    uci set "$prefix.dest_ip=$pi_ip"
    uci set "$prefix.dest_port=$port"
    uci set "$prefix.target=DNAT"
}
if [ "$need_http" = 1 ]; then add_forward firewall.netwatch_http 80; fi
if [ "$need_https" = 1 ]; then add_forward firewall.netwatch_https 443; fi
uci commit dhcp
uci commit firewall
fw4 check
/etc/init.d/firewall reload
/etc/init.d/dnsmasq reload
trap - EXIT HUP INT TERM
printf 'NetWatch gateway configuration applied. Test DNS and HTTPS.\n'
