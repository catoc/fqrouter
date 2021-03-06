#!/usr/bin/env python
import subprocess
import shlex
import time
import sys
import os
import argparse

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
import manager.china_ip

argument_parser = argparse.ArgumentParser()
argument_parser.add_argument('--proxy-list', action='append')
argument_parser.add_argument('--proxy', action='append')
args = argument_parser.parse_args()

PROXY_LIST_DIR = os.path.join(os.path.dirname(__file__), 'proxy-list')
CONCURRENT_CHECKERS_COUNT = 8

def log(message):
    sys.stderr.write(message)
    sys.stderr.write('\n')


proxies = set()
black_list = {
    '198.154.114.118',
    '173.213.113.111',
    '66.35.68.145',
    '91.193.75.101',
    '5.135.81.16',
    '122.38.94.49',
    '95.140.119.40',
    '95.140.123.78',
    '109.195.54.231',
    '95.140.118.193',
    '178.33.105.59',
    '92.46.119.60',
    '201.73.200.82',
    '212.119.97.198',
    '211.138.120.125',
    '202.29.216.236',
    '120.194.22.114',
    '202.29.216.236',
    '120.194.22.114',
    '60.2.227.123'
}


def add_proxy(line):
    line = line.strip()
    if not line:
        return
    try:
        ip, port = line.split(':')
        if manager.china_ip.is_china_ip(ip):
            log('skip china ip: %s' % ip)
        elif ip in black_list:
            log('skip blacklisted ip: %s' % ip)
        else:
            proxies.add((ip, port, 0))
    except:
        log('skip illegal proxy: %s' % line)

if args.proxy:
    for proxy in args.proxy:
        add_proxy(proxy)
if args.proxy_list:
    for command in args.proxy_list:
        try:
            before = len(proxies)
            log('executing %s' % command)
            lines = subprocess.check_output(command, shell=True, cwd=PROXY_LIST_DIR).splitlines(False)
            for line in lines:
                add_proxy(line)
            after = len(proxies)
            log('succeeded, %s new proxies' % (after - before))
        except subprocess.CalledProcessError, e:
            log('failed, output:')
            log(e.output)


class DomesticChecker(object):
    def __init__(self, ip, port, elapsed):
        self.ip = ip
        self.port = port
        self.elapsed = elapsed
        self.proc = subprocess.Popen(
            shlex.split('socksify curl --proxy %s:%s https://www.amazon.com/404' % (ip, port)),
            stderr=subprocess.STDOUT, stdout=subprocess.PIPE)
        self.started_at = time.time()

    def is_ok(self):
        if 0 == self.proc.poll() and 'Amazon.com' in self.proc.stdout.read():
            return round(time.time() - self.started_at, 2)
        return 0

    def is_failed(self):
        return self.proc.poll()

    def is_timed_out(self):
        return time.time() - self.started_at > 10

    def kill(self):
        self.proc.kill()


class InternationalChecker(object):
    def __init__(self, ip, port, elapsed):
        self.ip = ip
        self.port = port
        self.elapsed = elapsed
        self.proc = subprocess.Popen(
            shlex.split('curl --proxy %s:%s https://mobile.twitter.com/signup' % (ip, port)),
            stderr=subprocess.STDOUT, stdout=subprocess.PIPE)
        self.started_at = time.time()

    def is_ok(self):
        if 0 == self.proc.poll() and 'Welcome to Twitter' in self.proc.stdout.read():
            return round(time.time() - self.started_at, 2)
        return 0

    def is_failed(self):
        return self.proc.poll()

    def is_timed_out(self):
        return time.time() - self.started_at > 10

    def kill(self):
        self.proc.kill()


checkers = []
checked_proxies = []
for i in range(10):
    log('PASS %s' % (i + 1))
    while len(proxies) + len(checkers):
        for checker in list(checkers):
            try:
                ok = checker.is_ok()
                if ok:
                    log('OK[%s] %s:%s' % (ok, checker.ip, checker.port))
                    checked_proxies.append((checker.ip, checker.port, checker.elapsed + ok))
                    checkers.remove(checker)
                elif checker.is_failed():
                    log('FAILED %s:%s' % (checker.ip, checker.port))
                    checkers.remove(checker)
                elif checker.is_timed_out():
                    log('TIMEOUT %s:%s' % (checker.ip, checker.port))
                    checkers.remove(checker)
                    try:
                        checker.kill()
                    except:
                        pass
            except:
                log('FATAL %s:%s' % (checker.ip, checker.port))
                checkers.remove(checker)
        new_checkers_count = CONCURRENT_CHECKERS_COUNT - len(checkers)
        for i in range(new_checkers_count):
            if proxies:
                ip, port, elapsed = proxies.pop()
                if i % 2 == 0:
                    checkers.append(DomesticChecker(ip, port, elapsed))
                else:
                    checkers.append(InternationalChecker(ip, port, elapsed))
        time.sleep(0.2)
    proxies = checked_proxies
    checked_proxies = []

for ip, port, elapsed in sorted(proxies, key=lambda proxy: proxy[2])[:20]:
    print('%s:%s' % (ip, port))
print('')
