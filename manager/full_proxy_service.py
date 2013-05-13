import logging
from netfilterqueue import NetfilterQueue
import socket
import time
import urllib2
import threading
import thread
import struct
import subprocess

import dpkt
from pynetfilter_conntrack.conntrack import Conntrack
from pynetfilter_conntrack.conntrack_entry import ConntrackEntry
from pynetfilter_conntrack.constant import NFCT_SOPT_SETUP_REPLY
from pynetfilter_conntrack.constant import IPS_DST_NAT
from pynetfilter_conntrack.constant import IPS_DST_NAT_DONE
import lan_ip

import shutdown_hook
import iptables
import china_ip
import redsocks_monitor
import goagent_monitor
import dns_service
import wifi
import shell


LOGGER = logging.getLogger('fqrouter.%s' % __name__)
APPIDS_COUNT = 10


def run():
    try:
        insert_iptables_rules()
        thread.start_new(start_full_proxy, ())
    except:
        LOGGER.exception('failed to start full proxy service')


def status():
    return 'N/A'


def clean():
    global enabled
    enabled = False
    delete_iptables_rules()
    redsocks_monitor.kill_redsocks()
    goagent_monitor.kill_goagent()


#=== private ===

raw_socket = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
shutdown_hook.add(raw_socket.close)
raw_socket.setsockopt(socket.SOL_IP, socket.IP_HDRINCL, 1)
SO_MARK = 36
raw_socket.setsockopt(socket.SOL_SOCKET, SO_MARK, 0xcafe)

PROXIES_COUNT = 20
white_list = set()
black_list = set()
pending_list = {} # ip => started_at
proxies = {} # local_port => proxy
proxies_refreshed_at = 0
enabled = True
previously_resolved_results = {
    'appids': [],
    'free_proxies': []
}

RULES = [
    (
        # somehow 10.1.2.3 sends "un-nated" packet to destination, results in RST back
        # drop them is a hack, but works
        {'target': 'DROP', 'extra': 'tcpflags: 0x3F/0x04', 'destination': '10.1.2.3'},
        ('filter', 'INPUT', '-d 10.1.2.3 -p tcp --tcp-flags ALL RST -j DROP')
    ), (
        {'target': 'NFQUEUE', 'extra': 'NFQUEUE num 3'},
        ('nat', 'OUTPUT', '-p tcp -j NFQUEUE --queue-num 3')
    ), (
        {'target': 'NFQUEUE', 'extra': 'NFQUEUE num 3'},
        ('nat', 'PREROUTING', '-p tcp -j NFQUEUE --queue-num 3')
    )]


def insert_iptables_rules():
    shutdown_hook.add(delete_iptables_rules)
    iptables.insert_rules(RULES)


def delete_iptables_rules():
    iptables.delete_rules(RULES)


def start_full_proxy():
    try:
        shutdown_hook.add(redsocks_monitor.kill_redsocks)
        thread.start_new(refresh_proxies, ())
    except:
        LOGGER.exception('failed to start keep proxies fresh thread')
        proxies.clear()
    handle_nfqueue()


def refresh_proxies():
    if redsocks_monitor.is_redsocks_live():
        LOGGER.error('refresh proxies need to stop redsocks first')
        return
    proxies.clear()
    redsocks_monitor.kill_redsocks()
    appids = resolve_appids()
    if appids:
        try:
            start_goagent(appids)
        except:
            LOGGER.exception('failed to start goagent')
            goagent_monitor.kill_goagent()
    else:
        LOGGER.info('no appids resolved, do not start goagent')
    LOGGER.info('resolving free proxies')
    for i, connection_info in enumerate(resolve_free_proxies()):
        add_free_proxy(19830 + i, connection_info)
    LOGGER.info('starting redsocks')
    if not start_redsocks():
        LOGGER.error('clear proxies, due to redsocks failed to start')
        proxies.clear()
        return False
    if proxies:
        LOGGER.info('checking twitter access')
        LOGGER.info('twitter access success rate: %s' % shell.fqrouter_execute('twitter-check'))
    else:
        LOGGER.info('still no proxies after redsocks started, retry in 120 seconds')
        time.sleep(120)
        return refresh_proxies()
    return True


def resolve_appids():
    appids = []
    for i in range(6):
        try:
            domain_names = ['goagent%s.fqrouter.com' % i for i in range(1, 1 + APPIDS_COUNT)]
            answers = dns_service.resolve('TXT', domain_names)
            for appid in answers.values():
                appid = appid[0] if appid else ''
                appid = ''.join(e for e in appid if e.isalnum())
                if appid:
                    appids.append(appid)
            if answers and len(appids) >= (len(previously_resolved_results['appids']) / 2):
                previously_resolved_results['appids'] = appids
                return appids
        except:
            LOGGER.exception('failed to resolve appids once')
        LOGGER.info('retry resolving appids in 10 seconds')
        time.sleep(10)
    LOGGER.error('resolve appids failed, too many retries, give up')
    return previously_resolved_results['appids']


