"""Offline release rehearsal using a populated baseline *test* DB and baseline checkout."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory


def backup(src, dst):
    with sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(dst) as target:
        source.backup(target)


def run(root, path, program):
    subprocess.run([sys.executable, "-c", program], cwd=root, check=True,
                   env={**os.environ, "PYTHONPATH": str(root), "AOB_DEMO_DB_PATH": str(path)})


if __name__ == "__main__":
    old, fixture = map(Path, sys.argv[1:])
    current = Path(__file__).resolve().parents[1]
    with TemporaryDirectory() as tmp:
        before, after, restored = (Path(tmp) / name for name in ("before.db", "after.db", "restored.db"))
        backup(fixture, before)
        run(old, before, """
import demo_store, otlp_store
c = demo_store.connect()
assert len(c.execute('pragma table_info(external_agent_spans)').fetchall()) == 15
assert c.execute('select count(*) from trace_spans').fetchone()[0] == 3
span = dict(source_id='fixture', environment='local', run_id='old', unit_id='one', worker_id='worker',
            trace_id='a'*32, span_id='b'*16, parent_span_id=None, span_name='aob.unit.completed',
            start_time=1., end_time=2., duration_ms=1000., status='OK', validation_result='valid', attributes={})
assert otlp_store.insert_spans(c,[span]) == (1,0)
assert otlp_store.complete(c,'fixture','old',1,1)['status'] == 'TRACE_COMPLETE'
c.close()
""")
        backup(before, after)
        for _ in range(2):  # Independent application processes, including restart.
            run(current, after, """
import demo_store, otlp_store
c = demo_store.connect()
assert c.execute('pragma integrity_check').fetchone()[0] == 'ok'
assert c.execute('select count(*) from external_agent_conflicts').fetchone()[0] == 0
assert all(c.execute(f'select {col} from external_agent_spans').fetchone()[0] is None for col in otlp_store.EXTRA)
s = otlp_store.summary(c,'fixture','old')
assert (s['trace_status'],s['completed_units'],s['span_count']) == ('TRACE_COMPLETE',1,1)
c.close()
""")
        with sqlite3.connect(before) as original, sqlite3.connect(after) as migrated:
            for (table,) in original.execute("select name from sqlite_master where type='table'"):
                columns = ','.join(r[1] for r in original.execute(f'pragma table_info("{table}")'))
                sql = f'select {columns} from "{table}" order by rowid'
                assert original.execute(sql).fetchall() == migrated.execute(sql).fetchall(), table
        backup(before, restored)
        for path in (after, restored):
            run(old, path, """
import demo_store, otlp_store
c = demo_store.connect()
assert otlp_store.summary(c,'fixture','old')['trace_status'] == 'TRACE_COMPLETE'
assert c.execute('select count(*) from trace_spans').fetchone()[0] == 3
assert c.execute('pragma integrity_check').fetchone()[0] == 'ok'
c.close()
""")
        print("Migration, restart, original row projections, old reader and restored backup: PASS")
