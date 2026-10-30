"""Field-tool safety/unit checks; full HTTP run is explicit, not multiplied per CI matrix."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('field_http', ROOT/'tools/field_http.py')
field = importlib.util.module_from_spec(spec)
spec.loader.exec_module(field)


def test_prepare_creates_synthetic_settings_only_and_refuses_reuse(tmp_path):
    dest = tmp_path/'field space 한글'
    source_before = (ROOT/'config/event.example.json').read_bytes()
    field.prepare(dest, 'https://field.example.invalid')
    event = json.loads((dest/'config/event.json').read_text())
    cfg = json.loads((dest/'config/operations.json').read_text())
    assert event['id'].startswith('field-') and not event['draft']
    assert len(event['products'])==10 and all(p['name'].startswith('TEST') for p in event['products'])
    assert cfg['ExternalBackupPaths']==[] and cfg['Port']!=cfg['MetricsPort']
    assert not (dest/'data').exists() and not (dest/'app').exists()
    before = (dest/'config/event.json').read_bytes()
    with pytest.raises(field.CheckFailed): field.prepare(dest, 'https://field.example.invalid')
    assert (dest/'config/event.json').read_bytes()==before
    assert (ROOT/'config/event.example.json').read_bytes()==source_before


def test_existing_root_and_symlink_are_never_used(tmp_path):
    dest = tmp_path/'existing'; dest.mkdir()
    (dest/'sentinel').write_text('keep')
    with pytest.raises(field.CheckFailed): field.new_output(dest)
    assert (dest/'sentinel').read_text()=='keep'
    link = tmp_path/'linked'
    try: link.symlink_to(dest, target_is_directory=True)
    except OSError: pytest.skip('Symlink creation not permitted on this host')
    with pytest.raises(field.CheckFailed): field.new_output(link/'new')
    assert not (dest/'new').exists()


def test_runtime_guard_precedes_any_output_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(field,'inspect_runtime',lambda:dict(python='0',sqlite='0',sqlite_source_id='bad'))
    with pytest.raises(field.CheckFailed): field.run(tmp_path/'not-created')
    assert not (tmp_path/'not-created').exists()


def test_statistics_keep_errors_and_groups_without_payloads():
    rows=[dict(group='normal',route='/api/collect',status=200,ms=1),
          dict(group='normal',route='/api/collect',status=None,ms=20),
          dict(group='injected_admission',route='/api/collect',status=503,ms=15000)]
    result=field.summarize(rows)
    assert result['normal /api/collect']['n']==2
    assert result['normal /api/collect']['statuses']=={'200':1,'None':1}
    assert result['injected_admission /api/collect']['p99_ms']==15000


@pytest.mark.parametrize('origin', [
    'http://field.example.invalid', 'https://user:password@field.example.invalid',
    'https://field.example.invalid/path', 'https://field.example.invalid:443',
    'https://FIELD.example.invalid',
])
def test_invalid_settings_never_publish_output(tmp_path, origin):
    target = tmp_path/'not-created'
    with pytest.raises((field.CheckFailed, field.ops.OpsError)):
        field.prepare(target, origin)
    assert not target.exists()