def start_goagent(appids):
    goagent_monitor.kill_goagent()
    goagent_monitor.on_goagent_died = on_goagent_died
    goagent_monitor.start_goagent(appids)
    proxies[19830 + PROXIES_COUNT + 1] = {
        'clients': set(),
        'rank': 0, # lower is better
        'pre_rank': 0, # lower is better
        'error_penalty': 256, # if error is found, this will be added to rank
        'connection_info': ('http-relay', '127.0.0.1', '8319', '', '')
    }


def on_goagent_died():
    LOGGER.info('goagent died')
    local_port = 19830 + PROXIES_COUNT + 1
    if local_port in proxies:
        del proxies[local_port]


def resolve_free_proxies():
    proxy_domain_names = {}
    for i in range(1, 1 + PROXIES_COUNT):
        proxy_domain_names[19830 + i] = 'proxy%s.fqrouter.com' % i
    for i in range(6):
        try:
            answers = dns_service.resolve('TXT', proxy_domain_names.values())
            if answers:
                connection_infos = []
                for connection_info in answers.values():
                    connection_info = connection_info[0] if connection_info else ''
                    if not connection_info:
                        continue
                    connection_info = ''.join(e for e in connection_info if e.isalnum() or e in [':', '.', '-'])
                    connection_info = connection_info.split(':') # proxy_type:ip:port:username:password
                    connection_infos.append(connection_info)
                if len(connection_infos) > (len(previously_resolved_results['free_proxies']) / 2):
                    previously_resolved_results['free_proxies'] = connection_infos
                    return connection_infos
                else:
                    continue
        except:
            LOGGER.exception('failed to resolve free proxies once')
        LOGGER.info('retry resolving free proxies in 10 seconds')
        time.sleep(10)
    LOGGER.error('resolve free proxies failed, too many retries, give up')
    return previously_resolved_results['free_proxies']


def add_free_proxy(local_port, connection_info):
    proxies[local_port] = {
        'clients': set(),
        'rank': 0, # lower is better
        'pre_rank': 0, # lower is better
        'error_penalty': 256, # if error is found, this will be added to rank
        'connection_info': connection_info
    }
    LOGGER.info('add free proxy %s: %s' % (local_port, proxies[local_port]))


def start_redsocks():
    redsocks_monitor.list_proxies = proxies.items
    redsocks_monitor.handle_proxy_error = handle_proxy_error
    redsocks_monitor.update_proxy = update_proxy
    redsocks_monitor.refresh_proxies = refresh_proxies
    wifi.on_wifi_hotspot_started = redsocks_monitor.kill_redsocks
    return redsocks_monitor.start_redsocks(proxies)


def update_proxy(local_port, **kwargs):
    if local_port in proxies:
        proxies[local_port].update(kwargs)


def handle_proxy_error(local_port, proxy):
    error_penalty = proxy['error_penalty']
    if error_penalty > 256 * 2 * 2 * 2:
        LOGGER.error('proxy %s purged due to too many errors: %s' % (local_port, str(proxy['connection_info'])))
        del proxies[local_port]
    else:
        LOGGER.error('add error penalty to proxy %s: %s to %s' %
                     (local_port, error_penalty, str(proxy['connection_info'])))
        proxy['rank'] += error_penalty
        proxy['pre_rank'] += error_penalty
        proxy['error_penalty'] *= 2


def handle_nfqueue():
    try:
        nfqueue = NetfilterQueue()
        nfqueue.bind(3, handle_packet)
        nfqueue.run()
    except:
        LOGGER.exception('stopped handling nfqueue')
    finally:
        LOGGER.info('full proxy service stopped')


def handle_packet(nfqueue_element):
    try:
        if 0xcafe == nfqueue_element.get_mark():
            nfqueue_element.accept()
            return
        ip_packet = dpkt.ip.IP(nfqueue_element.get_payload())
        if ip_packet.tcp.dport not in {80, 443}:
            nfqueue_element.accept()
            return
        if lan_ip.is_lan_traffic(ip_packet):
            nfqueue_element.accept()
            return
        ip = socket.inet_ntoa(ip_packet.dst)
        if china_ip.is_china_ip(ip):
            nfqueue_element.accept()
        elif ip in black_list:
            set_verdict_proxy(nfqueue_element, ip_packet)
        elif ip in white_list:
            nfqueue_element.accept()
        else:
            nfqueue_element.accept()
    except:
        LOGGER.exception('failed to handle packet')
        nfqueue_element.accept()


