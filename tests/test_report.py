"""Отчёт о версии на сайт (report.py): без root и сети — отправку подменяет заглушка.

    python3 tests/test_report.py
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import core  # noqa: E402
from core import Stop  # noqa: E402
from report import Reporter  # noqa: E402

TMP = tempfile.mkdtemp(prefix='nas-update-report-')
core.STATE = TMP


class FakeApp:
    def __init__(self, name='minion2'):
        self.state = os.path.join(TMP, name)
        os.makedirs(self.state, exist_ok=True)
        self.outcome = None

    def path(self, name):
        return os.path.join(self.state, name)

    def version_info(self):
        return {'commit': 'abc1234567890', 'image': 'sha256:deadbeef', 'branch': 'main'}


class Section(dict):
    pass


sent = []
codes = []


def fake_send(body):
    sent.append(body)
    return codes.pop(0) if codes else 201


def reporter(app, **over):
    sec = Section(url='https://daetotyoma.com/admin/versions/minion2-nas', key='k', heartbeat_hours='24')
    sec.update(over)
    return Reporter(app, sec, send=fake_send)


def test_disabled_without_section():
    app = FakeApp('a')
    r = Reporter(app, None, send=fake_send)
    assert not r.enabled and r.post('deploy', True, 'x') is False and not sent


def test_payload_and_deploy():
    app = FakeApp('b')
    app.outcome = {'event': 'deploy', 'message': 'обновлено до abc1234', 'commit': 'abc1234567890',
                   'prev_commit': 'old', 'image': 'sha256:deadbeef'}
    r = reporter(app)
    r.after_run(0)
    assert sent[-1]['event'] == 'deploy' and sent[-1]['ok'] is True
    assert sent[-1]['commit'] == 'abc1234567890' and sent[-1]['prev_commit'] == 'old'
    assert sent[-1]['host'].startswith('nas-') and sent[-1]['finished_at'].endswith('Z')


def test_rollback_and_error_only_new():
    app = FakeApp('c')
    app.outcome = {'event': 'rollback', 'commit': 'old', 'prev_commit': 'new'}
    r = reporter(app)
    r.stopped(Stop(4, 'новая версия упала, откат удался'), 4)
    assert sent[-1]['event'] == 'rollback' and sent[-1]['ok'] is False and sent[-1]['commit'] == 'old'
    n = len(sent)
    r.stopped(Stop(3, 'заблокировано'), 0)  # повтор, письмо не шлётся — и отчёт тоже
    assert len(sent) == n
    r.stopped(Stop(3, 'заблокировано: политика'), 3)
    assert sent[-1]['event'] == 'error' and 'политика' in sent[-1]['message']


def test_heartbeat_once_per_period():
    app = FakeApp('d')
    r = reporter(app, heartbeat_hours='1')
    assert r.heartbeat() is True and sent[-1]['event'] == 'heartbeat'
    assert r.heartbeat() is False, 'второй раз в тот же час — молчим'
    core.write_json(app.path('report-heartbeat.json'), {'at': time.time() - 2 * 3600})
    assert r.heartbeat() is True


def test_outbox_when_offline():
    app = FakeApp('e')
    r = reporter(app)
    codes.extend([0])  # сети нет
    assert r.post('deploy', True, 'x') is False
    assert os.path.exists(r.outbox_path())
    codes.extend([201, 201])  # сеть вернулась: сначала отложенное, потом новое
    assert r.post('heartbeat', True, 'y') is True
    assert not os.path.exists(r.outbox_path())
    assert [b['event'] for b in sent[-2:]] == ['deploy', 'heartbeat']


def test_rejected_not_queued():
    app = FakeApp('f')
    r = reporter(app)
    codes.extend([401])
    assert r.post('deploy', True, 'x') is False and not os.path.exists(r.outbox_path())


def test_dry_run_silent():
    app = FakeApp('g')
    sec = Section(url='https://x', key='k')
    r = Reporter(app, sec, dry=True, send=fake_send)
    n = len(sent)
    r.after_run(0)
    assert len(sent) == n


def test_sick_containers():
    from core import sick_containers
    run = {'Id': '1', 'Name': '/a', 'State': {'Status': 'running'}, 'RestartCount': 0}
    unhealthy = {'Id': '2', 'Name': '/b', 'State': {'Status': 'running', 'Health': {'Status': 'unhealthy'}}, 'RestartCount': 0}
    exited = {'Id': '3', 'Name': '/c', 'State': {'Status': 'exited'}, 'RestartCount': 0}
    restarted = {'Id': '4', 'Name': '/d', 'State': {'Status': 'running'}, 'RestartCount': 2}
    assert sick_containers([run]) == []
    assert [c['Id'] for c in sick_containers([run, unhealthy, exited, restarted])] == ['2', '3', '4']
    assert sick_containers([restarted], baseline={'4': 2}) == []


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            try:
                fn()
                print('ok  ', name)
            except Exception as e:
                fails += 1
                print('FAIL', name, repr(e))
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if fails else 0)
