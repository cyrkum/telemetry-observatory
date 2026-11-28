from types import SimpleNamespace
import plistlib
import subprocess

import host_sensor
import pytest


def test_collect_skips_inaccessible_volume_and_process(monkeypatch):
    def disk_usage(path):
        if path == '/private':
            raise PermissionError()
        return SimpleNamespace(total=100, used=40, free=60, percent=40)

    class BadProcess:
        @property
        def info(self):
            raise host_sensor.psutil.AccessDenied()

    monkeypatch.setattr(host_sensor.psutil, 'disk_partitions', lambda all=False: [SimpleNamespace(mountpoint='/', device='disk1', fstype='apfs'), SimpleNamespace(mountpoint='/private', device='disk2', fstype='apfs')])
    monkeypatch.setattr(host_sensor.psutil, 'disk_usage', disk_usage)
    monkeypatch.setattr(host_sensor.psutil, 'process_iter', lambda fields: [BadProcess(), SimpleNamespace(info={'pid': 7, 'name': 'App', 'memory_info': SimpleNamespace(rss=1024), 'cpu_percent': 2})])
    monkeypatch.setattr(host_sensor.psutil, 'cpu_percent', lambda interval: 4)
    monkeypatch.setattr(host_sensor.psutil, 'virtual_memory', lambda: SimpleNamespace(_asdict=lambda: {'total': 100, 'used': 40, 'percent': 40}))
    monkeypatch.setattr(host_sensor.psutil, 'swap_memory', lambda: None)
    monkeypatch.setattr(host_sensor.psutil, 'disk_io_counters', lambda: None)
    monkeypatch.setattr(host_sensor.psutil, 'net_io_counters', lambda: None)
    monkeypatch.setattr(host_sensor.psutil, 'sensors_battery', lambda: None)
    monkeypatch.setattr(host_sensor, 'gpu_percent', lambda: 37)
    monkeypatch.setattr(host_sensor, 'local_ai', lambda apps: {'ollama': {'reachable': False, 'version': None, 'models': []}, 'processes': [], 'tokens': None})
    result = host_sensor.collect()
    assert result['disks'][0]['mount'] == '/'
    assert len(result['disks']) == 1
    assert result['applications'][0]['pid'] == 7
    assert result['gpu_percent'] == 37
    assert result['local_ai']['ollama']['reachable'] is False


@pytest.mark.parametrize('value', [0, 37, 100])
def test_gpu_percent_reads_ioreg(monkeypatch, value):
    def run(command, **kwargs):
        assert command == ['/usr/sbin/ioreg', '-r', '-c', 'IOAccelerator', '-d', '1', '-a']
        assert kwargs == {'capture_output': True, 'check': True, 'timeout': 5}
        return SimpleNamespace(stdout=plistlib.dumps([{}, {'PerformanceStatistics': {'Device Utilization %': value}}]))

    monkeypatch.setattr(host_sensor.subprocess, 'run', run)
    assert host_sensor.gpu_percent() == value


@pytest.mark.parametrize('value', [True, -1, 101, '42', None])
def test_gpu_percent_unavailable_is_audited(monkeypatch, value):
    stats = {} if value is None else {'Device Utilization %': value}
    monkeypatch.setattr(host_sensor.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=plistlib.dumps([{'PerformanceStatistics': stats}])))
    events = []
    monkeypatch.setattr(host_sensor, 'audit', lambda *args: events.append(args))
    assert host_sensor.gpu_percent() is None
    assert events[0][:2] == ('gpu.read', 'error')


@pytest.mark.parametrize('failure', [subprocess.TimeoutExpired('ioreg', 5), subprocess.CalledProcessError(1, 'ioreg'), OSError('missing')])
def test_gpu_percent_command_failure_is_audited(monkeypatch, failure):
    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(host_sensor.subprocess, 'run', run)
    events = []
    monkeypatch.setattr(host_sensor, 'audit', lambda *args: events.append(args))
    assert host_sensor.gpu_percent() is None
    assert events[0] == ('gpu.read', 'error', {'type': type(failure).__name__})


def test_gpu_percent_malformed_plist_is_audited(monkeypatch):
    monkeypatch.setattr(host_sensor.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=b'<?xml version="1.0"?><plist>'))
    events = []
    monkeypatch.setattr(host_sensor, 'audit', lambda *args: events.append(args))
    assert host_sensor.gpu_percent() is None
    assert events[0][:2] == ('gpu.read', 'error')


def test_top_applications_keeps_busy_low_memory_process():
    apps = [{'pid': i, 'memory_bytes': 1000 - i, 'cpu_percent': 0} for i in range(30)]
    apps.append({'pid': 99, 'memory_bytes': 1, 'cpu_percent': 80})
    result = host_sensor.top_applications(apps, limit=20)
    pids = [app['pid'] for app in result]
    assert 99 in pids
    assert len(pids) == len(set(pids)) == 21
    assert pids[0] == 0


def test_battery_reports_state_and_hides_unknown_time(monkeypatch):
    monkeypatch.setattr(host_sensor.psutil, 'sensors_battery', lambda: SimpleNamespace(percent=66.04, secsleft=17220, power_plugged=False))
    assert host_sensor.battery() == {'percent': 66.0, 'plugged': False, 'seconds_left': 17220}
    monkeypatch.setattr(host_sensor.psutil, 'sensors_battery', lambda: SimpleNamespace(percent=100, secsleft=host_sensor.psutil.POWER_TIME_UNLIMITED, power_plugged=True))
    assert host_sensor.battery()['seconds_left'] is None
    monkeypatch.setattr(host_sensor.psutil, 'sensors_battery', lambda: None)
    assert host_sensor.battery() is None