def set_verdict_proxy(nfqueue_element, ip_packet):
    local_port = pick_proxy(ip_packet)
    if local_port:
        src = socket.inet_ntoa(ip_packet.src)
        dst = socket.inet_ntoa(ip_packet.dst)
        create_conntrack_entry(src, ip_packet.tcp.sport, dst, ip_packet.tcp.dport, local_port)
        raw_socket.sendto(str(ip_packet), (dst, 0))
        nfqueue_element.drop()
    else:
        nfqueue_element.accept()


def pick_proxy(ip_packet):
    if not proxies:
        return None
    local_ports = {}
    for local_port, proxy in proxies.items():
        if 'http-relay' == proxy['connection_info'][0]:
            if 80 == ip_packet.tcp.dport:
                local_ports[-1] = local_port
            continue
        local_ports[proxy['rank']] = local_port
    if not local_ports:
        return None
    local_port = local_ports[sorted(local_ports.keys())[0]]
    ip = socket.inet_ntoa(ip_packet.src)
    port = ip_packet.tcp.sport
    proxy = proxies[local_port]
    proxy['rank'] += 1
    proxy['clients'].add((ip, port))
    LOGGER.info('full proxy via %s [%s] %s: %s:%s => %s:%s' % (
        local_port, proxy['rank'], str(proxy['connection_info']),
        ip, port, socket.inet_ntoa(ip_packet.dst), ip_packet.tcp.dport))
    return local_port


def add_to_black_list(ip, syn=None):
    if ip not in black_list and ip not in white_list:
        for local_port, proxy in proxies.items():
            if ip == proxy['connection_info'][1]:
                LOGGER.error('proxy %s died: %s' % (local_port, ip))
                del proxies[local_port]
                return
        LOGGER.info('add black list ip: %s' % ip)
        black_list.add(ip)
        if syn and delete_existing_conntrack_entry(ip):
            raw_socket.sendto(str(syn), (socket.inet_ntoa(syn.dst), 0))
    pending_list.pop(ip, None)


def create_conntrack_entry(src, sport, dst, dport, local_port):
    # delete_existing_conntrack_entry(dst)
    conntrack = Conntrack()
    try:
        conntrack_entry = ConntrackEntry.new(conntrack)
        try:
            conntrack_entry.orig_l3proto = socket.AF_INET
            conntrack_entry.orig_l4proto = socket.IPPROTO_TCP
            conntrack_entry.orig_ipv4_src = struct.unpack('!I', socket.inet_aton(src))[0]
            conntrack_entry.orig_ipv4_dst = struct.unpack('!I', socket.inet_aton(dst))[0]
            conntrack_entry.orig_port_src = sport
            conntrack_entry.orig_port_dst = dport
            conntrack_entry.setobjopt(NFCT_SOPT_SETUP_REPLY)
            conntrack_entry.repl_ipv4_src = struct.unpack('!I', socket.inet_aton(dst))[0]
            conntrack_entry.repl_ipv4_dst = struct.unpack('!I', socket.inet_aton(src))[0]
            conntrack_entry.repl_port_src = dport
            conntrack_entry.repl_port_dst = sport
            conntrack_entry.dnat_ipv4 = struct.unpack('!I', socket.inet_aton('10.1.2.3'))[0]
            conntrack_entry.dnat_port = local_port
            conntrack_entry.snat_ipv4 = struct.unpack('!I', socket.inet_aton(src))[0]
            conntrack_entry.snat_port = sport
            conntrack_entry.status = IPS_DST_NAT | IPS_DST_NAT_DONE
            conntrack_entry.timeout = 120
            try:
                conntrack_entry.create()
            except:
                LOGGER.exception('failed to create nat conntrack entry for %s:%s => %s:%s' % (src, sport, dst, dport))
        finally:
            del conntrack_entry
    finally:
        del conntrack


def delete_existing_conntrack_entry(ip):
    try:
        LOGGER.info('delete existing conntrack entry for: %s' % ip)
        output = subprocess.check_output(
            ['/data/data/fq.router/proxy-tools/conntrack', '-D', '-p', 'tcp', '--reply-src', ip],
            stderr=subprocess.STDOUT).strip()
        LOGGER.info('succeed: %s' % output)
    except subprocess.CalledProcessError, e:
        LOGGER.warn('failed: %s' % e.output)
        LOGGER.warn('failed to delete existing conntrack entry %s' % ip)


def add_to_white_list(ip):
    if ip not in white_list and not china_ip.is_china_ip(ip):
        LOGGER.info('add white list ip: %s' % ip)
        white_list.add(ip)
    pending_list.pop(ip, None)