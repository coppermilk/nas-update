#!/usr/bin/env python3
"""nas-update: безопасные автообновления docker-приложений на Synology NAS.

Одна задача в Task Scheduler (root, каждые 5 минут) запускает этот файл.
Он проходит по apps/*.conf; у каждого приложения своё расписание и свой
источник обновлений:

  source = git        код из репозитория, куда пушат агенты (недоверенный):
                      compose в клетке, политика, свой пользователь — git_source.py
  source = releases   релизы стороннего проекта на GitHub: major, задержка,
                      стоп на breaking-изменениях — release_source.py

Общее (core.py): свежий снапшот перед обновлением, проверка здоровья и
откат, одно письмо на одну проблему, чистка старых образов.

  nas-update.py                    обновить то, чему пришло время
  nas-update.py --dry-run [app…]   всё проверить и показать, ничего не меняя

Коды выхода (на ненулевой DSM шлёт письмо; если приложений несколько — худший):
0 готово или нечего делать; 2 ошибка; 3 нужно твоё решение (заблокировано,
отложено, нет снапшота, вышел новый major); 4 новая версия упала, откат
удался; 5 откат тоже не поднялся.

Совместимо с Python 3.8 (системный python3 в DSM 7).
"""
import argparse
import fcntl
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python3 -I не кладёт её сам

import core  # noqa: E402
from core import LOG, App, Failed, Stop, load_conf, log, under  # noqa: E402
from git_source import GitApp  # noqa: E402
from release_source import ReleaseApp  # noqa: E402
from report import Reporter  # noqa: E402

SOURCES = {'git': GitApp, 'releases': ReleaseApp}
APPS = core.APPS


def run_app(name, dry):
    """Одно приложение: код выхода, ошибки и письма — только его."""
    LOG['app'] = name
    app = None
    rep = None  # отчёт о версии на сайт ([report] в конфиге, report.py)
    try:
        c = load_conf(os.path.join(APPS, name + '.conf'))
        source = SOURCES.get(c.get('source', ''))
        if not source:
            raise Stop(2, '%s.conf: source = git или releases' % name, 'config')
        app = source(name, c, dry)
        rep = Reporter(app, c.parser['report'] if c.parser.has_section('report') else None, dry)
        if not app.due():
            rep.heartbeat()
            return 0
        log('===== %s =====' % ('dry-run: ничего не меняю' if dry else 'старт'))
        for root in app.guarded_paths():
            if under(core.HERE, root):
                raise Stop(2, 'nas-update лежит внутри %s, а это видят git или контейнеры' % root,
                           'config')
        code = app.run()
    except Stop as e:
        app = app or App(name, None, dry)
        if e.kind is None:
            app.mark_done()
        rc = app.stopped(e)
        if rep:
            rep.stopped(e, rc)
        return rc
    except Failed as e:
        app = app or App(name, None, dry)
        e = Stop(2, str(e), 'error')
        rc = app.stopped(e)
        if rep:
            rep.stopped(e, rc)
        return rc
    except Exception as e:  # ошибка в самом nas-update
        for line in traceback.format_exc().splitlines():
            log('  ' + line, stamp=False)
        app = app or App(name, None, dry)
        e = Stop(2, 'сбой nas-update: %r' % e, 'crash')
        rc = app.stopped(e)
        if rep:
            rep.stopped(e, rc)
        return rc
    finally:
        LOG['app'] = ''
    if not dry:
        app.clear_alerts()
    app.mark_done()
    rep.after_run(code)
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(description='nas-update: безопасные автообновления на NAS')
    ap.add_argument('--dry-run', action='store_true', help='всё проверить, ничего не меняя')
    ap.add_argument('apps', nargs='*', help='только эти приложения (по умолчанию все из apps/)')
    a = ap.parse_args(argv)

    try:  # DSM Task Scheduler даёт голое окружение без локали
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
    os.environ['PATH'] = '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/syno/bin:' \
        + os.environ.get('PATH', '')
    os.makedirs(core.STATE, exist_ok=True)
    logpath = os.path.join(core.STATE, 'update.log')
    if os.path.exists(logpath) and os.path.getsize(logpath) > 5 << 20:
        os.replace(logpath, logpath + '.old')
    LOG['file'] = open(logpath, 'a', encoding='utf-8')

    lock = open(os.path.join(core.STATE, 'lock'), 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log('другой запуск ещё идёт, пропускаю')
        return 0

    names = sorted(f[:-5] for f in os.listdir(APPS) if f.endswith('.conf')) if os.path.isdir(APPS) else []
    unknown = set(a.apps) - set(names)
    if unknown or not names:
        log('нет приложений: %s (жду их в %s/*.conf)' % (' '.join(sorted(unknown)) or 'ни одного', APPS))
        return 2
    return max(run_app(name, a.dry_run) for name in names if not a.apps or name in a.apps)


if __name__ == '__main__':
    sys.exit(main())
